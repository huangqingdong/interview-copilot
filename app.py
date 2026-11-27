"""Interview Copilot 本地应用服务。

运行模式（--window）：
    browser  默认。Edge/Chrome --app 窗口，适合共享前配置；该窗口属于外部进程，
             Windows 不允许跨进程设置捕获排除，屏幕共享时仍可能被拍到。
    overlay  自进程 tkinter 隐身提示窗，可设 WDA_EXCLUDEFROMCAPTURE：
             部分系统捕获方式支持排除，无法覆盖所有会议软件。
    none     完全不开本机窗口，界面用手机 / 第二台设备访问。最彻底。

    python app.py                            # 打开桌面窗口
    python app.py --share-safe --silent      # 共享时：本机零窗口，手机访问
    python app.py --browser                  # 用默认浏览器打开配置界面

--share-safe 显式启用后台采集，仅向持有令牌的其他局域网设备提供界面。
数据目录默认是脚本所在目录，可用 COPILOT_HOME 改（自检用）。
"""
from __future__ import annotations

import argparse
import atexit
import base64
import binascii
import hmac
import html
import importlib.util
from ipaddress import ip_address
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from http.client import HTTPConnection, HTTPException

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
sys.path.insert(0, str(ROOT))

import headless_audio
import interview as interview_module
import knowledge_base
import overlay as overlay_module
import persona
import realtime_asr
import stealth
from provider_config import ConfigError, ProviderConfig, public_provider

HOME = Path(os.environ.get("COPILOT_HOME") or ROOT).resolve()
MAX_UPLOAD = 6 * 1024 * 1024
MAX_BODY = 10 * 1024 * 1024

WRITE_ROUTES = {
    "/api/kb/upload", "/api/kb/delete",
    "/api/persona/save", "/api/persona/use", "/api/persona/delete",
    "/api/persona/build",
    "/api/provider/use", "/api/provider/save", "/api/provider/delete",
    "/api/provider/activate", "/api/profile/save", "/api/profile/delete",
    "/api/stealth/apply", "/api/stealth/hide", "/api/stealth/show",
    "/api/ui/save", "/api/shutdown",
}

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/interview.js": ("interview.js", "text/javascript; charset=utf-8"),
}

STATIC_PUBLIC = {"/style.css", "/app.js", "/interview.js", "/favicon.ico"}
DEFAULT_SETTINGS = {"display_name": stealth.DEFAULT_DISPLAY_NAME,
                    "mask_browser_window": True}


class AppError(Exception):
    pass


def public_providers(pool):
    """页面用：不返回明文密钥；${ENV} 占位原样显示，便于看清密钥来源。"""
    return {name: public_provider(item) for name, item in pool.items()}


# ---------------------------------------------------------------- 数据上下文

