'use strict';

const $ = id => document.getElementById(id);
let S = null;
let configReady = false;
let currentView = null;
const editing = { llm: null, speech: null };

// 默认「简洁视图」：只留面试页 + 顶栏 ASR/LLM + 两把密钥；
// 完整视图恢复人设 / 资料库 / 服务池 / 预设 / 隐身等全部维护项。
function loadSimpleMode() {
  try { return localStorage.getItem('copilot_simple_mode') !== 'full'; }
  catch (e) { return true; }
}
let simpleMode = loadSimpleMode();

// 手机 / 第二设备访问时地址里带 ?token=，之后所有接口请求都带这个头
const TOKEN = new URLSearchParams(location.search).get('token') ||
  sessionStorage.getItem('copilot_token') || '';
if (TOKEN) sessionStorage.setItem('copilot_token', TOKEN);

const CFG_FIELDS = {
  llm: [
    ['id', '服务 ID', 'text'],
    ['type', '类型', 'select', ['openai_compatible', 'azure_openai']],
    ['base_url', 'Base URL', 'text'],
    ['model', '模型 / 部署名', 'text'],
    ['api_version', 'API 版本（Azure OpenAI）', 'text'],
    ['note', '备注', 'text'],
    ['supports_json_mode', '支持 JSON mode', 'check'],
    ['secret', '新密钥（留空＝不修改）', 'password'],
    ['persist_secret', '写入 config/.env（重启后仍生效）', 'check']
  ],
  speech: [
    ['id', '服务 ID', 'text'],
    ['type', 'ASR 协议', 'select', ['azure_speech_translation', 'openai_compatible_stt', 'generic_http_stt', 'qwen_realtime_stt', 'faster_whisper']],
    ['stt_url', '完整识别地址 STT URL', 'text', null, ['azure_speech_translation', 'openai_compatible_stt', 'generic_http_stt', 'qwen_realtime_stt']],
    ['base_url', 'Base URL（OpenAI 兼容，自动补 /audio/transcriptions）', 'text', null, ['openai_compatible_stt']],
    ['region', 'Region（仅 Azure）', 'text', null, ['azure_speech_translation']],
    ['endpoint', 'Azure 资源 Endpoint（留空则用 Region）', 'text', null, ['azure_speech_translation']],
    ['model', '模型名（填了就覆盖请求体模板 / 额外字段里的 model）', 'text', null, ['openai_compatible_stt', 'generic_http_stt', 'qwen_realtime_stt', 'faster_whisper']],
    ['device', 'device（faster-whisper）', 'text', null, ['faster_whisper']],
    ['stt_method', '请求方法', 'select', ['POST', 'PUT'], ['generic_http_stt']],
    ['stt_headers', '请求头 JSON（密钥用 ${COPILOT_SPEECH_<ID>_KEY} 占位引用）', 'text', null, ['generic_http_stt']],
    ['stt_body', '请求体格式', 'select', ['raw_wav', 'multipart', 'json_base64', 'json_template'], ['generic_http_stt']],
    ['audio_field', '音频字段名（multipart / json_base64）', 'text', null, ['generic_http_stt'], ['raw_wav', 'multipart', 'json_base64']],
    ['stt_extra', '额外业务字段 JSON（如 model / appkey）', 'text', null, ['generic_http_stt'], ['raw_wav', 'multipart', 'json_base64']],
    ['stt_json_template', 'JSON 请求体模板（占位符写在引号内：{{audio_data_uri}} / {{audio_base64}} / {{audio_mime}} / {{language}} / {{lang_short}}）', 'textarea', null, ['generic_http_stt'], ['json_template']],
    ['response_path', '响应文本路径（如 data.text，留空自动找）', 'text', null, ['generic_http_stt']],
    ['stt_lang_query', '语种 query 参数名（留空不传）', 'text', null, ['generic_http_stt']],
    ['note', '备注', 'text'],
    ['secret', '新密钥（留空＝不修改）', 'password'],
    ['persist_secret', '写入 config/.env（重启后仍生效）', 'check']
  ]
};

async function api(path, body) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (TOKEN) headers['X-Copilot-Token'] = TOKEN;
  const opt = body === undefined ? { headers } : {
    method: 'POST',
    headers: headers,
    body: JSON.stringify(body)
  };
  const res = await fetch(path, opt);
  rejectLocalUi(res);
  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw new Error('响应不是 JSON (HTTP ' + res.status + ')');
  }
  if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function rejectLocalUi(response) {
  if (response.headers.get('X-Copilot-Local-UI') !== 'disabled') return;
  document.documentElement.style.visibility = 'hidden';
  location.replace(location.origin + '/');
  throw new Error('已切换到仅手机显示的共享模式');
}

