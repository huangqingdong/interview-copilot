/* 面试运行页：浏览器音频采集 → 整段或流式 ASR → 按「翻译 / 回答」开关处理。
   两个开关默认全关，此时只记录原文、不调用大模型；答案语言跟随提问语种。
   手机 / 第二设备为只读展示端，轮询同一份会话状态。零第三方依赖。 */
(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const TOKEN = new URLSearchParams(location.search).get('token') ||
    sessionStorage.getItem('copilot_token') || '';
  const SECURE = window.isSecureContext && !!navigator.mediaDevices;

  let S = null;
  let running = false;
  let starting = false, stopping = false, manualBusy = false;
  let captureSessionId = null, captureAbort = null, lastSession = null;
  let stream = null, audioCtx = null, node = null, mute = null;
  let chunks = [], pcmLen = 0, voiced = 0, silent = 0, heard = false;
  let queue = [], asrWork = null, turnTail = Promise.resolve(), pendingTurns = 0;
  let manualWork = null;
  let poller = null, polling = false, dropped = 0;
  let realtime = null, remoteStreaming = false, previewPoller = null, previewBusy = false;
  let hostCapture = null, outputUpdates = Promise.resolve();
  let lastPreview = '', lastWasStreaming = false;
  let lastLatency = null, lastGeneration = null, lastTotal = null, lastError = '';
  const MIN_VOICE = 350 * 16;
  const PRE_ROLL = 250 * 16;
  const END_PAD = 150 * 16;
  const STREAM_FRAME = 100 * 16;
  const STREAM_LIMIT = 2000 * 16;
  const REALTIME_API = '/api/interview/asr/realtime/';

  // 翻译 / 回答是两个独立总闸：默认全关，刷新页面保留上次选择。
  const TOGGLE_KEY = 'copilot.iv.toggles';
  let wantTranslate = false, wantAnswer = false;
  try {
    const saved = JSON.parse(localStorage.getItem(TOGGLE_KEY) || '{}');
    wantTranslate = !!saved.translate;
    wantAnswer = !!saved.answer;
  } catch (e) { /* 本地值损坏就按默认全关 */ }

  function paintToggles() {
    [['ivTranslate', wantTranslate],
     ['ivAnswer', wantAnswer]].forEach(row => {
      const b = $(row[0]);
      if (!b) return;
      b.classList.toggle('on', row[1]);
      b.setAttribute('aria-pressed', row[1] ? 'true' : 'false');
    });
  }

  function toggle(kind) {
    if (kind === 'translate') wantTranslate = !wantTranslate;
    else wantAnswer = !wantAnswer;
    try {
      localStorage.setItem(TOGGLE_KEY, JSON.stringify({
        translate: wantTranslate, answer: wantAnswer
      }));
    } catch (e) { /* 隐私模式下写不了本地存储，本次会话仍生效 */ }
    paintToggles();
    updateNotes();
    renderStats();
    if (hostCapture && lastSession) {
      const body = { session_id: lastSession.id, translate: wantTranslate, answers: wantAnswer };
      outputUpdates = outputUpdates
        .then(() => req('/api/interview/capture/outputs', body))
        .catch(e => {
          if (hostCapture && lastSession && lastSession.id === body.session_id)
            setLive('后台采集开关更新失败：' + e.message, true);
        });
    }
    status(wantTranslate || wantAnswer
      ? '翻译 ' + (wantTranslate ? '开' : '关') + ' / 回答 ' +
        (wantAnswer ? '开' : '关') + '：下一轮起生效'
      : '两个开关都已关闭：只记录原文，不再调用大模型');
  }

  async function req(path, body, signal) {
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (TOKEN) headers['X-Copilot-Token'] = TOKEN;
    const res = await fetch(path, body === undefined
      ? { headers, signal }
      : { method: 'POST', headers, body: JSON.stringify(body), signal });
    rejectLocalUi(res);
    let data;
    try { data = await res.json(); }
    catch (e) { throw new Error('响应不是 JSON (HTTP ' + res.status + ')'); }
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function status(msg, bad) {
    const bar = $('statusBar');
    if (!bar) return;
    bar.textContent = msg;
    bar.style.color = bad ? '#a02c2c' : '';
  }

  function setLive(msg, bad) {
    const el = $('ivLive');
    if (!el) return;
    el.hidden = !msg;
    el.textContent = msg;
    el.classList.toggle('bad', !!bad);
    if (bad) lastError = msg;
  }

  // ------------------------------------------------------------- 音频采集

  const WORKLET = `
    class PcmTap extends AudioWorkletProcessor {
      constructor() {
        super();
        this.ratio = sampleRate / 16000;
        this.in = [];
        this.next = 0;
      }
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (!ch) return true;
        for (let i = 0; i < ch.length; i++) this.in.push(ch[i]);
        const out = [];
        while (this.next + 1 < this.in.length) {
          const i = Math.floor(this.next), f = this.next - i;
          out.push(this.in[i] * (1 - f) + this.in[i + 1] * f);
          this.next += this.ratio;
        }
        if (out.length) this.port.postMessage(new Float32Array(out));
        if (this.in.length > 8192) {
          const drop = Math.floor(this.next) - 2;
          if (drop > 0) { this.in = this.in.slice(drop); this.next -= drop; }
        }
        return true;
      }
    }
    registerProcessor('pcm-tap', PcmTap);
  `;

  function resetPcm() {
    chunks = []; pcmLen = 0; voiced = 0; silent = 0; heard = false;
  }

  function onPcm(f32) {
    const pcm = new Int16Array(f32.length);
    let sum = 0;
    for (let i = 0; i < f32.length; i++) {
      const v = Math.max(-1, Math.min(1, f32[i]));
      pcm[i] = v * 32767;
      sum += v * v;
    }
    if (realtime) {
      streamPcm(realtime, pcm);
      return;
    }
    const rms = Math.sqrt(sum / Math.max(1, f32.length));
    const loud = rms > 0.012;
    if (loud) { silent = 0; voiced += pcm.length; heard = true; }
    else if (heard) silent += pcm.length;
    chunks.push(pcm);
    pcmLen += pcm.length;

    const ms = n => n / 16;
    const silenceMs = parseInt($('ivSilence').value, 10) || 500;
    if (!heard) {
      while (pcmLen > PRE_ROLL) pcmLen -= chunks.shift().length;
    } else if (ms(silent) >= silenceMs) flush();
    else if (ms(pcmLen) >= 12000) flush();
  }

  function wavBase64() {
    const buf = new ArrayBuffer(44 + pcmLen * 2);
    const v = new DataView(buf);
    const str = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + pcmLen * 2, true);
    str(8, 'WAVE'); str(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, 16000, true); v.setUint32(28, 32000, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, 'data'); v.setUint32(40, pcmLen * 2, true);
    const out = new Int16Array(buf, 44, pcmLen);
    let off = 0;
    for (const c of chunks) {
      const part = c.subarray(0, pcmLen - off);
      out.set(part, off);
      off += part.length;
      if (off === pcmLen) break;
    }
    return bytesBase64(new Uint8Array(buf));
  }

  function bytesBase64(bytes) {
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000)
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }

  function showPreview(event) {
    if (!event || !event.text) return;
    const message = (event.type === 'partial' ? '正在转写：' : '已听到：') + event.text;
    if (lastPreview === message) return;
    lastPreview = message;
    setLive(message);
  }

  function acceptRealtime(result, stream) {
    for (const event of result.events) {
      if (event.type === 'error') {
        stream.error = event.message;
        setLive(event.message, true);
      } else if (event.type === 'notice') {
        setLive(event.message, true);
      } else {
        showPreview(event);
        if (event.type === 'final') {
          lastLatency = event.latency_ms;
          enqueueTurn(event.text, event.lang, {
            session_id: captureSessionId, submittedAt: performance.now(),
            translate: wantTranslate, answers: wantAnswer
          });
          renderStats();
        }
      }
    }
  }

  function streamPcm(stream, pcm) {
    if (stream.error) return;
    stream.chunks.push(pcm);
    stream.samples += pcm.length;
    if (stream.samples > STREAM_LIMIT) {
      stream.error = '流式音频积压，采集已停止';
      void stop(true);
    } else if (!stream.work && stream.samples >= STREAM_FRAME) {
      stream.work = sendRealtime(stream);
    }
  }

  async function sendRealtime(stream) {
    const pcm = new Int16Array(stream.samples);
    let offset = 0;
    for (const chunk of stream.chunks) {
      pcm.set(chunk, offset);
      offset += chunk.length;
    }
    stream.chunks = [];
    stream.samples = 0;
    try {
      const result = await req(REALTIME_API + 'audio', {
        stream_id: stream.id, audio: bytesBase64(new Uint8Array(pcm.buffer))
      }, AbortSignal.any([captureAbort.signal, AbortSignal.timeout(5000)]));
      if (realtime !== stream) return;
      acceptRealtime(result, stream);
      if (result.closed) stream.error ||= '流式 ASR 连接已中断';
    } catch (e) {
      if (realtime === stream) stream.error = '实时音频发送失败：' + e.message;
    } finally {
      stream.work = null;
      if (realtime === stream && stream.error && running && !stopping) void stop(true);
    }
  }

  async function finishRealtime(stream, save) {
    try {
      if (save) {
        await stream.work;
        if (!stream.error && stream.samples) await sendRealtime(stream);
      }
      const result = await req(REALTIME_API + 'finish', { stream_id: stream.id, cancel: !save });
      if (save) {
        acceptRealtime(result, stream);
        if (!result.finalized) stream.error ||= '流式 ASR 未确认最后一段，已保存已收到的识别结果';
      }
    } catch (e) {
      if (save) stream.error ||= '流式 ASR 结束失败：' + e.message;
    }
  }

  function flush() {
    if (voiced < MIN_VOICE) { resetPcm(); return; }
    pcmLen -= Math.max(0, silent - END_PAD);
    queue.push({
      audio_b64: wavBase64(), language: $('ivLang').value,
      session_id: captureSessionId, submittedAt: performance.now(),
      translate: wantTranslate, answers: wantAnswer
    });
    resetPcm();
    if (queue.length > 6) {
      queue.shift();
      dropped += 1;
      setLive('识别积压，已跳过最早的一段音频', true);
    }
    if (!asrWork) asrWork = drain();
    renderStats();
  }

  async function drain() {
    while (queue.length) {
      const clip = queue.shift();
      try {
        const r = await req('/api/interview/asr', {
          audio_b64: clip.audio_b64, language: clip.language
        }, captureAbort.signal);
        if (clip.session_id !== captureSessionId) continue;
        lastLatency = r.latency_ms;
        if (!r.text.trim()) {
          setLive('这一段没识别到语音，已跳过（' + r.duration_ms + ' ms 音频）');
        } else {
          setLive('已听到：' + r.text.slice(0, 70));
          enqueueTurn(r.text, r.lang, clip);
        }
      } catch (e) {
        if (clip.session_id === captureSessionId)
          setLive('识别失败：' + e.message, true);
      }
      renderStats();
    }
    asrWork = null;
  }

  function enqueueTurn(text, lang, clip) {
    const { session_id, submittedAt, translate, answers } = clip;
    pendingTurns += 1;
    turnTail = turnTail.then(async () => {
      if (session_id !== captureSessionId) return;
      try {
        await submitTurn({ text, lang, session_id, translate, answers },
          submittedAt, captureAbort.signal);
      } catch (e) {
        if (session_id === captureSessionId)
          setLive('提交识别结果失败：' + e.message, true);
      }
    }).finally(() => {
      pendingTurns -= 1;
      renderStats();
    });
  }

  async function submitTurn(body, submittedAt = performance.now(), signal) {
    const r = await req('/api/interview/turn', body, signal);
    if (applySession(r.session)) {
      lastGeneration = r.turn.generation_ms;
      lastTotal = Math.round(performance.now() - submittedAt);
      if (r.generation_error) setLive(r.turn.skip_reason, true);
      renderStats();
    }
    return r;
  }

  // ------------------------------------------------------------- 渲染

  // 推理档模型在 max_tokens 不足时会被 reasoning 吃光配额，正文只剩半截
  // JSON；旧后端降级会把整段报文塞进 answer_zh，界面上就露出字段名。
  // 这里在渲染前兜底：能救出字段就归位，救不出就剥掉报文结构。
  function salvageAnswer(t) {
    let tr = t.translation_zh || '', az = t.answer_zh || '', ae = t.answer_en || '';
    const tag = /["']?(?:translation_zh|answer_zh|answer_en)["']?\s*:\s*/;
    const tagAll = /["']?(translation_zh|answer_zh|answer_en)["']?\s*:\s*/g;
    let note = '';
    const src = az || tr;
    if (tag.test(src)) {
      // 按字段名切段：即使末尾没闭合引号（token 被截断），也能把译文与
      // 回答分开归位，而不是把整段报文当成中文回答显示。
      const got = {}, marks = [];
      let m;
      while ((m = tagAll.exec(src)) !== null) marks.push(m);
      marks.forEach((mk, i) => {
        let seg = src.slice(mk.index + mk[0].length,
          i + 1 < marks.length ? marks[i + 1].index : src.length);
        seg = seg.replace(/^["']+/, '')
          .replace(/["']?\s*[,}\]]*\s*$/, '')
          .replace(/\\n/g, '\n').replace(/\\"/g, '"').trim();
        if (seg && !got[mk[1]]) got[mk[1]] = seg;
      });
      if (Object.keys(got).length) {
        tr = got.translation_zh || '';
        az = got.answer_zh || '';
        ae = got.answer_en || '';
        note = '模型输出被截断，已按字段提取；建议调大 max_tokens 或换快档模型';
      } else {
        const strip = s => String(s).replace(/^\s*[\[{]\s*/, '')
          .replace(/["']?(?:translation_zh|answer_zh|answer_en)["']?\s*:\s*/g, '')
          .replace(/["']\s*,\s*["']?/g, ' ')
          .replace(/[\]}]\s*$/, '').trim();
        az = strip(az); tr = strip(tr);
        note = '模型输出不完整，已去掉报文结构；建议调大 max_tokens 或换快档模型';
      }
    }
    return { translation_zh: tr, answer_zh: az, answer_en: ae, note: note };
  }

  function renderTurns(session) {
    const box = $('ivTurns');
    if (!box || !session) return;
    if (!session.turns.length) {
      box.innerHTML = '<div class="iv-empty"><div class="empty-wave" aria-hidden="true">' +
        '<i></i><i></i><i></i><i></i><i></i></div><h3>还没有转写</h3>' +
        '<p>' + ((SECURE || shareSafe()) ? '开始面试后，识别原文会显示在这里。' : '在电脑端开始面试后，转写会同步到这里。') +
        '</p></div>';
      return;
    }
    box.innerHTML = session.turns.slice().reverse().map(t => {
      const parts = [];
      const a = salvageAnswer(t);
      parts.push('<article class="iv-turn">');
      parts.push('<div class="iv-head"><b>' +
        (t.lang === 'en' ? '英文提问' : '中文提问') + '</b><span>' +
        new Date(t.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) +
        '</span></div>');
      parts.push('<div class="iv-q">' + esc(t.text) + '</div>');
      if (a.translation_zh)
        parts.push('<div class="iv-tr"><span>译文</span>' + esc(a.translation_zh) + '</div>');
      if (a.answer_zh)
        parts.push('<div class="iv-a"><span>中文回答</span>' + esc(a.answer_zh) + '</div>');
      if (a.answer_en)
        parts.push('<div class="iv-a en"><span>English answer</span>' + esc(a.answer_en) + '</div>');
      if (!a.answer_zh && !a.answer_en && /失败|超时|未就绪|未返回|缺少/.test(t.skip_reason || ''))
        parts.push('<div class="iv-none">' + esc(t.skip_reason) + '</div>');
      if (t.degraded || a.note)
        parts.push('<div class="iv-none">' +
          esc(a.note || '模型未按标准 JSON 返回，已提取正文；若内容被截断，可调大 max_tokens 或换快档模型') +
          '</div>');
      parts.push('</article>');
      return parts.join('');
    }).join('');
  }

  function renderStats() {
    const el = $('ivStats');
    if (!el) return;
    const s = S && S.interview || {};
    const label = starting ? '连接中' : stopping ? '正在结束' : s.active ? '进行中'
      : lastSession && lastSession.ended_at ? '已结束' : '未开始';
    const sessionStatus = $('ivSessionStatus');
    $('ivSessionLabel').textContent = label;
    sessionStatus.classList.toggle('is-active', !!s.active && !stopping);
    $('ivCount').textContent = (s.count || 0) + ' 轮';
    const duration = value => value == null ? '—' : value + ' ms';
    const speech = s.speech || {};
    const queued = hostCapture ? hostCapture.audio_queue : queue.length;
    const pending = hostCapture ? hostCapture.turn_queue : pendingTurns;
    const stats = [
      [lastWasStreaming || hostCapture?.streaming ? '断句到最终转写' : '最近语音识别',
        duration(hostCapture ? hostCapture.latency_ms : lastLatency)],
      ['最近翻译 / 回答', duration(hostCapture ? lastSession?.turns.at(-1)?.generation_ms : lastGeneration)],
      [lastWasStreaming ? '最终转写后耗时' : '断句后整轮耗时', duration(lastTotal)],
      ['排队音频 / 轮次', queued + ' / ' + pending],
      ['ASR 模型', speech.model || '—'],
      ['识别地址', speech.url || '—'],
      ['语言模型', s.llm_ready ? '就绪' : (llmProblems()[0] || '未就绪')]
    ];
    if (dropped) stats.push(['积压跳过音频', dropped]);
    if (hostCapture) {
      stats.push(['电脑音频设备', hostCapture.source]);
      if (hostCapture.dropped) stats.push(['积压跳过音频', hostCapture.dropped]);
    }
    if (lastError) stats.push(['最近错误', lastError]);
    el.innerHTML = stats.map(([name, value]) =>
      '<div><dt>' + esc(name) + '</dt><dd>' + esc(value) + '</dd></div>').join('');
  }

  // 把「未就绪」还原成具体原因，并找出已经配好 key、可直接切换的服务。
  function llmProblems() {
    return (((S && S.provider) || {}).problems || [])
      .filter(x => String(x).indexOf('[llm:') === 0);
  }

  function readyLlmIds() {
    const secs = ((S && S.provider) || {}).secrets || {};
    return Object.keys(secs)
      .filter(k => k.indexOf('llm:') === 0 && secs[k] && secs[k].set)
      .map(k => k.slice(4));
  }

  function setState(s) {
    S = s;
    const iv = s && s.interview || {};
    const sources = $('ivSource').options;
    sources[0].textContent = shareSafe() ? '电脑播放声音' : '屏幕共享音频';
    sources[1].textContent = shareSafe() ? '电脑麦克风' : '麦克风';
    updateNotes();
    updateControls();
    $('ivReview').disabled = !iv.llm_ready;
  }

  function shareSafe() { return !!(S && S.access && S.access.share_safe); }

  function updateControls() {
    const busy = starting || stopping || manualBusy;
    $('ivStart').disabled = (!SECURE && !shareSafe()) || running || busy || !!hostCapture;
    $('ivEnd').disabled = busy || !(running || (S && S.interview.active));
    $('ivSend').disabled = busy;
    $('ivClear').disabled = busy;
    $('ivLang').disabled = $('ivSilence').disabled = starting || stopping || !!realtime || !!hostCapture;
    $('ivMode').disabled = $('ivSource').disabled = starting || stopping || running || !!hostCapture;
    renderStats();
  }

  function updateNotes() {
    const iv = S && S.interview || {};
    const provider = S && S.provider;
    const speechOk = !!(iv.speech && iv.speech.ready);
    const llmNeeded = wantTranslate || wantAnswer;
    const notices = [];
    if (shareSafe()) notices.push('共享模式：电脑只采集声音，界面显示在此设备。');
    else if (!SECURE) notices.push('请在电脑本机采集音频，此处会同步转写。');
    if ((SECURE || shareSafe()) && !speechOk) notices.push((provider && !provider.active_ids.speech)
      ? '语音识别已停用。' : '请先配置语音识别服务。');
    if (llmNeeded && !iv.llm_ready)
      notices.push('语言模型未就绪，已开启的翻译或回答暂不可用。');
    $('ivNotice').hidden = notices.length === 0;
    $('ivNoticeText').textContent = notices.join(' ');
    $('ivSetupLink').hidden = !(((SECURE || shareSafe()) && !speechOk) || (llmNeeded && !iv.llm_ready));

    const notes = [];
    if (!speechOk && iv.speech && iv.speech.reason)
      notes.push(esc(iv.speech.reason));
    if (llmNeeded && !iv.llm_ready) {
      const activeLlm = (provider && provider.active_ids.llm) || '';
      const why = llmProblems();
      const usable = readyLlmIds().filter(id => id !== activeLlm);
      notes.push('<b>已打开翻译 / 回答，但 LLM 未就绪。</b>' +
        (why.length ? esc(why.join('；')) : '') +
        (usable.length ? ' 已填好密钥的服务：' + esc(usable.join('、')) + '。可在「服务」中切换。' : ''));
    }
    if (shareSafe()) {
      notes.push('「电脑播放声音」采集 Windows 默认播放设备的声音，请把会议输出设到该设备。');
    } else if (SECURE) {
      notes.push('共享时必须先点选会议标签页或窗口。网页会议勾选<b>共享标签页音频</b>，客户端会议打开<b>使用系统音频共享</b>。请避开名为 Notes 的本工具窗口。');
      notes.push('使用麦克风时，佩戴耳机可减少回音。');
    }
    notes.push('开关对下一轮生效，历史轮次不会回溯补答案。');
    const noteEl = $('ivNote');
    noteEl.innerHTML = notes.map(note => '<p>' + note + '</p>').join('');
  }

  function applySession(s) {
    if (lastSession && (s.started_at < lastSession.started_at ||
        (s.id === lastSession.id &&
          (s.count < lastSession.count || (lastSession.ended_at && !s.ended_at)))))
      return false;
    if (!lastSession || s.id !== lastSession.id || s.count !== lastSession.count)
      renderTurns(s);
    lastSession = s;
    if (S) {
      Object.assign(S.interview, { active: s.active, mode: s.mode, count: s.count });
      updateControls();
    }
    return true;
  }

  async function loadSession() {
    if (polling) return;
    polling = true;
    try {
      const r = await req('/api/interview/session');
      const captureError = r.capture && r.capture.error;
      const oldCaptureError = hostCapture && hostCapture.error;
      if (!applySession(r.session)) return;
      hostCapture = r.capture || null;
      if (captureError && captureError !== oldCaptureError) setLive(captureError, true);
      updateControls();
      if (remoteStreaming && !r.streaming && !running && !stopping) {
        lastPreview = '';
        setLive('转写已结束');
      }
      remoteStreaming = r.streaming;
    } catch (e) { /* 轮询失败不打断采集 */ }
    finally { polling = false; }
  }

  async function loadPreview() {
    if (!remoteStreaming || realtime || starting || stopping || previewBusy) return;
    previewBusy = true;
    try {
      const r = await req('/api/interview/asr/live');
      if (!realtime && !starting && !stopping && remoteStreaming &&
          lastSession && r.session_id === lastSession.id) showPreview(r.preview);
    } catch (e) { /* 预览同步失败不影响采集 */ }
    finally { previewBusy = false; }
  }

  function onShow(show) {
    if (show) {
      loadSession();
      if (!poller) poller = setInterval(loadSession, 1500);
      if (!previewPoller) previewPoller = setInterval(loadPreview, 300);
    } else if (poller) {
      clearInterval(poller); poller = null;
      clearInterval(previewPoller); previewPoller = null;
    }
  }

  // ------------------------------------------------------------- 操作

  async function start() {
    if (starting || stopping || manualBusy || running || hostCapture) return;
    if (!SECURE && !shareSafe()) { setLive('请使用电脑本机页面或启动共享模式', true); return; }
    const iv = (S && S.interview) || {};
    if (!(iv.speech && iv.speech.ready)) {
      setLive('ASR 未就绪：' + ((iv.speech && iv.speech.reason) || '未配置') +
        '。请到「设置」编辑识别地址或密钥；也可通过「手动输入」提交问题。', true);
      status('请先配置 ASR 服务', true);
      return;
    }
    if (!iv.llm_ready) {
      if (wantTranslate || wantAnswer) {
        setLive('翻译 / 回答已打开。请在「服务」选择语言模型，并在「设置」填写密钥。', true);
        status('LLM 未就绪，翻译 / 回答开关已打开', true);
      } else {
        setLive('只开 ASR：两个开关都关着，本场只记录原文转写，不调用大模型。');
        status('单边模式：ASR 就绪，LLM 未启用');
      }
    }
    const mode = $('ivMode').value;
    const streaming = iv.speech.type === 'qwen_realtime_stt';
    starting = true;
    updateControls();
    try {
      const src = $('ivSource').value;
      if (shareSafe()) {
        const r = await req('/api/interview/capture/start', {
          mode, source: src, language: $('ivLang').value,
          silence_ms: Number($('ivSilence').value), translate: wantTranslate, answers: wantAnswer
        });
        hostCapture = r.capture;
        lastError = lastPreview = '';
        lastGeneration = lastTotal = null;
        applySession(r.session);
        remoteStreaming = true;
        setLive('电脑正在采集：' + hostCapture.source);
        status('面试已开始');
        return;
      }
      stream = src === 'tab'
        ? await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true })
        : await navigator.mediaDevices.getUserMedia({ audio: true });
      // 只禁用视频轨，不 stop()：部分 Chrome / Edge 把视频轨 stop 当成「结束共享」，
      // 会连带停掉同一 display stream 的音频轨，表现为共享后没声音或立刻断开。
      if (src === 'tab') stream.getVideoTracks().forEach(t => { t.enabled = false; });
      if (!stream.getAudioTracks().length)
        throw new Error('没有拿到音频轨道：共享标签页时勾「共享标签页音频」，' +
          '共享窗口时打开「使用系统音频共享」；也可以把音源改成「麦克风」');
      audioCtx = new AudioContext();
      const url = URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' }));
      try { await audioCtx.audioWorklet.addModule(url); }
      finally { URL.revokeObjectURL(url); }
      node = new AudioWorkletNode(audioCtx, 'pcm-tap');
      node.port.onmessage = e => { if (running) onPcm(e.data); };
      mute = audioCtx.createGain();
      mute.gain.value = 0;                 // 静音输出，保持音频图运转但不回放对方声音
      audioCtx.createMediaStreamSource(stream).connect(node);
      node.connect(mute); mute.connect(audioCtx.destination);
      const track = stream.getAudioTracks()[0];
      if (track.readyState === 'ended') throw new Error('音频共享已结束');
      if (streaming) setLive('正在连接百炼流式 ASR…');
      const r = streaming
        ? await req(REALTIME_API + 'open', {
          mode, language: $('ivLang').value, silence_ms: Number($('ivSilence').value)
        })
        : await req('/api/interview/start', { mode });
      captureSessionId = r.session.id;
      captureAbort = new AbortController();
      realtime = streaming
        ? { id: r.stream_id, chunks: [], samples: 0, work: null, error: '' }
        : null;
      applySession(r.session);
      resetPcm();
      running = true;
      lastError = '';
      lastPreview = '';
      lastWasStreaming = streaming;
      lastLatency = lastGeneration = lastTotal = null;
      dropped = 0;
      setLive((streaming ? '流式 ASR 正在听…' : '正在听…') +
        '（静音 ' + $('ivSilence').value + ' ms 判定一句结束）');
      status('面试已开始，模式：' + (mode === 'live' ? '实战' : '练习'));
      track.addEventListener('ended', () => stop(true), { once: true });
      if (track.readyState === 'ended') {
        starting = false;
        await stop(true);
      }
      renderStats();
    } catch (e) {
      await releaseCapture();
      const reason = (e && e.name === 'NotAllowedError')
        ? '共享授权被取消或被浏览器拒绝。请再点一次「开始面试」，' +
          '在弹窗里点选一个具体目标，等「共享」按钮变亮后再按。'
        : '音频启动失败：' + (e && e.message || e);
      setLive(reason, true);
      status('音频未启动', true);
    } finally {
      starting = false;
      updateControls();
    }
  }

  async function releaseCapture() {
    running = false;
    try { if (node) node.disconnect(); } catch (e) { }
    try { if (stream) stream.getTracks().forEach(t => t.stop()); } catch (e) { }
    try { if (audioCtx) await audioCtx.close(); } catch (e) { }
    node = mute = stream = audioCtx = null;
  }

  async function stop(save) {
    if (stopping || starting) return;
    stopping = true;
    updateControls();
    const sessionId = captureSessionId || (lastSession && lastSession.id);
    const stream = realtime;
    try {
      if (save && running && !stream) flush();
      resetPcm();
      if (!save) {
        queue = [];
        realtime = null;
        captureSessionId = null;
        if (captureAbort) captureAbort.abort();
      }
      await releaseCapture();
      if (stream) {
        if (save && !stream.error) setLive('正在确认最后一段转写…');
        await finishRealtime(stream, save);
      }
      if (save) {
        if (asrWork || pendingTurns || manualWork || hostCapture)
          setLive('正在处理剩余音频和回答，完成后保存…');
        await asrWork;
        await turnTail;
        if (manualWork) await manualWork.catch(() => {});
        const r = await req('/api/interview/end', sessionId ? { session_id: sessionId } : {});
        hostCapture = null;
        applySession(r.session);
        const error = r.capture_error || (stream && stream.error);
        setLive('已结束，共 ' + r.session.count + ' 轮' +
          (r.file ? '，记录已保存' : '') + (error ? '。' + error : ''), !!error);
        status('面试已结束');
      } else {
        const r = await req('/api/interview/clear', {});
        hostCapture = null;
        applySession(r.session);
        $('ivReport').textContent = '';
        lastLatency = lastGeneration = lastTotal = null;
        dropped = 0;
        lastError = '';
        setLive('记录已清空');
      }
    } catch (e) {
      setLive((save ? '结束失败：' : '清空失败：') + e.message, true);
    } finally {
      realtime = null;
      remoteStreaming = false;
      captureSessionId = captureAbort = null;
      stopping = false;
      updateControls();
      renderStats();
    }
  }

  async function manual() {
    if (starting || stopping || manualBusy) return;
    const text = $('ivManual').value.trim();
    if (!text) { status('请先输入问题', true); return; }
    const body = { text, lang: $('ivManualLang').value,
      translate: wantTranslate, answers: wantAnswer };
    manualBusy = true;
    updateControls();
    try {
      if (!lastSession || !lastSession.active) {
        const r = await req('/api/interview/start', { mode: $('ivMode').value });
        applySession(r.session);
      }
      body.session_id = lastSession.id;
      manualWork = submitTurn(body);
      const r = await manualWork;
      $('ivManual').value = '';
      if (!r.generation_error) setLive('已提交手动输入的一轮');
      status(r.generation_error ? '已保存原文，生成失败' : '已提交这一轮', !!r.generation_error);
    } catch (e) {
      setLive('提交失败：' + e.message, true);
      status('提交失败：' + e.message, true);
    } finally {
      manualWork = null;
      manualBusy = false;
      updateControls();
    }
  }

  async function exportMd() {
    try {
      const headers = TOKEN ? { 'X-Copilot-Token': TOKEN } : {};
      const res = await fetch('/api/interview/export?fmt=md', { headers });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'interview-' + new Date().toISOString().slice(0, 10) + '.md';
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { status('导出失败：' + e.message, true); }
  }

  async function review() {
    $('ivReport').textContent = '生成中…';
    try {
      const r = await req('/api/interview/review', {});
      $('ivReport').textContent = r.report;
    } catch (e) { $('ivReport').textContent = '复盘失败：' + e.message; }
  }

  [['ivTranslate', 'translate'], ['ivAnswer', 'answer']].forEach(row => {
    const b = $(row[0]);
    if (b) b.addEventListener('click', () => toggle(row[1]));
  });
  paintToggles();

  $('ivStart').addEventListener('click', start);
  $('ivEnd').addEventListener('click', () => stop(true));
  $('ivSend').addEventListener('click', manual);
  $('ivExport').addEventListener('click', exportMd);
  $('ivReview').addEventListener('click', review);
  $('ivClear').addEventListener('click', () => stop(false));
  $('ivRecordMenu').addEventListener('click', event => {
    if (event.target.matches('button')) {
      const menu = $('ivRecordMenu');
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });
  $('ivManual').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) manual();
  });
  window.addEventListener('beforeunload', () => {
    if (stream) stream.getTracks().forEach(t => t.stop());
    if (realtime) {
      const headers = { 'Content-Type': 'application/json' };
      if (TOKEN) headers['X-Copilot-Token'] = TOKEN;
      fetch(REALTIME_API + 'finish', { method: 'POST', headers, keepalive: true,
        body: JSON.stringify({ stream_id: realtime.id, cancel: true }) }).catch(() => {});
    }
  });

  window.InterviewView = { onShow: onShow, setState: setState };
  renderTurns({ turns: [] });
})();