class AppContext:
    """把配置层、人设层、资料库与运行状态绑定到同一个数据目录。"""

    def __init__(self, home=HOME):
        self.home = Path(home)
        self.config_dir = self.home / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.kb_dir = self.home / "kb"
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = ProviderConfig(path=self.config_dir / "providers.json")
        self.store = persona.PersonaStore(
            path=self.config_dir / "persona.json",
            example=ROOT / "config" / "persona.example.json")
        self.kb = knowledge_base.KnowledgeBase(self.kb_dir)
        self.session = interview_module.Session(self.home)
        self.lock = threading.RLock()
        self.realtime = None
        self.realtime_open = threading.Lock()
        self.share_safe = False
        self.capture = None
        self.capture_action = threading.Lock()

        self.settings_path = self.config_dir / "app.json"
        self.settings = self._load_settings()
        self.display_name = self.settings["display_name"]
        self.last_answer = None
        self.shield = stealth.WindowShield()
        self.browser_pid = None
        self.mode = "browser"
        self.lan = False
        self.token = ""
        self.port = 8765
        self.lan_ips = []
        self.allowed_hosts = {"127.0.0.1", "localhost", "::1"}
        self.shutdown = lambda: None

    def submit_turn(self, data):
        ctx = self
        text = str(data.get("text") or "").strip()
        if not text:
            raise AppError("本轮没有识别到内容")
        lang = "zh" if str(data.get("lang") or "en").startswith("zh") else "en"
        # 「翻译 / 回答」是唯一输出开关；模式只决定结束时是否落盘。
        want_translation = bool(data.get("translate", False))
        want_answers = bool(data.get("answers", False))
        fields = persona.turn_fields(lang, want_translation, want_answers)
        with ctx.lock:
            session = ctx.session.data
            session_id = data.get("session_id")
            if session_id is not None and (
                    session_id != session["id"] or not session["active"]):
                raise AppError("会话已结束或切换")
            history = ctx.session.history() if want_answers else None
        skip_reason = ""
        generation_error = ""
        generation_ms = None
        result = {"translation_zh": "", "answer_zh": "", "answer_en": "",
                  "degraded": False}
        if fields:
            started = time.perf_counter()
            try:
                llm = ctx.cfg.llm
                if not all(llm.get(key) for key in ("base_url", "api_key", "model")):
                    raise ConfigError("LLM 未就绪：请检查服务地址、模型与密钥")
                if want_answers:
                    result = persona.generate_turn(
                        text, cfg=ctx.cfg, store=ctx.store, history=history,
                        source_lang=lang, kb=ctx.kb,
                        kb_k=int(data.get("kb_k") or 4),
                        want_translation=want_translation, want_answer=True)
                else:
                    raw = persona.call_llm(interview_module.translate_messages(text), ctx.cfg)
                    result = persona.parse_turn(raw, lang, True, False)
            except ConfigError as exc:
                generation_error = str(exc)
            except HTTPError as exc:
                generation_error = "LLM 请求失败（HTTP %d）" % exc.code
            except (OSError, persona.PersonaError, ValueError, KeyError,
                    IndexError, TypeError, AttributeError) as exc:
                generation_error = "LLM 生成失败（%s）" % type(exc).__name__
            generation_ms = round((time.perf_counter() - started) * 1000)
            if generation_error:
                skip_reason = generation_error + "，已保留原文"
            elif want_answers and not (result.get("answer_zh") or result.get("answer_en")):
                skip_reason = "模型本轮未返回答案"
            elif not want_answers and not result.get("translation_zh"):
                skip_reason = "模型本轮未返回译文"
            elif not want_answers:
                skip_reason = "本轮只开启了翻译"
        elif want_translation and lang == "zh":
            skip_reason = "中文提问无需翻译；打开「回答」才会生成答案"
        else:
            skip_reason = "翻译 / 回答开关都未打开，本轮只记录原文"
        with ctx.lock:
            if ctx.session.data is not session or session["ended_at"] is not None:
                raise AppError("会话已结束或切换")
            turn = ctx.session.add_turn({
                "lang": lang, "text": text,
                "translation_zh": result.get("translation_zh", ""),
                "answer_zh": result.get("answer_zh", ""),
                "answer_en": result.get("answer_en", ""),
                "degraded": result.get("degraded", False),
                "generation_ms": generation_ms,
                "skip_reason": skip_reason})
            if turn["answer_zh"] or turn["answer_en"]:
                ctx.last_answer = {
                    "question": text, "answer_zh": turn["answer_zh"],
                    "answer_en": turn["answer_en"],
                    "translation_zh": turn["translation_zh"],
                    "lang": lang, "seq": turn["seq"], "ts": turn["ts"]}
            current = ctx.session.state()
        return {"turn": turn, "session": current, "generation_error": generation_error}

    def _load_settings(self) -> dict:
        data = dict(DEFAULT_SETTINGS)
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULT_SETTINGS:
                    if key in raw:
                        data[key] = (bool(raw[key]) if isinstance(
                            DEFAULT_SETTINGS[key], bool)
                            else sanitize_display_name(raw[key]))
        except (OSError, ValueError):
            pass
        return data

    def save_settings(self, updates: dict) -> dict:
        with self.lock:
            if "display_name" in updates:
                self.settings["display_name"] = sanitize_display_name(
                    updates["display_name"])
            if "mask_browser_window" in updates:
                self.settings["mask_browser_window"] = bool(
                    updates["mask_browser_window"])
            self.settings_path.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            self.display_name = self.settings["display_name"]
        return dict(self.settings)

    def app_windows(self) -> list[dict]:
        """只报告本进程窗口与本次启动的浏览器实例窗口，不列出用户其它窗口。"""
        if not stealth.IS_WINDOWS:
            return []
        try:
            rows = stealth.enum_windows()
        except Exception:
            return []
        return [r for r in rows if r["own"] or
                (self.browser_pid and r["pid"] == self.browser_pid)]

    def stealth_state(self) -> dict:
        return {
            "capabilities": stealth.capabilities(),
            "mode": self.mode,
            "windows": self.app_windows(),
            "mask_result": getattr(self, "mask_result", None),
            "note": (
                "共享模式下电脑仅采集声音，界面在另一台设备上打开。"
                if self.share_safe else
                "浏览器窗口属于外部进程，Windows 拒绝跨进程设置捕获排除。"
                "屏幕共享时请用 overlay（隐身提示窗）或 none（无窗口）模式。"
                if self.mode == "browser" else
                "提示窗尝试使用 Windows 捕获排除，效果取决于共享方式和软件。"
                if self.mode == "overlay" else
                "本机不开任何窗口，屏幕上没有可共享的界面。"),
        }

    def asr_status(self) -> dict:
        """ASR 侧就绪状态；与 LLM 完全无关，单独判断、单独展示。"""
        stt_type = ""
        try:
            cfg = self.cfg.speech
            stt_type = cfg["type"]
            if cfg.get("type") == "disabled":
                return {"ready": False, "type": "disabled", "label": "已停用",
                        "model": "", "url": "",
                        "reason": "ASR 已停用（顶栏「ASR」可重新选择）"}
            info = interview_module.asr_types().get(cfg["type"], {})
            url = interview_module.mask_stt_url(
                interview_module.stt_effective_url(cfg))
            model = interview_module.effective_asr_model(cfg)
            if stt_type == interview_module.REALTIME_STT and not importlib.util.find_spec("websockets"):
                raise AppError("请先安装实时 ASR 依赖：python -m pip install -r requirements.txt")
            # 地址合法不等于密钥齐备：headers / api_key 里的 ${ENV} 未解析时，
            # check() 会报缺环境变量。直接显示原因，避免「就绪」却 401。
            missing = [p for p in self.cfg.check()
                       if p.startswith("[speech:") and "缺少环境变量" in p]
            if missing:
                return {"ready": False, "type": cfg["type"],
                        "label": info.get("label", cfg["type"]),
                        "model": model, "url": url, "reason": "；".join(missing)}
            return {"ready": True, "type": cfg["type"],
                    "label": info.get("label", cfg["type"]),
                    "model": model, "url": url, "reason": ""}
        except (AppError, interview_module.InterviewError, ConfigError, KeyError) as exc:
            return {"ready": False, "type": stt_type, "label": "", "model": "",
                    "url": "", "reason": str(exc)}

    def speech_status(self) -> dict:
        """兼容旧字段名，返回同一个 ASR 状态。"""
        return self.asr_status()

    def state(self):
        self.kb.refresh()
        lan_urls = (["http://%s:%d/?token=%s" % (ip, self.port, self.token)
                     for ip in self.lan_ips]
                    if self.lan and self.token else [])
        files = []
        for path in sorted(self.kb_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in knowledge_base.SUPPORTED:
                continue
            stat = path.stat()
            files.append({"name": path.name, "size": stat.st_size,
                          "mtime": int(stat.st_mtime), "ext": path.suffix.lower()})
        try:
            speech_id, llm_id = self.cfg.effective_ids()
        except ConfigError:
            speech_id = llm_id = ""
        try:
            problems = self.cfg.check()
        except ConfigError as exc:
            problems = [str(exc)]
        llm_ready = bool(llm_id) and not [p for p in problems
                                         if "[llm:" in p or "LLM" in p]
        try:
            snapshot = self.cfg.snapshot()
        except ConfigError as exc:
            snapshot = {"error": str(exc)}
        return {
            "home": str(self.home),
            "persona": {
                "active": self.store.active,
                "list": {name: {"label": item.get("label", ""),
                                "has_prompt": bool(item.get("custom_prompt"))}
                         for name, item in self.store.list().items()},
                "current": self.store.current(),
                "using_example": self.store.using_example,
            },
            "kb": {"root": str(self.kb_dir), "files": files,
                   "stats": self.kb.stats(),
                   "supported": sorted(knowledge_base.SUPPORTED),
                   "max_upload_mb": MAX_UPLOAD // (1024 * 1024)},
            "provider": {
                "active": self.cfg.active_profile,
                "active_ids": {"speech": speech_id, "llm": llm_id},
                "selection": dict(self.cfg.raw.get("selection", {})),
                "asr_types": interview_module.asr_types(),
                "profiles": self.cfg.raw["profiles"],
                "speech_providers": public_providers(
                    self.cfg.raw["speech_providers"]),
                "llm_providers": public_providers(
                    self.cfg.raw["llm_providers"]),
                "secrets": self.cfg.secrets(),
                "snapshot": snapshot, "problems": problems,
                "dotenv": str(self.cfg.dotenv_path),
                "dotenv_exists": self.cfg.dotenv_path.exists(),
            },
            "llm_ready": llm_ready,
            "ui": {"display_name": self.display_name,
                   "mask_browser_window": self.settings["mask_browser_window"],
                   "modes": ["browser", "overlay", "none"]},
            "access": {
                "mode": self.mode, "lan": self.lan,
                "share_safe": self.share_safe,
                "local_url": "" if self.share_safe else "http://127.0.0.1:%d/" % self.port,
                "lan_ips": self.lan_ips,
                "lan_urls": lan_urls,
                "lan_url": lan_urls[0] if lan_urls else "",
                "token": self.token,
                "token_required": self.lan,
            },
            "stealth": self.stealth_state(),
            "interview": {
                "active": self.session.active,
                "mode": self.session.mode,
                "count": len(self.session.data["turns"]),
                "llm_ready": llm_ready,
                "speech": self.speech_status(),
                "vad_silence_ms": self.cfg.raw.get("defaults", {})
                                  .get("speech", {}).get("vad_silence_ms", 500),
                "phrase_list": self.cfg.raw.get("defaults", {})
                               .get("speech", {}).get("phrase_list", []),
            },
        }

    def start_capture(self, data):
        with self.capture_action:
            if self.capture is not None:
                raise AppError("当前采集尚未结束，请先结束面试")
            speech_status = self.asr_status()
            if not speech_status["ready"]:
                raise AppError(speech_status["reason"])
            source = data.get("source") or "tab"
            language = data.get("language") or "auto"
            if source not in ("tab", "mic") or language not in ("auto", "en-US", "zh-CN"):
                raise AppError("音源或语种无效")
            silence_ms = int(data.get("silence_ms", 500))
            if not 200 <= silence_ms <= 6000:
                raise AppError("静音断句应在 200–6000 ms 之间")
            capture = headless_audio.HeadlessCapture(
                self.cfg.speech, source, language, silence_ms, self.submit_turn)
            try:
                with self.lock:
                    session = self.session.start(data.get("mode"))
                    self.last_answer = None
                    capture.session_id = session["id"]
                    capture.outputs = (bool(data.get("translate")), bool(data.get("answers")))
                    self.capture = capture
                    capture.start()
            except Exception:
                self.capture = None
                capture.stop(False)
                capture.release()
                raise
            return {"session": session, "capture": capture.state()}


# ---------------------------------------------------------------- 输入校验

def sanitize_display_name(name) -> str:
    """窗口标题 / 页面标题用的中性显示名，去掉控制字符并限长。"""
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    return (text[:40] or stealth.DEFAULT_DISPLAY_NAME)


def safe_filename(name) -> str:
    name = str(name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", name)
    if not name or name.startswith(".") or len(name) > 120:
        raise AppError("文件名不合法")
    suffix = Path(name).suffix.lower()
    if suffix not in knowledge_base.SUPPORTED:
        raise AppError(
            "不支持 .%s；请用 %s，PDF 请先另存为 .txt / .docx" %
            (suffix.lstrip(".") or "?", " / ".join(sorted(knowledge_base.SUPPORTED))))
    return name


def safe_persona_name(name) -> str:
    name = str(name or "").strip()
    if not name or len(name) > 64:
        raise AppError("人设名应为 1–64 个字符")
    if not re.fullmatch(r"[\w\u4e00-\u9fff\- ]+", name):
        raise AppError("人设名只能包含中英文、数字、下划线、连字符和空格")
    return name


def normalize_persona(data: dict, current: dict) -> dict:
    """把表单的文本域整理成人设结构；未提交的字段沿用原值。"""
    out = dict(current)
    for key in ("label", "target_role", "years", "domain", "style", "custom_prompt"):
        if key in data:
            out[key] = str(data[key] or "").strip()
    for key in ("stack", "highlights", "constraints"):
        if key in data:
            value = data[key]
            rows = value if isinstance(value, list) else str(value or "").splitlines()
            out[key] = [str(x).strip() for x in rows if str(x).strip()]
    if "examples" in data:
        value = data["examples"]
        items = []
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict) and row.get("q") and row.get("a"):
                    items.append({"q": str(row["q"]).strip(),
                                  "a": str(row["a"]).strip()})
        else:
            for line in str(value or "").splitlines():
                if "|" in line:
                    q, a = line.split("|", 1)
                    if q.strip() and a.strip():
                        items.append({"q": q.strip(), "a": a.strip()})
        out["examples"] = items
    out.pop("id", None)
    return out


def decode_upload(b64) -> bytes:
    try:
        raw = base64.b64decode(str(b64 or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppError("文件内容不是合法的 base64：%s" % exc) from exc
    if not raw:
        raise AppError("文件内容为空")
    if len(raw) > MAX_UPLOAD:
        raise AppError("单个文件超过 %d MB" % (MAX_UPLOAD // (1024 * 1024)))
    return raw


def pick_lang(value) -> str:
    return "zh" if str(value or "en").lower().startswith("zh") else "en"


# ---------------------------------------------------------------- HTTP 处理

class Handler(BaseHTTPRequestHandler):
    ctx: AppContext = None
    server_version = "InterviewCopilot/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body: bytes, ctype, headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json(self, obj, status=200):
        self._send(status,
                   json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, exc, status=400):
        self._json({"error": str(exc)}, status)

    def _body(self, max_bytes=MAX_BODY) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= max_bytes:
            raise AppError("请求体长度无效或过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError("请求体不是合法 JSON：%s" % exc) from exc
        if not isinstance(data, dict):
            raise AppError("请求体应为 JSON 对象")
        return data

    def _authorized(self) -> bool:
        """所有请求校验 Host，局域网客户端读取数据时还须提供令牌。"""
        ctx = self.ctx
        parsed = urlparse(self.path)
        try:
            authority = urlparse("//" + (self.headers.get("Host") or "").strip())
        except ValueError:
            return False
        if (authority.hostname not in ctx.allowed_hosts or authority.username is not None
                or authority.path or authority.params or authority.query or authority.fragment):
            return False
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        if not ctx.lan:
            return False
        if parsed.path in STATIC_PUBLIC:
            return True
        token = self.headers.get("X-Copilot-Token") or ""
        if not token:
            token = parse_qs(parsed.query).get("token", [""])[0]
        return bool(token) and bool(ctx.token) and hmac.compare_digest(
            token.encode("utf-8"), ctx.token.encode("utf-8"))

    def _reject_local_ui(self):
        if not self.ctx.share_safe:
            return False
        client = ip_address(self.client_address[0])
        server = ip_address(self.connection.getsockname()[0])
        if client.is_loopback or client == server or str(client) in self.ctx.lan_ips:
            self._send(403, b"", "text/plain", {"X-Copilot-Local-UI": "disabled"})
            return True
        return False

    def _static(self, path):
        filename, ctype = STATIC[path]
        target = (WEB_DIR / filename).resolve()
        if WEB_DIR.resolve() not in target.parents:
            raise AppError("路径越界")
        if not target.exists():
            raise AppError("缺少界面文件：%s" % filename)
        if filename == "index.html":
            # 页面标题与顶栏品牌统一用中性显示名，窗口标题/Alt-Tab 不露痕迹
            text = target.read_text(encoding="utf-8").replace(
                "__DISPLAY_NAME__", html.escape(self.ctx.display_name))
            return self._send(200, text.encode("utf-8"), ctype)
        self._send(200, target.read_bytes(), ctype)

    def do_GET(self):
        if self._reject_local_ui():
            return
        path = urlparse(self.path).path
        if not self._authorized():
            return self._error("需要访问令牌：请用含 ?token= 的完整地址打开", 403)
        try:
            if path in STATIC:
                return self._static(path)
            if path == "/api/health":
                return self._json({"ok": True, "home": str(self.ctx.home)})
            if path == "/api/state":
                return self._json(self.ctx.state())
            if path == "/api/answer/latest":
                return self._json({"answer": self.ctx.last_answer})
            if path == "/api/stealth/status":
                return self._json(self.ctx.stealth_state())
            if path == "/api/interview/session":
                capture = self.ctx.capture
                return self._json({"session": self.ctx.session.state(),
                                   "answer": self.ctx.last_answer,
                                   "streaming": self.ctx.realtime is not None or capture is not None,
                                   "capture": capture.state() if capture else None})
            if path == "/api/interview/asr/live":
                stream = self.ctx.realtime or self.ctx.capture
                return self._json({"preview": stream.preview if stream else None,
                                   "session_id": stream.session_id if stream else None})
            if path == "/api/interview/export":
                fmt = parse_qs(urlparse(self.path).query).get("fmt", ["md"])[0]
                if fmt == "json":
                    body = json.dumps(self.ctx.session.state(),
                                      ensure_ascii=False, indent=2)
                    ctype = "application/json; charset=utf-8"
                else:
                    body = self.ctx.session.export_md()
                    ctype = "text/markdown; charset=utf-8"
                return self._send(200, body.encode("utf-8"), ctype)
            return self._error("not found: " + path, 404)
        except Exception as exc:
            traceback.print_exc()
            return self._error(exc, 500)

    def do_POST(self):
        if self._reject_local_ui():
            return
        path = urlparse(self.path).path
        if not self._authorized():
            return self._error("需要访问令牌", 403)
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        if (origin is not None and origin not in ("http://" + host, "https://" + host)
                or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            return self._error("请求来源与当前页面地址不一致", 403)
        realtime_route = path.startswith("/api/interview/asr/realtime/")
        try:
            if realtime_route:
                if (self.client_address[0] not in ("127.0.0.1", "::1")
                        or urlparse("//" + host).hostname not in ("127.0.0.1", "localhost", "::1")):
                    return self._error("流式 ASR 音频采集只接受本机页面", 403)
                if self.headers.get_content_type() != "application/json":
                    return self._error("流式 ASR 需要 JSON 请求", 415)
            data = self._body(max_bytes=90000 if realtime_route else MAX_BODY)
            if path in WRITE_ROUTES:
                with self.ctx.lock:
                    return self._route(path, data)
            return self._route(path, data)
        except AppError as exc:
            return self._error(exc, 400)
        except (persona.PersonaError, interview_module.InterviewError,
                ConfigError, ValueError, KeyError) as exc:
            return self._error(exc, 400)
        except OSError as exc:
            return self._error(exc, 500)
        except Exception as exc:
            traceback.print_exc()
            return self._error("%s: %s" % (type(exc).__name__, exc), 502)

    def _route(self, path, data):
        ctx = self.ctx

        if path == "/api/kb/upload":
            name = safe_filename(data.get("filename"))
            content = decode_upload(data.get("content_b64"))
            dest = ctx.kb_dir / name
            replaced = dest.exists()
            dest.write_bytes(content)
            ctx.kb.refresh(force=True)
            return self._json({"ok": True, "name": name, "replaced": replaced,
                               "state": ctx.state()})

        if path == "/api/kb/delete":
            name = safe_filename(data.get("filename"))
            target = ctx.kb_dir / name
            if not target.exists():
                raise AppError("资料不存在：" + name)
            target.unlink()
            ctx.kb.refresh(force=True)
            return self._json({"ok": True, "state": ctx.state()})

        if path == "/api/persona/save":
            name = safe_persona_name(data.get("name"))
            payload = data.get("data")
            if not isinstance(payload, dict):
                raise AppError("缺少人设内容 data")
            merged = normalize_persona(payload, ctx.store.current())
            ctx.store.save(name, merged)
            return self._json({"ok": True, "name": name, "state": ctx.state()})

        if path == "/api/persona/use":
            name = safe_persona_name(data.get("name"))
            ctx.store.use(name)
            return self._json({"ok": True, "state": ctx.state()})

        if path == "/api/persona/delete":
            name = safe_persona_name(data.get("name"))
            ctx.store.delete(name)
            return self._json({"ok": True, "state": ctx.state()})

        if path == "/api/persona/preview":
            store = persona.PersonaStore(path=ctx.store.path,
                                         example=ctx.store.example)
            if data.get("name"):
                store.set_override(str(data["name"]))
            messages = persona.build_messages(
                str(data.get("question") or ""),
                history=data.get("history"),
                source_lang=pick_lang(data.get("lang")),
                store=store, kb=ctx.kb,
                kb_k=int(data.get("kb_k") or 4),
                use_kb=bool(data.get("use_kb", True)))
            return self._json({"messages": messages})

        if path == "/api/persona/answer":
            if not ctx.state()["llm_ready"]:
                raise AppError("当前 LLM 服务未启用或密钥未配置：请在顶栏「LLM」选服务并在配置页填 key："
                               + "；".join(ctx.state()["provider"]["problems"]))
            question = str(data.get("question") or "")
            source_lang = pick_lang(data.get("lang"))
            # 与面试页同一套规则：答案语言跟随提问语言，英文问给英文稿。
            result = persona.generate_turn(
                question, cfg=ctx.cfg, store=ctx.store,
                history=data.get("history"),
                source_lang=source_lang, kb=ctx.kb,
                kb_k=int(data.get("kb_k") or 4),
                want_translation=(source_lang == "en"), want_answer=True)
            # 推给隐身提示窗；只存内存，不落盘
            ctx.last_answer = {"question": question,
                               "answer_zh": result.get("answer_zh", ""),
                               "answer_en": result.get("answer_en", ""),
                               "lang": source_lang,
                               "ts": time.time()}
            return self._json({"answer": result})

        if path == "/api/persona/build":
            if not ctx.state()["llm_ready"]:
                raise AppError("从资料生成人设需要可用的 LLM key")
            name = safe_persona_name(data.get("name") or ctx.store.active)
            # 表单里的手写提示词先落盘，保证「本次生成要求」与「以后回答注入」一致。
            if "prompt" in data:
                current = ctx.store.current()
                prompt = str(data["prompt"] or "")
                if str(current.get("custom_prompt") or "") != prompt:
                    current["custom_prompt"] = prompt
                    ctx.store.save(ctx.store.active, current)
            result = persona.build_persona_from_kb(
                ctx.cfg, store=ctx.store, kb=ctx.kb, name=name)
            return self._json({"ok": True, "name": name, "persona": result,
                               "state": ctx.state()})

        if path == "/api/provider/use":
            profile = str(data.get("profile") or "")
            ctx.cfg.use(profile)
            return self._json({"ok": True, "state": ctx.state()})

        if path == "/api/provider/save":
            kind = str(data.get("kind") or "")
            item_id = str(data.get("id") or "")
            result = ctx.cfg.set_provider(
                kind, item_id, data.get("data") or {},
                secret=data.get("secret") or "",
                persist_secret=bool(data.get("persist_secret", True)))
            return self._json({"ok": True, "id": item_id,
                               "provider": public_provider(result), "state": ctx.state()})

        if path == "/api/provider/delete":
            ctx.cfg.delete_provider(str(data.get("kind") or ""),
                                    str(data.get("id") or ""),
                                    drop_refs=bool(data.get("drop_refs")))
            return self._json({"ok": True, "state": ctx.state()})

        if path == "/api/provider/activate":
            selection = ctx.cfg.activate(str(data.get("kind") or ""),
                                         str(data.get("id") or ""))
            return self._json({"ok": True, "selection": selection,
                               "profile": ctx.cfg.active_profile,
                               "state": ctx.state()})

        if path == "/api/profile/save":
            name = ctx.cfg.set_profile(
                data.get("name"), data.get("speech"), data.get("llm"),
                description=data.get("description") or "")
            return self._json({"ok": True, "name": name,
                               "state": ctx.state()})

        if path == "/api/profile/delete":
            ctx.cfg.delete_profile(str(data.get("name") or ""))
            return self._json({"ok": True, "state": ctx.state()})

        if path == "/api/ui/save":
            settings = ctx.save_settings(data)
            # 立刻把已开的浏览器窗口标题改成新显示名
            applied = []
            if settings["mask_browser_window"]:
                for row in ctx.app_windows():
                    if not row["own"]:
                        applied.append(stealth.set_title(
                            row["hwnd"], settings["display_name"]))
            return self._json({"ok": True, "settings": settings,
                               "titles_updated": sum(bool(x) for x in applied),
                               "state": ctx.state()})

        if path == "/api/stealth/apply":
            results = []
            for row in ctx.app_windows():
                if row["own"]:
                    results.append(ctx.shield.protect_own(row["hwnd"]))
                else:
                    results.append(ctx.shield.mask(
                        row["hwnd"], ctx.display_name))
            ctx.mask_result = {
                "applied": results,
                "excluded": sum(1 for r in results if r.get("capture_exclusion")),
                "note": ("浏览器窗口无法排除捕获（跨进程限制）；"
                         "请切换 overlay / none 模式。"
                         if ctx.mode == "browser" else "已应用"),
            }
            return self._json({"ok": True, "result": ctx.mask_result,
                               "state": ctx.state()})

        if path in ("/api/stealth/hide", "/api/stealth/show"):
            visible = path.endswith("show")
            rows = ctx.app_windows()
            for row in rows:
                (ctx.shield.unhide(row["hwnd"]) if visible
                 else ctx.shield.hide(row["hwnd"]))
            return self._json({"ok": True, "visible": visible,
                               "windows": len(rows), "state": ctx.state()})

        # ---------------------------------------------------------- 面试链路
        if path == "/api/interview/capture/start":
            if not ctx.share_safe:
                raise AppError("电脑后台采集仅在共享模式可用")
            return self._json(ctx.start_capture(data))

        if path == "/api/interview/capture/outputs":
            with ctx.lock:
                capture = ctx.capture
                if not capture or data.get("session_id") != capture.session_id:
                    raise AppError("电脑后台采集已结束或切换")
                capture.outputs = (bool(data.get("translate")), bool(data.get("answers")))
            return self._json({"ok": True})

        if path == "/api/interview/start":
            mode = "live" if str(data.get("mode") or "") == "live" else "practice"
            with ctx.capture_action, ctx.lock:
                if ctx.capture:
                    raise AppError("当前采集尚未结束，请先结束面试")
                old_stream, ctx.realtime = ctx.realtime, None
                session = ctx.session.start(mode)
                ctx.last_answer = None
            if old_stream:
                old_stream.close()
            return self._json({"ok": True, "session": session,
                               "state": ctx.state()})

        if path == "/api/interview/clear":
            with ctx.capture_action:
                if ctx.capture:
                    ctx.capture.stop(False)
                    ctx.capture = None
                with ctx.lock:
                    old_stream, ctx.realtime = ctx.realtime, None
                    session = ctx.session.reset()
                    ctx.last_answer = None
            if old_stream:
                old_stream.close()
            return self._json({"ok": True, "session": session,
                               "state": ctx.state()})

        if path == "/api/interview/asr/realtime/open":
            if not ctx.realtime_open.acquire(blocking=False):
                raise AppError("正在连接实时 ASR，请稍后重试")
            try:
                with ctx.lock:
                    previous = ctx.session.data
                    previous_end = previous["ended_at"]
                    speech = ctx.cfg.speech
                if speech.get("type") != interview_module.REALTIME_STT:
                    raise AppError("请在顶栏 ASR 选择百炼流式服务")
                stream = realtime_asr.RealtimeASR(
                    speech, data.get("language") or "auto", data.get("silence_ms", 500))
                with ctx.lock:
                    changed = (ctx.session.data is not previous or previous["ended_at"] != previous_end)
                    if not changed:
                        session = ctx.session.start(data.get("mode"))
                        ctx.last_answer = None
                        stream.session_id = session["id"]
                        old_stream, ctx.realtime = ctx.realtime, stream
                if changed:
                    stream.close()
                    raise AppError("会话已切换，请重新开始录音")
                if old_stream:
                    old_stream.close()
                return self._json({"stream_id": stream.id, "session": session})
            finally:
                ctx.realtime_open.release()

        if path in ("/api/interview/asr/realtime/audio", "/api/interview/asr/realtime/finish"):
            with ctx.lock:
                stream = ctx.realtime
                if (stream is None or stream.id != data.get("stream_id")
                        or stream.session_id != ctx.session.data["id"]):
                    raise AppError("实时 ASR 会话已结束或切换")
            if path.endswith("/audio"):
                return self._json(stream.push(data.get("audio")))
            if data.get("cancel"):
                stream.close()
                result = {"events": [], "closed": True, "finalized": False}
            else:
                result = stream.finish()
            with ctx.lock:
                if ctx.realtime is stream:
                    ctx.realtime = None
            return self._json(result)

        if path == "/api/interview/asr":
            raw = interview_module.decode_audio(data.get("audio_b64"))
            language = str(data.get("language") or "auto")
            return self._json(interview_module.recognize_clip(raw, ctx.cfg.speech, language))

        if path == "/api/interview/turn":
            return self._json(ctx.submit_turn(data))

        if path == "/api/interview/end":
            capture_error = ""
            with ctx.capture_action:
                if (data.get("session_id") is not None
                        and data["session_id"] != ctx.session.data["id"]):
                    raise AppError("会话已切换")
                if ctx.capture:
                    capture_error = ctx.capture.stop(True)
                    ctx.capture = None
                with ctx.lock:
                    ended = ctx.session.end()
                    old_stream, ctx.realtime = ctx.realtime, None
            if old_stream:
                old_stream.close()
            return self._json({"ok": True, "session": ended["state"],
                               "capture_error": capture_error,
                               "file": ended["file"], "state": ctx.state()})

        if path == "/api/interview/review":
            if not ctx.state()["llm_ready"]:
                raise AppError("生成复盘需要可用的 LLM key")
            report = persona.call_llm(
                interview_module.review_messages(ctx.session.state()),
                ctx.cfg, json_mode=False)
            return self._json({"report": report,
                               "export_md": ctx.session.export_md()})

        if path == "/api/shutdown":
            # 先还原改动过的外部窗口，再退出；响应发出后由定时器执行
            def _stop():
                time.sleep(0.35)
                try:
                    ctx.shield.restore()
                finally:
                    os._exit(0)
            threading.Thread(target=_stop, daemon=True).start()
            return self._json({"ok": True, "stopping": True})

        return self._error("not found: " + path, 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ExclusiveServer(Server):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(home=HOME, port=8765, ctx=None, host="127.0.0.1", strict_port=False):
    ctx = ctx or AppContext(home)
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    if strict_port:
        return ExclusiveServer((host, port), handler)
    for offset in range(12):
        try:
            return Server((host, port + offset), handler)
        except OSError:
            continue
    raise RuntimeError("端口 %d–%d 都被占用" % (port, port + 11))


def sharing_service_listening(port: int) -> bool:
    """确认默认端口上的实例是否为手机共享后台。"""
    if not port:
        return False
    conn = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        conn.request("GET", "/api/health")
        response = conn.getresponse()
        return (response.status == 403
                and response.getheader("X-Copilot-Local-UI") == "disabled")
    except (OSError, HTTPException):
        return False
    finally:
        conn.close()


def physical_ipv4() -> list[str]:
    """Windows 上找出正在联网的实体网卡地址，用于推荐给同一局域网的手机。"""
    if sys.platform != "win32":
        return []
    command = (
        "$physical = (Get-NetAdapter -Physical | Where-Object Status -eq Up).ifIndex; "
        "Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred | "
        "Where-Object { $_.InterfaceIndex -in $physical } | "
        "Select-Object -ExpandProperty IPAddress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="ascii", errors="ignore", timeout=4, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in result.stdout.splitlines():
        try:
            addr = ip_address(line.strip())
        except ValueError:
            continue
        if addr.version == 4 and not (addr.is_loopback or addr.is_link_local or addr.is_unspecified):
            found.append(str(addr))
    return list(dict.fromkeys(found))


def local_ips() -> list[str]:
    """列出可用于手机访问的局域网地址；不发起实际网络连接。"""
    ips: list[str] = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and not ip.startswith("169.254"):
                if ip not in ips:
                    ips.append(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("223.5.5.5", 80))   # UDP connect 不发包，只取出口地址
            ip = sock.getsockname()[0]
        if not ip.startswith(("127.", "169.254.")):
            if ip in ips:
                ips.remove(ip)
            ips.insert(0, ip)
    except OSError:
        pass
    physical = physical_ipv4()
    return list(dict.fromkeys(
        [ip for ip in ips if ip in physical] + physical + ips))


def open_window(url: str, force_browser=False, display_name="Notes"):
    """独立 profile 的 Edge/Chrome --app 窗口；返回进程对象便于精确定位窗口。

    用独立 --user-data-dir 有三个理由：不会复用用户日常浏览器进程（PID 可定位）、
    不会带出用户的扩展/书签/标签页、首次运行弹窗可用参数关掉。
    """
    if not force_browser:
        candidates = []
        for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if not base:
                continue
            candidates.extend([
                Path(base) / "Microsoft/Edge/Application/msedge.exe",
                Path(base) / "Google/Chrome/Application/chrome.exe",
            ])
        profile = HOME / "runtime" / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        args = ["--app=" + url, "--window-size=1360,900",
                "--user-data-dir=" + str(profile),
                "--no-first-run", "--no-default-browser-check",
                "--disable-features=msEdgeSidebarV2"]
        for exe in candidates:
            if not exe.exists():
                continue
            try:
                return subprocess.Popen(
                    [str(exe)] + args,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                continue
    webbrowser.open(url)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Interview Copilot 本地应用")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--window", choices=["browser", "overlay", "none"])
    parser.add_argument("--browser", action="store_true", help="用默认浏览器打开")
    parser.add_argument("--setup", action="store_true", help="兼容原有设置启动器，使用默认桌面模式")
    parser.add_argument("--no-open", action="store_true", help="不打开浏览器窗口")
    parser.add_argument("--share-safe", action="store_true",
                        help="电脑后台采集，界面仅供其他局域网设备访问")
    parser.add_argument("--lan", action="store_true",
                        help="允许局域网访问（手机 / 第二设备），强制 token 鉴权")
    parser.add_argument("--token", default="", help="自定义访问令牌")
    parser.add_argument("--silent", action="store_true",
                        help="无控制台输出，日志写 logs/app.log（配合 pythonw）")
    args = parser.parse_args(argv)

    share_safe = args.share_safe
    mode = "none" if share_safe else args.window or ("none" if args.no_open else "browser")
    lan = share_safe or args.lan
    if args.silent:
        log_dir = HOME / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = (log_dir / "app.log").open(
            "a", encoding="utf-8", buffering=1)
        print("\n--- %s ---" % time.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        server = make_server(port=args.port,
                             host="0.0.0.0" if lan else "127.0.0.1",
                             strict_port=share_safe or mode == "browser")
    except OSError as exc:
        if sharing_service_listening(args.port):
            print("手机共享后台仍在运行，占用了端口 %d。" % args.port, file=sys.stderr)
            if not share_safe:
                print("请先在手机上结束面试并退出服务，再启动桌面版。", file=sys.stderr)
        else:
            print("无法监听端口 %d：%s" % (args.port, exc), file=sys.stderr)
            print("如果另一个启动模式仍在运行，请先退出该模式再重试。", file=sys.stderr)
        return 1
    ctx = server.RequestHandlerClass.ctx
    port = server.server_address[1]
    ctx.port = port
    ctx.mode = mode
    ctx.share_safe = share_safe
    ctx.lan = lan
    ctx.token = args.token or (secrets.token_urlsafe(18) if lan else "")
    ctx.lan_ips = local_ips() if lan else []
    ctx.allowed_hosts |= set(ctx.lan_ips)
    local = "http://127.0.0.1:%d/" % port
    phones = (["http://%s:%d/?token=%s" % (ip, port, ctx.token)
               for ip in ctx.lan_ips] if ctx.token else [])
    phone_lines = ("".join("%s  %s\n" % ("手机（推荐）" if i == 0 else "手机（备用）", u)
                           for i, u in enumerate(phones))
                   or "手机  未取到局域网地址\n")

    mode_label = "share-safe" if share_safe else mode
    local_line = "本机  界面已禁用（请在第二台设备打开）\n" if share_safe else "本机  %s\n" % local
    print("模式: %s  数据目录: %s" % (mode_label, ctx.home))
    print(local_line.rstrip())
    if lan:
        for u in phones:
            print("手机候选: %s" % u)
        if len(phones) > 1:
            print("手机请先试「推荐」地址，并连接与电脑相同的局域网。")
        print("首次绑定 0.0.0.0 时 Windows 防火墙可能弹窗，请提前允许一次。")
        print("访问令牌在重启后会自动更新（除非显式指定）。")
    (ctx.config_dir / "access.txt").write_text(
        "%s%s模式  %s\n" % (local_line, phone_lines, mode_label),
        encoding="utf-8")

    atexit.register(ctx.shield.restore)

    if mode == "browser" and not args.no_open:
        def _launch():
            proc = open_window(local, args.browser, ctx.display_name)
            if proc is None:
                return
            ctx.browser_pid = proc.pid
            if not (ctx.settings["mask_browser_window"] and stealth.IS_WINDOWS):
                return
            for _ in range(24):          # 等窗口真正出现，最多 ~7 秒
                time.sleep(0.3)
                rows = [r for r in stealth.enum_windows(pid=proc.pid)
                        if r["title"]]
                if rows:
                    results = [ctx.shield.mask(r["hwnd"], ctx.display_name)
                               for r in rows]
                    ctx.mask_result = {
                        "applied": results,
                        "excluded": sum(1 for r in results
                                        if r.get("capture_exclusion")),
                        "note": "浏览器窗口无法排除捕获（跨进程限制）",
                    }
                    break
        threading.Thread(target=_launch, daemon=True).start()

    if mode == "overlay":
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("隐身提示窗已启动；关闭提示窗即退出服务。")
        try:
            overlay_module.run_overlay(local, ctx.display_name)
        except RuntimeError as exc:
            print("提示窗启动失败：%s" % exc)
        finally:
            server.shutdown()
            server.server_close()
            ctx.shield.restore()
        return 0

    print("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        if ctx.capture:
            ctx.capture.stop(False)
        server.server_close()
        ctx.shield.restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