if (['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) {
  setInterval(() => {
    fetch('/api/health', { cache: 'no-store' }).then(rejectLocalUi).catch(() => {});
  }, 2000);
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function status(msg, bad) {
  const bar = $('statusBar');
  bar.textContent = msg;
  bar.style.color = bad ? '#a02c2c' : '';
}

function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function lines(v) { return Array.isArray(v) ? v.join('\n') : ''; }

function options(ids, selected, labelOf) {
  const label = labelOf || (n => n);
  return ids.map(n => {
    const txt = label(n);
    return '<option value="' + esc(n) + '"' +
      (n === selected ? ' selected' : '') +
      ' title="' + esc(txt) + '">' + esc(txt) + '</option>';
  }).join('');
}

// 百炼云（DashScope / 业务空间专属域名）的服务在显示名前加标识，和
// DeepSeek 官方、智谱、火山、Azure 区分开；option 的 value 仍是服务 ID。
function isBailian(kind, id) {
  const it = (pool(kind) || {})[id] || {};
  const u = String(it.base_url || it.stt_url || it.endpoint || '');
  return /dashscope|maas\.aliyuncs\.com/i.test(u);
}

function modelLabel(kind, id) {
  if (!id || id === '__off__') return '';
  return (isBailian(kind, id) ? '百炼云 · ' : '') + id;
}

function renderOutput(blocks) {
  $('output').innerHTML = blocks.map(([head, text]) =>
    '<div class="outblock"><div class="outh">' + esc(head) +
    '</div><pre>' + esc(text) + '</pre></div>').join('');
}

// ---------------------------------------------------------------- 渲染

function renderTop() {
  const p = S.provider;
  $('personaSelect').innerHTML = options(Object.keys(S.persona.list), S.persona.active);
  const offOption = disabled => '<option value="__off__"' +
    (disabled ? ' selected' : '') + '>停用</option>';
  $('speechSelect').innerHTML = offOption(!p.active_ids.speech) +
    options(Object.keys(p.speech_providers), p.active_ids.speech,
            n => modelLabel('speech', n));
  $('modelSelect').innerHTML = offOption(!p.active_ids.llm) +
    options(Object.keys(p.llm_providers), p.active_ids.llm,
            n => modelLabel('llm', n));
  // ASR / LLM 各自独立着色：未就绪红、就绪绿、停用灰，不再整条一起变红
  const ks = $('keyState');
  const asrOk = !!(S.interview && S.interview.speech && S.interview.speech.ready);
  const asrId = p.active_ids.speech, llmId = p.active_ids.llm;
  const part = (label, cls, text, tip) =>
    '<span class="state-part ' + cls + '"' + (tip ? ' title="' + esc(tip) + '"' : '') +
    '>' + label + ' ' + text + '</span>';
  ks.innerHTML = part('ASR', asrId ? (asrOk ? 'ok' : 'bad') : 'off',
      asrId ? (asrOk ? '就绪' : '未就绪') : '停用',
      !asrId ? '该侧已停用' : (asrOk ? '' : (S.interview.speech.reason || '请检查配置'))) +
    '<span class="state-sep">/</span>' +
    part('LLM', llmId ? (S.llm_ready ? 'ok' : 'bad') : 'off',
      llmId ? (S.llm_ready ? '就绪' : '未就绪') : '停用',
      !llmId ? '该侧已停用' : (S.llm_ready ? '' : '密钥未配置或服务不可用'));
  $('buildBtn').disabled = !S.llm_ready;
  $('answerBtn').disabled = !S.llm_ready;

  const st = S.stealth || {}, ac = S.access || {};
  const badge = $('stealthState');
  let label = '', bad = false;
  if (ac.share_safe) {
    label = '共享模式 · 电脑无界面';
  } else if (ac.mode === 'none') {
    label = '服务无本机窗口 · 别在本机开页面';
  } else if (ac.mode === 'overlay') {
    const ok = (st.windows || []).some(w => w.own && w.capture_excluded);
    label = ok ? '提示窗 · 已启用系统捕获排除' : '提示窗 · 排除未生效';
    bad = !ok;
  } else {
    label = '浏览器窗口 · 共享有风险';
    bad = true;
  }
  badge.textContent = label;
  // 只切颜色：className 整体重写会抹掉 adv-only，简洁视图就藏不住隐身提示了
  badge.classList.toggle('bad', bad);
  badge.classList.toggle('good', !bad);
}

function renderKb() {
  const rows = S.kb.files.map(f =>
    '<tr><td class="fname" title="' + esc(f.name) + '">' + esc(f.name) +
    '</td><td class="fsize">' + fmtSize(f.size) +
    '</td><td><button class="tiny" data-del-kb="' + esc(f.name) + '">删除</button></td></tr>').join('');
  $('fileTable').querySelector('tbody').innerHTML = rows ||
    '<tr><td colspan="3" class="hint">暂无资料</td></tr>';
  $('kbStats').textContent = S.kb.stats.files + ' 个文件 / ' +
    S.kb.stats.chunks + ' 个片段 · 放入或修改文件后自动重建索引';
}

function renderPersonaList() {
  const rows = Object.keys(S.persona.list).map(n => {
    const info = S.persona.list[n];
    const active = n === S.persona.active;
    return '<tr><td><span class="pname' + (active ? ' active' : '') +
      '" data-use-persona="' + esc(n) + '">' + esc(n) + '</span>' +
      '<div class="hint">' + esc(info.label || '') +
      (info.has_prompt ? ' · 含手写提示词' : '') + '</div></td>' +
      '<td><button class="tiny" data-del-persona="' + esc(n) + '">删除</button></td></tr>';
  }).join('');
  $('personaTable').querySelector('tbody').innerHTML = rows;
}

function renderProvider() {
  const p = S.provider, snap = p.snapshot || {};
  const speech = snap.speech || {}, llm = snap.llm || {};
  $('providerInfo').textContent =
    '当前生效（两侧独立）　ASR ' + (p.active_ids.speech || '已停用') +
    ' · ' + (speech.type || '-') + ' · ' + (speech.model || '无模型') +
    '　/　LLM ' + (p.active_ids.llm || '已停用') + ' · ' + (llm.model || '-');
  const problems = S.provider.problems || [];
  $('providerProblems').innerHTML = problems.map(p =>
    '<div>' + esc(p) + '</div>').join('');
}

function renderStealth() {
  const st = S.stealth || {}, ac = S.access || {}, ui = S.ui || {};
  const caps = st.capabilities || {};
  $('uiDisplayName').value = ui.display_name || '';

  const modeText = {
    browser: '桌面窗口（Edge/Chrome）',
    overlay: '隐身提示窗（本进程）',
    none: '无本机窗口'
  }[ac.mode] || ac.mode;
  $('stealthNote').textContent = ac.share_safe ? st.note :
    '当前模式：' + modeText + '。' + (st.note || '') + ' ' +
    (caps.note || '') + ' 本机能力：' + (caps.flag || '无') + '。' +
    '共享屏幕时请使用手机模式。';

  const wins = st.windows || [];
  $('windowTable').querySelector('tbody').innerHTML = wins.length
    ? wins.map(w =>
        '<tr><td class="fname">' + esc(w.title || '(无标题)') + '</td>' +
        '<td class="fsize">' + (w.own ? '本进程' : '浏览器进程') + '</td>' +
        '<td class="fsize">' + (w.capture_excluded ? '已排除捕获' : '捕获可见') + '</td>' +
        '<td class="fsize">' + (w.toolwindow ? '不在任务栏' : '在任务栏') + '</td></tr>'
      ).join('')
    : '<tr><td colspan="4" class="hint">没有可管理的本机窗口</td></tr>';

  if (ac.lan) {
    $('accessHint').textContent =
      '当前允许局域网访问，非本机请求必须带令牌；令牌每次重启都会变，' +
      '重启后请重新取地址。手机与电脑需在同一局域网；列出多个地址时优先试 ' +
      '192.168.x.x，172.x / 10.x 常是虚拟网卡。首次绑定 0.0.0.0 时' +
      'Windows 防火墙可能弹授权框，请提前运行一次并允许。' +
      '请在第二台设备打开地址。';
  } else {
    $('accessHint').textContent =
      '当前只监听本机。共享时用 start-phone-only.vbs 启动后台服务。';
  }
  const urls = ac.lan_urls || (ac.lan_url ? [ac.lan_url] : []);
  $('accessUrl').innerHTML =
    (ac.local_url ? '<div>本机　' + esc(ac.local_url) + '</div>' : '') +
    (urls.length
      ? urls.map((u, i) =>
          '<div>' + (i === 0 ? '手机　' : '　　　') +
          '<a href="' + esc(u) + '">' + esc(u) + '</a></div>').join('')
      : '<div class="hint">未启用局域网访问</div>');
}

function fillForm() {
  const p = S.persona.current || {};
  $('f_name').value = S.persona.active;
  $('f_label').value = p.label || '';
  $('f_target_role').value = p.target_role || '';
  $('f_years').value = p.years || '';
  $('f_domain').value = p.domain || '';
  $('f_stack').value = lines(p.stack);
  $('f_highlights').value = lines(p.highlights);
  $('f_constraints').value = lines(p.constraints);
  $('f_style').value = p.style || '';
  $('f_custom_prompt').value = p.custom_prompt || '';
  $('f_examples').value = (p.examples || [])
    .map(e => (e.q || '') + ' | ' + (e.a || '')).join('\n');
}

function collect() {
  return {
    label: $('f_label').value,
    target_role: $('f_target_role').value,
    years: $('f_years').value,
    domain: $('f_domain').value,
    stack: $('f_stack').value,
    highlights: $('f_highlights').value,
    constraints: $('f_constraints').value,
    style: $('f_style').value,
    custom_prompt: $('f_custom_prompt').value,
    examples: $('f_examples').value
  };
}

// ---------------------------------------------------------------- 简洁视图

function applyMode() {
  document.body.classList.toggle('simple', simpleMode);
  const t = $('modeToggle');
  t.textContent = simpleMode ? '完整视图' : '简洁视图';
  t.title = simpleMode
    ? '显示人设、资料库、服务池、隐身等全部维护项'
    : '回到只保留常用项的简洁界面';
  // 完整视图 = 原来那套配置页，高级区块直接全部展开
  if (!simpleMode) $('advancedDetails').open = true;
}

function renderQuick() {
  const p = S.provider, secs = p.secrets || {};
  const sp = (S.interview && S.interview.speech) || {};
  const asrId = p.active_ids.speech, llmId = p.active_ids.llm;
  const asrSet = !!(asrId && secs['speech:' + asrId] && secs['speech:' + asrId].set);
  const llmSet = !!(llmId && secs['llm:' + llmId] && secs['llm:' + llmId].set);
  $('quickAsrName').textContent = asrId ? modelLabel('speech', asrId) : '已停用';
  $('quickAsrProto').textContent = (sp.label || sp.type || '') +
    (sp.model ? ' · ' + sp.model : '');
  $('quickAsrState').textContent = !asrId ? '停用'
    : (sp.ready ? '就绪 · 密钥' + (asrSet ? '已设置' : '未设置')
      : '未就绪 · ' + (sp.reason || '请检查配置'));
  $('quickLlmName').textContent = llmId ? modelLabel('llm', llmId) : '已停用';
  $('quickLlmModel').textContent = ((p.snapshot || {}).llm || {}).model || '';
  $('quickLlmState').textContent = !llmId ? '停用'
    : (S.llm_ready ? '就绪 · 密钥' + (llmSet ? '已设置' : '未设置')
      : '未就绪 · 密钥未配置');
  const problems = p.problems || [];
  $('quickHint').textContent = problems.length
    ? '当前问题：' + problems.join('；') : '';
}

// ---------------------------------------------------------------- 模型管理（简洁视图）
// 新增走「复制现有模型 / 空白模板」，复杂字段跟着模板走，页面只暴露常用参数；
// 完整字段仍在高级设置里编辑，两边共用 /api/provider/save 与 /api/provider/delete。

const MM_BLANK = {
  llm: [
    { id: 'qwen', label: '空白 · 百炼 Qwen', data: {
      type: 'openai_compatible',
      base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: 'qwen-plus', supports_json_mode: true } },
    { id: 'deepseek', label: '空白 · DeepSeek', data: {
      type: 'openai_compatible', base_url: 'https://api.deepseek.com',
      model: 'deepseek-chat', supports_json_mode: true } },
    { id: 'openai', label: '空白 · OpenAI 兼容 LLM', data: {
      type: 'openai_compatible', base_url: '', model: '',
      supports_json_mode: true } }
  ],
  speech: [
    { id: 'openai-stt', label: '空白 · OpenAI 兼容 ASR', data: {
      type: 'openai_compatible_stt', base_url: '', model: 'whisper-1' } },
    { id: 'generic', label: '空白 · 通用 HTTP ASR', data: {
      type: 'generic_http_stt', stt_url: '', stt_method: 'POST',
      stt_headers: '{}', stt_body: 'multipart', audio_field: 'file',
      stt_extra: '{}', stt_json_template: '{}', response_path: 'text',
      stt_lang_query: 'language' } },
    { id: 'azure', label: '空白 · Azure Speech', data: {
      type: 'azure_speech_translation', region: '', endpoint: '' } }
  ]
};

let mm = { open: false, mode: 'new', kind: 'llm', id: '', data: {}, autoId: true };

function mmEnv(kind, id) {
  return '${COPILOT_' + kind.toUpperCase() + '_' +
    id.replace(/[^A-Za-z0-9]/g, '_').toUpperCase() + '_KEY}';
}

// 复制现有模型：丢掉 api_key / id，其余参数（含请求体模板）原样带过来
function mmClone(src) {
  const out = {};
  Object.keys(src || {}).forEach(k => {
    if (k !== 'api_key' && k !== 'id') out[k] = src[k];
  });
  return out;
}

// Azure 用 region；填了完整 URL 则改走 stt_url；其余 ASR 与 LLM 各有固定地址字段
function mmAddrField(kind, type, value) {
  if (kind === 'llm') return 'base_url';
  if (type === 'azure_speech_translation')
    return /^https?:\/\//i.test(value || '') ? 'stt_url' : 'region';
  if (type === 'openai_compatible_stt') return 'base_url';
  return 'stt_url';
}

function mmAddrValue(item, kind) {
  return item.stt_url || item.base_url || item.endpoint || item.region || '';
}

function mmProtoLabel(kind, type) {
  if (kind === 'llm') return 'Base URL（到 /v1）';
  if (type === 'azure_speech_translation') return 'Region 或完整识别地址';
  if (type === 'openai_compatible_stt') return 'Base URL（自动补 /audio/transcriptions）';
  if (type === 'qwen_realtime_stt') return '百炼业务空间 WebSocket 地址（wss://）';
  return '完整识别地址 STT URL';
}

function mmSuggestId(kind, presetValue) {
  const base = String(presetValue || 'model')
    .replace(/^(?:copy|blank):/, '').replace(/[^A-Za-z0-9_.-]/g, '-');
  let id = base + '-new', i = 2;
  while (pool(kind)[id]) id = base + '-' + (i++);
  return id;
}

function mmPresets(kind) {
  const out = [];
  Object.keys(pool(kind)).forEach(id => out.push({
    value: 'copy:' + id, label: '复制 · ' + id, data: mmClone(pool(kind)[id])
  }));
  (MM_BLANK[kind] || []).forEach(t => out.push({
    value: 'blank:' + t.id, label: t.label, data: Object.assign({}, t.data)
  }));
  return out;
}

function mmApplyTemplate(kind, presetValue) {
  const list = mmPresets(kind);
  const found = list.find(x => x.value === presetValue) || list[0];
  mm.data = found ? Object.assign({}, found.data) : {};
  const type = mm.data.type || (kind === 'speech'
    ? 'openai_compatible_stt' : 'openai_compatible');
  if (kind === 'speech') $('mmProto').value = type;
  $('mmModel').value = mm.data.model || '';
  $('mmUrl').value = mmAddrValue(mm.data, kind);
  $('mmTemp').value = mm.data.temperature ?? '';
  $('mmMaxTokens').value = mm.data.max_tokens ?? '';
  $('mmTimeout').value = mm.data.timeout_s ?? '';
  $('mmJsonMode').checked = mm.data.supports_json_mode !== false;
  mmApplyFields();
  const copied = found && found.value.startsWith('copy:');
  $('mmEditorHint').textContent = (copied
    ? '已复制「' + found.value.slice(5) + '」的全部参数（含请求头 / 请求体模板），' +
      '改服务 ID 与模型名后保存即可；填新密钥会写入该 ID 专属的环境变量。'
    : '空白模板只带最小可用参数，其余到「高级设置」里补。') +
    '　数值参数留空＝用默认值 / 不修改。';
}

function mmApplyFields() {
  const kind = $('mmKind').value;
  const isLlm = kind === 'llm';
  document.querySelectorAll('.mm-form label.mm-llm').forEach(el => {
    el.hidden = !isLlm;
  });
  $('mmProtoLabel').hidden = isLlm;
  $('mmPresetLabel').hidden = mm.mode !== 'new';
  $('mmKind').disabled = mm.mode !== 'new';
  $('mmId').disabled = mm.mode !== 'new';
  $('mmUrlLabel').textContent = mmProtoLabel(kind, $('mmProto').value);
}

function openModelEditor(mode, kind, id) {
  mm = { open: true, mode: mode, kind: kind, id: id || '',
          data: {}, autoId: mode === 'new' };
  $('mmEditor').hidden = false;
  $('mmKind').value = kind;
  $('mmId').value = id || '';
  $('mmKey').value = '';
  $('mmEditorTitle').textContent = mode === 'new'
    ? '新增模型' : '编辑模型 · ' + id;
  if (mode === 'new') {
    const presets = mmPresets(kind);
    const active = S.provider.active_ids[kind];
    const prefer = presets.find(x => x.value === 'copy:' + active) || presets[0];
    $('mmPreset').innerHTML = presets.map(x =>
      '<option value="' + esc(x.value) + '">' + esc(x.label) + '</option>').join('');
    $('mmPreset').value = prefer ? prefer.value : '';
    $('mmId').value = mmSuggestId(kind, $('mmPreset').value);
    mmApplyTemplate(kind, $('mmPreset').value);
    mm.autoId = true;
  } else {
    const item = pool(kind)[id] || {};
    const type = item.type || (kind === 'speech'
      ? 'openai_compatible_stt' : 'openai_compatible');
    if (kind === 'speech') $('mmProto').value = type;
    $('mmModel').value = item.model || '';
    $('mmUrl').value = mmAddrValue(item, kind);
    $('mmTemp').value = item.temperature ?? '';
    $('mmMaxTokens').value = item.max_tokens ?? '';
    $('mmTimeout').value = item.timeout_s ?? '';
    $('mmJsonMode').checked = item.supports_json_mode !== false;
    $('mmEditorHint').textContent =
      '只修改这里的常用参数，请求头 / 请求体模板等保持不变；新密钥留空＝不修改。';
    mmApplyFields();
  }
}

function closeModelEditor() {
  mm.open = false;
  $('mmEditor').hidden = true;
}

function renderModels() {
  if (!S) return;
  ['speech', 'llm'].forEach(kind => {
    const root = $(kind === 'speech' ? 'mmAsrList' : 'mmLlmList');
    if (!root) return;
    const items = pool(kind), ids = Object.keys(items);
    const secs = S.provider.secrets || {};
    root.innerHTML = ids.map(id => {
      const it = items[id] || {};
      const active = S.provider.active_ids[kind] === id;
      // 被方案引用不算硬限制：删除时后端会把这些方案的该侧置为停用
      const locked = active || ids.length <= 1;
      const reason = active ? '正在生效，请先在「服务」中切换' : '至少保留一个服务';
      const sec = secs[kind + ':' + id] || {};
      const proto = kind === 'speech'
        ? ((S.provider.asr_types || {})[it.type] || {}).label || it.type || '' : '';
      const meta = [it.model || '', proto,
        sec.set ? '密钥已设置' : '密钥未设置'].filter(Boolean).join(' · ');
      return '<div class="mm-row' + (active ? ' mm-active' : '') +
        '" data-edit-model="' + esc(id) + '" data-kind="' + kind + '">' +
        '<button class="mm-main" type="button"><span class="mm-name">' + esc(modelLabel(kind, id)) +
        (active ? ' <span class="mm-tag">生效中</span>' : '') + '</span>' +
        '<span class="mm-meta">' + esc(meta) + '</span></button>' +
        '<button class="mm-del" data-del-model="' + esc(id) +
        '" data-kind="' + kind + '"' +
        (locked ? ' disabled title="' + esc(reason) + '">不可删'
          : ' title="删除">删除') + '</button></div>';
    }).join('') || '<p class="hint">暂无模型</p>';
  });
}

async function saveModel() {
  const kind = $('mmKind').value;
  const id = $('mmId').value.trim();
  const isNew = mm.mode === 'new';
  try {
    if (!/^[A-Za-z0-9_.-]+$/.test(id))
      throw new Error('服务 ID 只能用英文字母、数字、下划线、连字符和点号');
    if (isNew && pool(kind)[id])
      throw new Error('服务 ID「' + id + '」已存在，请换一个或直接点该行编辑');
    const type = kind === 'speech' ? $('mmProto').value : 'openai_compatible';
    const data = isNew ? Object.assign({}, mm.data) : {};
    const addr = $('mmUrl').value.trim();
    data.type = type;
    data.model = $('mmModel').value.trim();
    data[mmAddrField(kind, type, addr)] = addr;
    if (kind === 'llm') {
      [['mmTemp', 'temperature', 0, 2],
       ['mmMaxTokens', 'max_tokens', 1, 100000],
       ['mmTimeout', 'timeout_s', 1, 600]].forEach(([el, field, lo, hi]) => {
        const raw = $(el).value.trim();
        if (raw === '') { delete data[field]; return; }
        const n = Number(raw);
        if (!Number.isFinite(n) || n < lo || n > hi)
          throw new Error(field + ' 需为 ' + lo + '–' + hi + ' 的数字');
        data[field] = field === 'max_tokens' ? Math.round(n) : n;
      });
      data.supports_json_mode = $('mmJsonMode').checked;
    }
    const secret = $('mmKey').value.trim();
    if (isNew) {
      // 复制来的 ${...KEY} 占位要换成新 ID 的环境变量，避免和原服务共用一把 key
      const env = mmEnv(kind, id);
      Object.keys(data).forEach(k => {
        if (typeof data[k] === 'string')
          data[k] = data[k].replace(
            /\$\{COPILOT_(?:SPEECH|LLM)_[A-Z0-9_]+_KEY\}/g, env);
      });
      if (kind === 'speech' && type === 'generic_http_stt' && secret &&
          (!data.stt_headers || data.stt_headers === '{}'))
        data.stt_headers = JSON.stringify({ Authorization: 'Bearer ' + env });
    }
    await api('/api/provider/save', {
      kind: kind, id: id, data: data, secret: secret, persist_secret: true
    });
    await refresh(true);
    fillProviderForm(kind, id);
    closeModelEditor();
    status((isNew ? '已新增' : '已保存') + '模型「' + id + '」' +
      (secret ? ' · 密钥已写入 ' + mmEnv(kind, id) : ''));
  } catch (e) { status('保存模型失败：' + e.message, true); }
}

async function deleteModel(kind, id) {
  const ids = Object.keys(pool(kind));
  if (S.provider.active_ids[kind] === id) {
    status('模型「' + id + '」正在生效。请在「服务」中切换或停用后再删除。', true);
    return;
  }
  if (ids.length <= 1) { status('至少保留一个 ' + kind + ' 服务', true); return; }
  const usedBy = Object.keys(S.provider.profiles || {}).filter(n =>
    (S.provider.profiles[n] || {})[kind] === id);
  const msg = usedBy.length
    ? '删除模型「' + id + '」？方案 ' + usedBy.join('、') +
      ' 引用它，删除会把这些方案的这一侧设为「停用」，当前生效项不变。'
    : '删除模型「' + id + '」？此操作不可撤销。';
  if (!confirm(msg)) return;
  try {
    await api('/api/provider/delete', { kind: kind, id: id, drop_refs: true });
    await refresh(true);
    if (mm.open && mm.mode === 'edit' && mm.id === id) closeModelEditor();
    fillProviderForm(kind, S.provider.active_ids[kind] || Object.keys(pool(kind))[0]);
    status('已删除模型 ' + id + (usedBy.length ? '；相关方案该侧已停用' : ''));
  } catch (e) {
    status(String(e.message).indexOf('仍被方案引用') >= 0
      ? '后端是旧版本：重启服务后即可自动清理方案引用；或到「高级设置」先改方案。'
      : '删除失败：' + e.message, true);
  }
}

async function saveQuickKeys() {
  const p = S.provider;
  const jobs = [];
  [['speech', 'quickAsrKey', p.active_ids.speech],
   ['llm', 'quickLlmKey', p.active_ids.llm]].forEach(([kind, elId, id]) => {
    const secret = $(elId).value.trim();
    if (secret) jobs.push({ kind: kind, id: id, secret: secret, el: $(elId) });
  });
  if (!jobs.length) { status('两把密钥都留空，未做修改', true); return; }
  if (jobs.some(j => !j.id)) {
    status('请先在「服务」中选一个模型，再填写密钥。', true);
    return;
  }
  try {
    for (const j of jobs) {
      // data 传空对象：只更新密钥，不覆盖地址 / 模型 / 协议等已有配置
      await api('/api/provider/save', {
        kind: j.kind, id: j.id, data: {},
        secret: j.secret, persist_secret: true
      });
      j.el.value = '';
    }
    await refresh(true);
    status('密钥已保存并生效：' + jobs.map(j => j.id).join('、'));
  } catch (e) { status('保存密钥失败：' + e.message, true); }
}

// ---------------------------------------------------------------- 配置视图

function buildForm(kind) {
  const root = $(kind + 'Form');
  if (root.dataset.built) return;
  root.innerHTML = CFG_FIELDS[kind].map(([key, label, type, list, types, bodies]) => {
    const id = 'cfg_' + kind + '_' + key;
    let control;
    if (type === 'select') {
      control = '<select id="' + id + '">' + list.map(v =>
        '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('') + '</select>';
    } else if (type === 'check') {
      control = '<input type="checkbox" id="' + id + '">';
    } else if (type === 'textarea') {
      control = '<textarea id="' + id + '" rows="6" spellcheck="false" autocomplete="off"></textarea>';
    } else {
      control = '<input type="' + type + '" id="' + id + '" autocomplete="off">';
    }
    const wide = type === 'check' ? ''
      : ['base_url', 'note', 'secret', 'endpoint', 'stt_url',
         'stt_headers', 'stt_extra', 'stt_json_template'].includes(key) ? 'wide' : '';
    const cls = type === 'check' ? 'check' : wide;
    const show = types ? ' data-types="' + esc(types.join(' ')) + '"' : '';
    const byBody = bodies ? ' data-bodies="' + esc(bodies.join(' ')) + '"' : '';
    return '<label class="' + cls + '"' + show + byBody + ' data-field="' + key + '">' +
      esc(label) + control + '</label>';
  }).join('');
  root.dataset.built = '1';
  if (kind === 'speech') {
    cfgEl(kind, 'type').addEventListener('change', () => {
      applyAsrFieldVisibility();
      renderAsrTypeHint();
    });
    cfgEl(kind, 'stt_body').addEventListener('change', applyAsrFieldVisibility);
  }
}

// 按当前 ASR 协议 + 请求体格式只显示用得上的字段；切换不丢已填内容，只是隐藏。
function applyAsrFieldVisibility() {
  const type = cfgVal('speech', 'type');
  const body = cfgVal('speech', 'stt_body');
  document.querySelectorAll('#speechForm label[data-types]').forEach(el => {
    const byType = el.dataset.types.split(' ').includes(type);
    const bodies = el.dataset.bodies ? el.dataset.bodies.split(' ') : null;
    el.hidden = !byType || (bodies && !bodies.includes(body));
  });
}

function renderAsrTypeHint() {
  const el = $('asrTypeHint');
  if (!el || !S) return;
  const info = (S.provider.asr_types || {})[cfgVal('speech', 'type')] || {};
  const asr = S.provider.active_ids.speech || '已停用';
  const llm = S.provider.active_ids.llm || '已停用';
  el.textContent = info.label
    ? '当前编辑协议：' + info.label + '。鉴权：' + info.auth + '。' +
      (info.note || '') + (info.available === false ? '（当前版本尚不可用）' : '') +
      '　｜　当前生效 ASR：' + asr + '，LLM：' + llm +
      '；两侧由顶栏分别选择，互不影响。'
    : '';
}

function cfgEl(kind, key) { return $('cfg_' + kind + '_' + key); }

function cfgVal(kind, key) {
  const el = cfgEl(kind, key);
  if (!el) return '';
  return el.type === 'checkbox' ? el.checked : el.value;
}

function cfgSet(kind, key, value) {
  const el = cfgEl(kind, key);
  if (!el) return;
  if (el.type === 'checkbox') el.checked = Boolean(value);
  else el.value = value == null ? '' : String(value);
}

function pool(kind) {
  return kind === 'llm' ? S.provider.llm_providers : S.provider.speech_providers;
}

const CFG_KEYS = ['type', 'base_url', 'model', 'api_version', 'note',
  'region', 'endpoint', 'stt_url', 'device', 'stt_method', 'stt_headers',
  'stt_body', 'audio_field', 'stt_extra', 'stt_json_template',
  'response_path', 'stt_lang_query'];

function fillProviderForm(kind, id) {
  buildForm(kind);
  editing[kind] = id || null;
  const item = id ? (pool(kind)[id] || {}) : {};
  cfgSet(kind, 'id', id || '');
  CFG_KEYS.forEach(k => {
    if (cfgEl(kind, k)) cfgSet(kind, k, item[k] || '');
  });
  // 新建 ASR 服务默认非 Azure 协议，避免一上手就被 Azure 门户绑定
  if (!id && kind === 'speech') {
    cfgSet(kind, 'type', 'openai_compatible_stt');
    cfgSet(kind, 'stt_method', 'POST');
    cfgSet(kind, 'stt_body', 'multipart');
    cfgSet(kind, 'audio_field', 'file');
    cfgSet(kind, 'stt_lang_query', 'language');
    cfgSet(kind, 'response_path', 'text');
    cfgSet(kind, 'stt_headers', '{}');
    cfgSet(kind, 'stt_extra', '{}');
    cfgSet(kind, 'stt_json_template', '{}');
  }
  if (cfgEl(kind, 'supports_json_mode')) {
    cfgSet(kind, 'supports_json_mode', item.supports_json_mode !== false);
  }
  cfgSet(kind, 'secret', '');
  cfgSet(kind, 'persist_secret', true);
  if (kind === 'speech') {
    applyAsrFieldVisibility();
    renderAsrTypeHint();
  }
  const sec = (S.provider.secrets || {})[kind + ':' + id] || {};
  const keyState = id ? (sec.set ? '密钥已设置：' + (sec.env || '字面值') : '密钥未设置') : '';
  status(id ? '正在编辑 ' + kind + ' 服务「' + id + '」' +
    (keyState ? ' · ' + keyState : '') : '新建 ' + kind + ' 服务');
}

function renderProviderTable(kind) {
  const items = pool(kind);
  const active = S.provider.active_ids[kind];
  const secs = S.provider.secrets || {};
  const rows = Object.keys(items).map(id => {
    const p = items[id];
    const proto = kind === 'speech'
      ? ((S.provider.asr_types || {})[p.type] || {}).label || p.type || '' : '';
    const desc = kind === 'llm'
      ? (p.model || '-') + ' · ' + (p.base_url || '-')
      : proto + ' · ' +
        (p.stt_url || p.base_url || p.region || p.endpoint || p.model || '-');
    const sec = secs[kind + ':' + id] || {};
    const keyMark = sec.set ? (sec.inline ? '字面值' : sec.env) : '未设置';
    return '<tr><td><span class="pname' + (id === active ? ' active' : '') +
      '" data-edit="' + esc(id) + '">' + esc(id) + '</span>' +
      '<div class="hint">' + esc(p.type || '') + ' · ' + esc(desc) + '</div></td>' +
      '<td class="fsize">' + esc(keyMark) + (id === active ? '<br>当前生效' : '') +
      '</td><td><button class="tiny" data-del-provider="' + esc(id) +
      '">删除</button></td></tr>';
  }).join('');
  $(kind + 'Table').querySelector('tbody').innerHTML = rows;
}

function fillProfileForm(name) {
  const p = S.provider;
  const key = name || p.active;
  const item = p.profiles[key] || {};
  $('profileSelect').value = key;
  $('profileName').value = key;
  $('profileSpeech').value = item.speech === undefined
    ? (Object.keys(p.speech_providers)[0] || '') : (item.speech || '__off__');
  $('profileLlm').value = item.llm === undefined
    ? (Object.keys(p.llm_providers)[0] || '') : (item.llm || '__off__');
  $('profileDesc').value = item.description || '';
}

function renderConfig() {
  const p = S.provider;
  const offOpt = '<option value="__off__">停用（该侧不启用）</option>';
  $('profileSelect').innerHTML = options(Object.keys(p.profiles), p.active);
  $('profileSpeech').innerHTML = offOpt + options(Object.keys(p.speech_providers));
  $('profileLlm').innerHTML = offOpt + options(Object.keys(p.llm_providers));
  fillProfileForm();
  renderProviderTable('llm');
  renderProviderTable('speech');
  buildForm('llm');
  buildForm('speech');
  const sp = (S.interview && S.interview.speech) || {};
  $('speechUrlHint').textContent =
    '当前生效 ASR：' + (p.active_ids.speech || '已停用') +
    '　协议：' + (sp.label || sp.type || '—') +
    '　模型：' + (sp.model || '—') +
    '　LLM：' + (p.active_ids.llm || '已停用') +
    '　实际识别地址：' + (sp.url || '（未就绪）') + '。' +
    '请在「服务」菜单切换语音识别。模型名会覆盖请求体模板里的 model，' +
    '改一次即生效。嵌套 JSON 请求体（如百炼 qwen3-asr-flash）把「请求体格式」设为 ' +
    'json_template，或直接选 bailian-qwen-asr 预设；查询串里的 key / token 会自动打码。' +
    (sp.reason ? ' 当前问题：' + sp.reason : '');
  if (!configReady) {
    fillProviderForm('llm', p.active_ids.llm || Object.keys(p.llm_providers)[0]);
    fillProviderForm('speech', p.active_ids.speech || Object.keys(p.speech_providers)[0]);
    configReady = true;
  }
  renderAsrTypeHint();
  const missing = Object.keys(p.secrets || {}).filter(k => !p.secrets[k].set);
  $('secretHint').textContent =
    '密钥不回显；providers.json 只保存 ${ENV_VAR} 占位。页面填入的密钥默认写入 ' +
    p.dotenv + '（已被 .gitignore 排除），取消勾选则只对本次进程有效。当前 .env ' +
    (p.dotenv_exists ? '存在' : '不存在') + '；未设置密钥的服务：' +
    (missing.length ? missing.join('、') : '无') + '。';
  const preset = p.profiles[p.active] || {};
  const differs = p.active_ids.llm !== preset.llm ||
    p.active_ids.speech !== preset.speech;
  $('profileHint').textContent =
    '两侧独立生效：顶栏改语音或模型只写当前生效项，不改动这里的方案定义；' +
    '选中方案则一次性把两侧一起套用。任一侧都可单独「停用」。' +
    (differs ? ' 当前生效项与「' + p.active + '」不一致，是独立切换后的正常状态，' +
      '重选一次该方案即可重新套用。' : '');
  renderQuick();
  renderModels();
}

function switchView(which) {
  currentView = which;
  const isIv = which === 'interview';
  const main = document.querySelector('main.layout');
  if (main) main.hidden = isIv;
  $('interviewView').hidden = !isIv;
  $('personaView').hidden = which !== 'persona';
  $('configView').hidden = which !== 'config';
  const views = { viewInterview: 'interview', viewPersona: 'persona', viewConfig: 'config', viewSetup: 'config' };
  for (const [id, view] of Object.entries(views)) {
    $(id).classList.toggle('active', which === view);
    if (which === view) $(id).setAttribute('aria-current', 'page');
    else $(id).removeAttribute('aria-current');
  }
  $('serviceMenu').open = false;
  $('quickView').hidden = !simpleMode || which !== 'config';
  if (window.InterviewView) window.InterviewView.onShow(isIv);
}

async function saveProvider(kind) {
  buildForm(kind);
  const id = String(cfgVal(kind, 'id') || '').trim();
  if (!id) { status('请先填服务 ID', true); return; }
  const data = {};
  CFG_KEYS.forEach(k => {
    if (cfgEl(kind, k)) data[k] = cfgVal(kind, k);
  });
  if (cfgEl(kind, 'supports_json_mode')) {
    data.supports_json_mode = cfgVal(kind, 'supports_json_mode');
  }
  try {
    const r = await api('/api/provider/save', {
      kind: kind, id: id, data: data,
      secret: cfgVal(kind, 'secret'),
      persist_secret: cfgVal(kind, 'persist_secret')
    });
    await refresh(true);
    fillProviderForm(kind, id);
    status('已保存 ' + kind + ' 服务「' + id + '」' +
      (r.provider.api_key ? ' · 密钥来源 ' + r.provider.api_key : ''));
  } catch (e) { status('保存失败：' + e.message, true); }
}

async function deleteProvider(kind, id) {
  if (!id) { status('请先在列表里点选要删除的服务', true); return; }
  if (!confirm('删除 ' + kind + ' 服务「' + id + '」？被方案引用时会拒绝。')) return;
  try {
    await api('/api/provider/delete', { kind: kind, id: id });
    await refresh(true);
    fillProviderForm(kind, S.provider.active_ids[kind]);
    status('已删除服务 ' + id);
  } catch (e) { status(e.message, true); }
}

async function refresh(keepForm) {
  try {
    S = await api('/api/state');
    renderTop();
    renderKb();
    renderPersonaList();
    renderProvider();
    renderStealth();
    renderConfig();
    if (!keepForm) fillForm();
    if (window.InterviewView) window.InterviewView.setState(S);
  } catch (e) {
    status('加载状态失败：' + e.message, true);
  }
}

// ---------------------------------------------------------------- 资料上传

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(new Error('读取失败：' + file.name));
    reader.readAsDataURL(file);
  });
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length || !S) return;
  const allowed = S.kb.supported || [];
  const limit = (S.kb.max_upload_mb || 6) * 1024 * 1024;
  let done = 0;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    const ext = '.' + f.name.split('.').pop().toLowerCase();
    if (!allowed.includes(ext)) {
      status('跳过不支持的格式：' + f.name + '（PDF 请先另存为 txt / docx）', true);
      continue;
    }
    if (f.size > limit) {
      status('跳过超过大小限制：' + f.name, true);
      continue;
    }
    status('上传 ' + (i + 1) + '/' + files.length + '：' + f.name);
    try {
      const b64 = await readAsBase64(f);
      await api('/api/kb/upload', { filename: f.name, content_b64: b64 });
      done++;
    } catch (e) {
      status('上传失败：' + f.name + ' — ' + e.message, true);
      return;
    }
  }
  await refresh(true);
  status('已写入 ' + done + ' 个文件，索引已重建（' + S.kb.stats.files +
    ' 个文件 / ' + S.kb.stats.chunks + ' 个片段）');
}

