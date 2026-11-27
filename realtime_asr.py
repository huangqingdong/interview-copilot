"""在本机服务端连接百炼实时 ASR，浏览器只交换 PCM 和转写事件。"""

import base64
import binascii
import json
import re
import threading
import time
import uuid

from interview import InterviewError, detect_lang, realtime_stt_url

PCM_LIMIT = 64000
EVENT_PREFIX = "conversation.item.input_audio_transcription."


def _connect(*args, **kwargs):
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise InterviewError("请先安装实时 ASR 依赖：python -m pip install -r requirements.txt") from exc
    return connect(*args, **kwargs)


def _service_error(event):
    error = event.get("error")
    code = str(error.get("code", "")) if isinstance(error, dict) else ""
    return "百炼实时 ASR 返回错误" + (f"（{code}）" if re.fullmatch(r"[\w.-]{1,80}", code) else "")


class RealtimeASR:
    def __init__(self, speech, language="auto", silence_ms=500):
        self.id = uuid.uuid4().hex
        self.session_id = None
        self.preview = None
        self.events = []
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.last_audio = time.monotonic()
        self.finalized = False
        self.finishing = False
        self.canceled = False
        language = {"zh-CN": "zh", "en-US": "en"}.get(language)
        try:
            silence_ms = int(silence_ms)
        except (TypeError, ValueError) as exc:
            raise InterviewError("断句时长须为整数") from exc
        if not 200 <= silence_ms <= 6000:
            raise InterviewError("断句时长须在 200–6000 ms 之间")
        url = realtime_stt_url(speech)
        try:
            self.socket = _connect(
                url, additional_headers={
                    "Authorization": "Bearer " + speech["api_key"],
                    "OpenAI-Beta": "realtime=v1",
                }, open_timeout=6, close_timeout=1, max_size=256000)
            self._send("session.update", session={
                "modalities": ["text"], "input_audio_format": "pcm", "sample_rate": 16000,
                "input_audio_transcription": {"language": language} if language else {},
                "turn_detection": {
                    "type": "server_vad", "threshold": 0.2, "silence_duration_ms": silence_ms,
                },
            })
            deadline = time.monotonic() + 6
            while True:
                event = json.loads(self.socket.recv(timeout=max(0, deadline - time.monotonic())))
                if event.get("type") == "session.updated":
                    break
                if event.get("type") == "error":
                    raise InterviewError(_service_error(event))
                if time.monotonic() >= deadline:
                    raise TimeoutError()
        except Exception as exc:
            if hasattr(self, "socket"):
                self.socket.close()
            if isinstance(exc, InterviewError):
                raise
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            raise InterviewError(f"百炼实时 ASR 连接失败（{detail}），请检查地址、密钥和网络") from exc
        self.last_audio = time.monotonic()
        threading.Thread(target=self._receive, daemon=True).start()

    def _send(self, kind, **data):
        self.socket.send(json.dumps({"type": kind, "event_id": "event_" + uuid.uuid4().hex, **data}))

    def _publish(self, event):
        with self.lock:
            kind = event["type"]
            if kind in ("partial", "final"):
                self.preview = event
            if kind == "partial" and self.events and self.events[-1]["type"] == "partial":
                self.events[-1] = event
            else:
                self.events.append(event)
            if len(self.events) > 256:
                raise InterviewError("实时 ASR 事件积压，连接已停止")

    def _receive(self):
        stopped = {}
        languages = {}
        finalized = set()
        try:
            while not self.canceled:
                try:
                    event = json.loads(self.socket.recv(timeout=1))
                except TimeoutError:
                    if not self.finishing and time.monotonic() - self.last_audio > 20:
                        raise InterviewError("实时 ASR 音频已中断")
                    continue
                kind = event.get("type")
                item = event.get("item_id")
                if kind == "input_audio_buffer.speech_stopped":
                    stopped[item] = time.monotonic()
                elif kind == EVENT_PREFIX + "text" and item not in finalized:
                    languages[item] = event.get("language")
                    text = (event.get("text") or "") + (event.get("stash") or "")
                    self._publish({"type": "partial", "text": text})
                elif kind == EVENT_PREFIX + "completed" and item not in finalized:
                    if item:
                        finalized.add(item)
                    text = (event.get("transcript") or "").strip()
                    lang = event.get("language") or languages.pop(item, None)
                    languages.pop(item, None)
                    ended = stopped.pop(item, None)
                    if text:
                        self._publish({
                            "type": "final", "text": text,
                            "lang": "zh" if lang in ("zh", "yue") else "en" if lang == "en" else detect_lang(text),
                            "latency_ms": round((time.monotonic() - ended) * 1000) if ended is not None else None,
                        })
                elif kind == EVENT_PREFIX + "failed":
                    stopped.pop(item, None)
                    languages.pop(item, None)
                    self._publish({"type": "notice", "message": _service_error(event)})
                elif kind == "session.finished":
                    self.finalized = True
                    return
                elif kind == "error":
                    raise InterviewError(_service_error(event))
        except Exception as exc:
            if not self.canceled:
                message = str(exc) if isinstance(exc, InterviewError) else "百炼实时 ASR 连接已中断"
                with self.lock:
                    self.events.append({"type": "error", "message": message})
        finally:
            self.done.set()
            self.socket.close()

    def _take_events(self):
        with self.lock:
            events, self.events = self.events, []
            return {"events": events, "closed": self.done.is_set()}

    def push(self, audio):
        if not isinstance(audio, str) or len(audio) > ((PCM_LIMIT + 2) // 3) * 4:
            raise InterviewError("实时音频块过大或格式无效")
        try:
            raw = base64.b64decode(audio, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise InterviewError("实时音频不是有效的 Base64") from exc
        if not raw or len(raw) > PCM_LIMIT or len(raw) % 2:
            raise InterviewError("实时音频需要 16 位 PCM")
        with self.send_lock:
            if not (self.finishing or self.canceled or self.done.is_set()):
                try:
                    self._send("input_audio_buffer.append", audio=audio)
                    self.last_audio = time.monotonic()
                except Exception:
                    self.done.wait(timeout=1)
                    result = self._take_events()
                    result["closed"] = True
                    return result
        return self._take_events()

    def close(self):
        self.canceled = True
        self.socket.close()

    def finish(self):
        with self.send_lock:
            if not (self.finishing or self.canceled or self.done.is_set()):
                self.finishing = True
                try:
                    self._send("session.finish")
                except Exception:
                    pass
        if not self.done.wait(timeout=6):
            self.close()
        result = self._take_events()
        result["finalized"] = self.finalized
        return result