// ---------------------------------------------------------------- 事件

$('viewInterview').addEventListener('click', () => switchView('interview'));
$('viewPersona').addEventListener('click', () => switchView('persona'));
$('viewConfig').addEventListener('click', () => switchView('config'));
$('viewSetup').addEventListener('click', () => switchView('config'));
$('ivSetupLink').addEventListener('click', () => switchView('config'));

for (const id of ['serviceMenu', 'ivRecordMenu']) {
  const menu = $(id);
  document.addEventListener('click', event => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  menu.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !menu.open) return;
    menu.open = false;
    menu.querySelector('summary').focus();
  });
}

function setMode(simple) {
  simpleMode = simple;
  try { localStorage.setItem('copilot_simple_mode', simple ? 'simple' : 'full'); }
  catch (e) { /* 隐私模式下不持久化，本次会话仍生效 */ }
  $('advancedDetails').open = !simple;
  applyMode();
  if (simple && currentView === 'persona') switchView('interview');
  else if (currentView === 'config') switchView('config');
  status(simple ? '已切到简洁视图' : '已切到完整视图');
}
$('modeToggle').addEventListener('click', () => setMode(!simpleMode));
$('quickSaveBtn').addEventListener('click', saveQuickKeys);
$('mmNewBtn').addEventListener('click', () => openModelEditor('new', 'llm', ''));
$('mmCancelBtn').addEventListener('click', closeModelEditor);
$('mmSaveBtn').addEventListener('click', saveModel);
$('mmId').addEventListener('input', () => { mm.autoId = false; });
$('mmKind').addEventListener('change', () => {
  if (mm.mode !== 'new') return;
  const kind = $('mmKind').value;
  $('mmPreset').innerHTML = mmPresets(kind).map(x =>
    '<option value="' + esc(x.value) + '">' + esc(x.label) + '</option>').join('');
  if (mm.autoId) $('mmId').value = mmSuggestId(kind, $('mmPreset').value);
  mmApplyTemplate(kind, $('mmPreset').value);
});
$('mmPreset').addEventListener('change', () => {
  if (mm.autoId)
    $('mmId').value = mmSuggestId($('mmKind').value, $('mmPreset').value);
  mmApplyTemplate($('mmKind').value, $('mmPreset').value);
});
$('mmProto').addEventListener('change', () => {
  mmApplyFields();
  if (mm.mode === 'edit') $('mmEditorHint').textContent =
    '已改协议：请到「高级设置」核对请求头 / 请求体 / 响应路径是否匹配新协议。';
});
['mmAsrList', 'mmLlmList'].forEach(listId => {
  $(listId).addEventListener('click', e => {
    const kind = listId === 'mmAsrList' ? 'speech' : 'llm';
    const del = e.target.getAttribute('data-del-model');
    if (del) { e.stopPropagation(); deleteModel(kind, del); return; }
    const row = e.target.closest('.mm-row');
    const id = row && row.getAttribute('data-edit-model');
    if (id) openModelEditor('edit', kind, id);
  });
});
$('quickAdvancedBtn').addEventListener('click', () => {
  setMode(false);
  switchView('persona');
  status('完整视图 · 可编辑人设与资料库');
});

$('dropZone').addEventListener('dragover', e => {
  e.preventDefault();
  $('dropZone').classList.add('over');
});
$('dropZone').addEventListener('dragleave', () => $('dropZone').classList.remove('over'));
$('dropZone').addEventListener('drop', e => {
  e.preventDefault();
  $('dropZone').classList.remove('over');
  uploadFiles(e.dataTransfer.files);
});
$('pickBtn').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', e => {
  uploadFiles(e.target.files);
  e.target.value = '';
});

$('fileTable').addEventListener('click', async e => {
  const name = e.target.getAttribute('data-del-kb');
  if (!name) return;
  if (!confirm('删除资料 ' + name + '？')) return;
  try {
    await api('/api/kb/delete', { filename: name });
    await refresh(true);
    status('已删除资料 ' + name);
  } catch (err) { status(err.message, true); }
});

$('personaTable').addEventListener('click', async e => {
  const use = e.target.getAttribute('data-use-persona');
  const del = e.target.getAttribute('data-del-persona');
  if (use && use !== S.persona.active) {
    try {
      await api('/api/persona/use', { name: use });
      await refresh();
      status('已切换到「' + use + '」');
    } catch (err) { status(err.message, true); }
  }
  if (del) {
    if (!confirm('删除人设「' + del + '」？')) return;
    try {
      await api('/api/persona/delete', { name: del });
      await refresh();
      status('已删除人设「' + del + '」');
    } catch (err) { status(err.message, true); }
  }
});

$('personaSelect').addEventListener('change', async e => {
  try {
    await api('/api/persona/use', { name: e.target.value });
    await refresh();
    status('已切换到「' + e.target.value + '」');
  } catch (err) { status(err.message, true); }
});

async function activate(kind, id, label) {
  try {
    await api('/api/provider/activate', { kind: kind, id: id });
    await refresh(true);
    status(label + '已设为 ' + (id === '__off__' ? '停用' : id) +
      '；另一侧不受影响，方案定义未改动');
  } catch (e) { status(e.message, true); }
}
$('speechSelect').addEventListener('change', e => activate('speech', e.target.value, '语音'));
$('modelSelect').addEventListener('change', e => activate('llm', e.target.value, '模型'));

$('profileSelect').addEventListener('change', async e => {
  try {
    await api('/api/provider/use', { profile: e.target.value });
    await refresh(true);
    fillProfileForm(e.target.value);
    status('已套用预设「' + e.target.value + '」到语音与模型两侧；之后仍可各自单独切换');
  } catch (err) { status(err.message, true); }
});

$('profileSaveBtn').addEventListener('click', async () => {
  const name = $('profileName').value.trim();
  if (!name) { status('请填方案名', true); return; }
  try {
    await api('/api/profile/save', {
      name: name, speech: $('profileSpeech').value,
      llm: $('profileLlm').value, description: $('profileDesc').value
    });
    await refresh(true);
    status('已保存并启用方案「' + name + '」');
  } catch (e) { status('保存方案失败：' + e.message, true); }
});

$('profileDeleteBtn').addEventListener('click', async () => {
  const name = $('profileSelect').value;
  if (!confirm('删除方案「' + name + '」？')) return;
  try {
    await api('/api/profile/delete', { name: name });
    await refresh(true);
    status('已删除方案「' + name + '」');
  } catch (e) { status(e.message, true); }
});

['llm', 'speech'].forEach(kind => {
  $(kind + 'Table').addEventListener('click', async e => {
    const edit = e.target.getAttribute('data-edit');
    const del = e.target.getAttribute('data-del-provider');
    if (edit) fillProviderForm(kind, edit);
    if (del) await deleteProvider(kind, del);
  });
  $(kind + 'SaveBtn').addEventListener('click', () => saveProvider(kind));
  $(kind + 'NewBtn').addEventListener('click', () => fillProviderForm(kind, null));
  $(kind + 'DeleteBtn').addEventListener('click', () => deleteProvider(kind, editing[kind]));
});

$('uiSaveBtn').addEventListener('click', async () => {
  const name = $('uiDisplayName').value.trim();
  if (!name) { status('显示名不能为空', true); return; }
  try {
    await api('/api/ui/save', { display_name: name });
    document.title = name;
    $('brand').textContent = name;
    await refresh(true);
    status('显示名已改为「' + name + '」，已开窗口标题同步更新');
  } catch (e) { status('保存失败：' + e.message, true); }
});

$('stealthApplyBtn').addEventListener('click', async () => {
  try {
    const r = await api('/api/stealth/apply', {});
    await refresh(true);
    const n = (r.result && r.result.excluded) || 0;
    status(n ? '已对 ' + n + ' 个窗口设置捕获排除' :
      '已重设标题与任务栏隐藏；浏览器窗口无法排除捕获，请切到 overlay / none 模式');
  } catch (e) { status('应用失败：' + e.message, true); }
});

$('stealthHideBtn').addEventListener('click', async () => {
  if (!confirm('隐藏后本机所有相关窗口都会消失，包括你正在看的这个页面。' +
      '请从手机访问地址点「恢复本机窗口」，或退出服务。继续？')) return;
  try {
    await api('/api/stealth/hide', {});
    status('已隐藏本机窗口');
  } catch (e) { status('隐藏失败：' + e.message, true); }
});

$('stealthShowBtn').addEventListener('click', async () => {
  try {
    const r = await api('/api/stealth/show', {});
    await refresh(true);
    status('已恢复 ' + r.windows + ' 个窗口');
  } catch (e) { status('恢复失败：' + e.message, true); }
});

$('shutdownBtn').addEventListener('click', async () => {
  if (!confirm('退出本地服务？提示窗与手机页面都会断开。')) return;
  try {
    await api('/api/shutdown', {});
    status('服务已退出，可以关闭本页');
  } catch (e) { status(e.message, true); }
});

$('refreshBtn').addEventListener('click', async () => {
  await refresh();
  status('已重新加载，人设表单恢复为已保存内容');
});

$('saveBtn').addEventListener('click', async () => {
  const name = $('f_name').value.trim();
  if (!name) { status('请填人设名', true); return; }
  try {
    await api('/api/persona/save', { name: name, data: collect() });
    await refresh();
    status('已保存并切换到「' + name + '」；改名保存等于另存一套');
  } catch (e) { status('保存失败：' + e.message, true); }
});

$('newPersonaBtn').addEventListener('click', async () => {
  const name = $('newPersonaName').value.trim();
  if (!name) { status('请填新人设名', true); return; }
  try {
    await api('/api/persona/save', { name: name, data: collect() });
    await refresh();
    $('newPersonaName').value = '';
    status('已用当前表单内容另存为「' + name + '」');
  } catch (e) { status('另存失败：' + e.message, true); }
});

$('deletePersonaBtn').addEventListener('click', async () => {
  const name = S.persona.active;
  if (!confirm('删除当前人设「' + name + '」？')) return;
  try {
    await api('/api/persona/delete', { name: name });
    await refresh();
    status('已删除「' + name + '」');
  } catch (e) { status(e.message, true); }
});

$('buildBtn').addEventListener('click', async () => {
  if (!S.kb.stats.files) { status('请先拖入至少一份资料', true); return; }
  const name = $('f_name').value.trim() || S.persona.active;
  if (!confirm('将用 ' + S.kb.stats.files +
      ' 个资料文件 + 手写提示词重写「' + name +
      '」的事实字段，边界、风格、提示词与范例保持不变。继续？')) return;
  status('调用 LLM 生成人设…');
  try {
    const r = await api('/api/persona/build', {
      name: name, prompt: $('f_custom_prompt').value
    });
    await refresh();
    status('已从资料生成人设「' + r.name + '」');
  } catch (e) { status('生成失败：' + e.message, true); }
});

$('previewBtn').addEventListener('click', async () => {
  const q = $('question').value.trim();
  if (!q) { status('请输入面试官的问题', true); return; }
  try {
    const r = await api('/api/persona/preview', {
      question: q, lang: $('lang').value, name: S.persona.active
    });
    renderOutput(r.messages.map((m, i) => [
      (i === 0 ? 'system' : 'user') + ' · ' + m.content.length + ' 字', m.content
    ]));
    status('已生成完整 prompt（不调用模型）；基于已保存的人设，修改后请先保存');
  } catch (e) { status('预览失败：' + e.message, true); }
});

$('answerBtn').addEventListener('click', async () => {
  const q = $('question').value.trim();
  if (!q) { status('请输入面试官的问题', true); return; }
  const lang = $('lang').value;
  status('生成回答中…');
  try {
    const r = await api('/api/persona/answer', { question: q, lang: lang });
    const a = r.answer;
    const blocks = [];
    if (lang === 'en') {
      blocks.push(['中文译文', a.translation_zh || '（空）']);
      blocks.push(['English answer', a.answer_en || '（空）']);
    } else {
      blocks.push(['中文回答', a.answer_zh || '（空）']);
    }
    if (a.degraded) blocks.push(['注意', '模型未返回标准 JSON，已按字段提取正文']);
    renderOutput(blocks);
    status('完成');
  } catch (e) { status('生成失败：' + e.message, true); }
});

refresh().then(() => {
  applyMode();
  $('advancedDetails').open = !simpleMode;
  // 简洁视图默认进面试页；完整视图保持原来的人设页。手机端 ?view=interview 优先。
  const qv = new URLSearchParams(location.search).get('view');
  const forced = qv === 'interview';
  if (simpleMode && qv === 'config') switchView('config');
  else switchView(forced || simpleMode ? 'interview' : 'persona');
  status('就绪 · 数据目录 ' + S.home);
});
