"""面试会话层：ASR（WAV → 文本）、语种判定、翻译、会话记录与复盘。

ASR 与 LLM 完全解耦：ASR 按 provider 的 type 分发到不同协议适配器，只负责
把一段 WAV 变成文本；文本侧（译文 / 回答 / 复盘）只认 LLM，不知道 ASR 是谁。
新增 ASR 服务商只需在配置页新建一套服务，不用改代码。

支持的 ASR 协议见 asr_types()：Azure Speech REST、OpenAI 兼容
/audio/transcriptions、通用 HTTP（任意国内 ASR / 自建网关，请求与响应都可配）、
百炼实时 WebSocket（由 realtime_asr.py 接入）、faster-whisper（占位）。
实战模式不落盘，练习模式结束后写入 runtime/sessions/。
"""
from __future__ import annotations

import base64
import json
import re
import struct
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

STT_PATH = "/speech/recognition/conversation/cognitiveservices/v1"
STT_AUDIO_PATH = "/audio/transcriptions"
AZURE_STT = "azure_speech_translation"
OPENAI_STT = "openai_compatible_stt"
GENERIC_STT = "generic_http_stt"
REALTIME_STT = "qwen_realtime_stt"
WHISPER_STT = "faster_whisper"
SUPPORTED_LANG = {"en-US", "zh-CN"}
SENSITIVE_QUERY = re.compile(r"key|token|secret|sig|signature|password|credential", re.I)
TEXT_KEYS = ("text", "transcript", "result", "DisplayText", "content", "sentence")
TIMEOUT_S = 12
MAX_WAV_BYTES = 4 * 1024 * 1024

# 每种 ASR 协议需要哪些配置字段、怎么鉴权；配置页据此渲染表单。
# 加协议时在这里登记 + 在 recognize() 里加一个分支即可，LLM 侧无需改动。
ASR_TYPE_INFO = {
    AZURE_STT: {
        "label": "Azure Speech REST",
        "address": "region / 资源 Endpoint / 完整识别地址",
        "auth": "Ocp-Apim-Subscription-Key",
        "fields": ["stt_url", "region", "endpoint"],
        "note": "Azure 门户、APIM 或自建代理；stt_url 填了就原样请求，端口 / 路径 / 查询串全保留。",
    },
    OPENAI_STT: {
        "label": "OpenAI 兼容 /audio/transcriptions",
        "address": "Base URL（到 /v1）或完整识别地址",
        "auth": "Authorization: Bearer",
        "fields": ["base_url", "stt_url", "model"],
        "note": "自建 Whisper / FunASR、Groq、各类 OpenAI 兼容网关；multipart 上传 WAV，取返回的 text。",
    },
    GENERIC_STT: {
        "label": "通用 HTTP（任意服务商 / 自建网关）",
        "address": "完整识别地址 stt_url",
        "auth": "在 Headers 里用 ${...} 引用密钥，或写公网 token",
        "fields": ["stt_url", "model", "stt_method", "stt_headers", "stt_body",
                   "audio_field", "stt_extra", "stt_json_template",
                   "response_path", "stt_lang_query"],
        "note": "请求方法、请求头、请求体格式、额外业务字段、语种参数名、响应文本路径都可配；"
                "国内 ASR / 私有部署不必改代码。请求体选 json_template 可写嵌套 JSON，"
                "例如百炼 qwen3-asr-flash 的 chat/completions。"
                "「模型名」是唯一入口：填了就覆盖模板 / 额外字段里的同名 model，留空才用模板里的值。",
    },
    REALTIME_STT: {
        "label": "百炼 Qwen 流式 ASR",
        "address": "业务空间 WebSocket 地址（wss://…/api-ws/v1/realtime）",
        "auth": "Authorization: Bearer",
        "fields": ["stt_url", "model"],
        "note": "填百炼业务空间 WebSocket 地址与该地域密钥。音频边采边传，实时显示转写，服务端断句。",
    },
    WHISPER_STT: {
        "label": "本地 faster-whisper",
        "address": "本机模型路径",
        "auth": "无需密钥",
        "fields": ["model", "device"],
        "available": False,
        "note": "需另装 faster-whisper 依赖，当前版本只保留配置项，选中后就绪判断会提示。",
    },
}


def asr_types() -> dict:
    """给配置页用的 ASR 协议清单；ASR 与 LLM 各自独立，互不知晓。"""
    return {k: dict(v, available=v.get("available", True))
            for k, v in ASR_TYPE_INFO.items()}


class InterviewError(Exception):
    pass


# ---------------------------------------------------------------- WAV / PCM

def wav_info(raw: bytes) -> dict:
    """校验浏览器上传的 WAV，返回采样信息；只接受 16bit PCM。"""
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise InterviewError("音频不是 WAV 格式")
    if len(raw) > MAX_WAV_BYTES:
        raise InterviewError("单段音频过长（>4 MB），请检查断句设置")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        body = pos + 8
        if cid == b"fmt " and size >= 16:
            fmt = {
                "format": struct.unpack_from("<H", raw, body)[0],
                "channels": struct.unpack_from("<H", raw, body + 2)[0],
                "rate": struct.unpack_from("<I", raw, body + 4)[0],
                "bits": struct.unpack_from("<H", raw, body + 14)[0],
            }
        elif cid == b"data":
            data = (body, min(size, len(raw) - body))
        pos = body + size + (size & 1)
        if fmt and data:
            break
    if not fmt or not data:
        raise InterviewError("WAV 缺少 fmt 或 data 块")
    if fmt["format"] != 1 or fmt["bits"] != 16:
        raise InterviewError("只接受 16bit PCM WAV")
    if fmt["rate"] != 16000 or fmt["channels"] != 1:
        raise InterviewError("音频须为 16000 Hz 单声道，实际 %s Hz / %s 声道"
                             % (fmt["rate"], fmt["channels"]))
    return {"header": raw[:data[0]], "pcm": raw[data[0]:data[0] + data[1]],
            "duration_ms": data[1] // 32}


def decode_audio(b64: str) -> bytes:
    try:
        raw = base64.b64decode(str(b64 or ""), validate=True)
    except Exception as exc:
        raise InterviewError("音频 base64 无效：%s" % exc) from exc
    wav_info(raw)
    return raw


# ---------------------------------------------------------------- Azure STT

def _url_parts(value, label: str):
    """接受完整 URL 或纯域名；拒绝非 http(s) 协议，避免 file:// 之类被当地址发出去。"""
    text = str(value or "").strip()
    if not text:
        raise InterviewError("语音服务 %s 为空，请到配置页填写" % label)
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise InterviewError("语音服务 %s 需为 http(s) 地址，当前：%s" % (label, value))
    return parts


def _compose(parts, path: str) -> str:
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def append_query(url: str, params: dict) -> str:
    """已有查询串的自定义地址（网关常带 subscription-key）要用 & 追加语种参数。"""
    if url.endswith("?") or url.endswith("&"):
        sep = ""
    else:
        sep = "&" if urlsplit(url).query else "?"
    return url + sep + urlencode(params)


def stt_endpoint(speech: dict) -> str:
    """Azure 语音识别地址，优先级：stt_url > endpoint > region。

    stt_url 是完整识别地址，填了就原样用（保留端口、网关路径、已有查询串）；
    endpoint 是 Azure 门户给的资源域名，会换成 STT 域名；
    两者都空才按 region 拼官方域名。
    """
    if speech.get("type") != AZURE_STT:
        raise InterviewError(
            "当前 ASR 类型是 %s，Azure 地址规则不适用；"
            "请在配置页把服务协议换成通用 HTTP / OpenAI 兼容" % speech.get("type"))
    key = str(speech.get("api_key") or "").strip()
    if not key or key.startswith("${"):
        raise InterviewError("Speech key 未配置，请到配置页填写")

    full = str(speech.get("stt_url") or "").strip()
    if full:
        parts = _url_parts(full, "stt_url")
        path = parts.path.rstrip("/")
        return _compose(parts, path if path else STT_PATH)

    custom = str(speech.get("endpoint") or "").strip().rstrip("/")
    if custom:
        parts = _url_parts(custom, "endpoint")
        host = parts.netloc
        if "stt.speech" not in host:
            # 门户给的是资源 endpoint（xxx.cognitiveservices.azure.cn），换成 STT 域名
            host = re.sub(r"\.cognitiveservices\.azure\.cn$",
                          ".stt.speech.azure.cn", host)
            host = re.sub(r"\.api\.cognitive\.microsoft\.com$",
                          ".stt.speech.microsoft.com", host)
        path = parts.path.rstrip("/")
        if STT_PATH in path:
            path = path.split(STT_PATH)[0].rstrip("/")
        # 自定义网关的路径前缀要保留，不能只取 host
        path = (path + STT_PATH) if path else STT_PATH
        return urlunsplit((parts.scheme, host, path, parts.query, ""))

    region = str(speech.get("region") or "").strip()
    if not region or region.startswith("${"):
        raise InterviewError("Speech region 未配置：请在配置页填 region，或填 endpoint / 完整识别地址")
    suffix = "azure.cn" if region.lower().startswith("china") or region.lower() in (
        "chinaeast2", "chinaeast3", "chinanorth", "chinanorth2",
        "chinanorth3", "chinaeast") else "microsoft.com"
    return "https://%s.stt.speech.%s%s" % (region, suffix, STT_PATH)


def stt_openai_config(speech: dict) -> tuple[str, str, str]:
    """OpenAI 兼容 STT 的地址、密钥、模型；base_url 填到 /v1，自动补音频路径。"""
    key = str(speech.get("api_key") or "").strip()
    if not key or key.startswith("${"):
        raise InterviewError("语音服务 key 未配置，请到配置页填写")
    model = str(speech.get("model") or "").strip()
    if not model or model.startswith("${"):
        raise InterviewError("语音服务 model 未配置，请到配置页填写")
    full = str(speech.get("stt_url") or "").strip()
    if full:
        parts = _url_parts(full, "stt_url")
        path = parts.path.rstrip("/") or STT_AUDIO_PATH
        return _compose(parts, path), key, model
    parts = _url_parts(speech.get("base_url"), "base_url")
    path = parts.path.rstrip("/")
    if not path.endswith(STT_AUDIO_PATH):
        path = path + STT_AUDIO_PATH
    return _compose(parts, path), key, model


def realtime_stt_url(speech: dict) -> str:
    address = str(speech.get("stt_url") or "").strip()
    if not address or "<" in address or "${" in address:
        raise InterviewError("请填写百炼业务空间的 WebSocket 地址（wss://…/api-ws/v1/realtime）")
    try:
        parts = urlsplit(address)
        if (parts.scheme != "wss" or not parts.hostname or parts.username is not None or parts.port == 0
                or parts.fragment or not parts.path.strip("/")):
            raise ValueError()
    except ValueError as exc:
        raise InterviewError("实时识别地址须为完整的 wss:// 地址") from exc
    model = str(speech.get("model") or "").strip()
    if not model:
        raise InterviewError("请填写实时 ASR 模型名")
    if not speech.get("api_key"):
        raise InterviewError("请填写百炼实时 ASR 密钥")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "model"]
    query.append(("model", model))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def stt_effective_url(speech: dict) -> str:
    """当前实际会请求的地址；同时用于就绪判断，配置不对就直接报出来。"""
    stt_type = speech.get("type")
    if stt_type == AZURE_STT:
        return stt_endpoint(speech)
    if stt_type == OPENAI_STT:
        return stt_openai_config(speech)[0]
    if stt_type == GENERIC_STT:
        return stt_generic_config(speech)[0]
    if stt_type == REALTIME_STT:
        return realtime_stt_url(speech)
    if stt_type == WHISPER_STT:
        raise InterviewError("faster-whisper 为离线方案，需另装依赖后再接")
    raise InterviewError("当前 ASR 类型 %s 不支持实时识别；请在配置页换成已支持的协议"
                         % stt_type)


def mask_stt_url(url: str) -> str:
    """页面展示用：移除用户信息并遮盖查询串里的凭据。"""
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    pairs = [(n, "***" if v and SENSITIVE_QUERY.search(n) else v)
             for n, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, netloc, parts.path,
                       urlencode(pairs), ""))


def stt_request(raw: bytes, speech: dict, language: str) -> tuple[str, dict]:
    url = append_query(stt_endpoint(speech), {
        "language": language if language in SUPPORTED_LANG else "en-US",
        "format": "simple",
    })
    headers = {
        "Ocp-Apim-Subscription-Key": speech["api_key"],
        "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
        "Accept": "application/json",
    }
    return url, headers


def stt_openai_request(raw: bytes, speech: dict, language: str) -> tuple[str, dict, bytes]:
    """OpenAI 兼容 /audio/transcriptions：multipart 上传 WAV + Bearer 鉴权。"""
    url, key, model = stt_openai_config(speech)
    boundary = "----copilot" + uuid.uuid4().hex
    lang = "zh" if str(language).lower().startswith("zh") else "en"
    body = bytearray()
    for name, value in (("model", model), ("language", lang),
                        ("response_format", "json")):
        body += ("--%s\r\n" % boundary).encode("ascii")
        body += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("ascii")
        body += ("%s\r\n" % value).encode("utf-8")
    body += ("--%s\r\n" % boundary).encode("ascii")
    body += (b'Content-Disposition: form-data; name="file"; '
             b'filename="audio.wav"\r\n')
    body += b"Content-Type: audio/wav\r\n\r\n"
    body += raw + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("ascii")
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Accept": "application/json",
    }
    return url, headers, bytes(body)


# ---------------------------------------------------------------- 通用 HTTP ASR

def _json_object(value, label: str) -> dict:
    """配置页里以 JSON 字符串保存的请求头 / 额外业务字段。"""
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InterviewError("ASR %s 不是合法 JSON 对象：%s" % (label, exc)) from exc
    if not isinstance(obj, dict):
        raise InterviewError("ASR %s 需为 JSON 对象（如 {\"key\": \"value\"}）" % label)
    return obj


def stt_generic_config(speech: dict) -> tuple[str, dict, dict]:
    """通用 HTTP ASR：完整地址 + 请求头 + 额外业务字段。"""
    parts = _url_parts(speech.get("stt_url"), "stt_url")
    url = _compose(parts, parts.path or "/")
    headers = {str(k): str(v) for k, v in
               _json_object(speech.get("stt_headers"), "stt_headers").items()}
    extra = _json_object(speech.get("stt_extra"), "stt_extra")
    return url, headers, extra


def _multipart(fields: list[tuple[str, str]], audio_field: str,
               raw: bytes, boundary: str) -> bytes:
    body = bytearray()
    for name, value in fields:
        body += ("--%s\r\n" % boundary).encode("ascii")
        body += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("ascii")
        body += ("%s\r\n" % value).encode("utf-8")
    body += ("--%s\r\n" % boundary).encode("ascii")
    body += ('Content-Disposition: form-data; name="%s"; '
             'filename="audio.wav"\r\n' % audio_field).encode("ascii")
    body += b"Content-Type: audio/wav\r\n\r\n"
    body += raw + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("ascii")
    return bytes(body)


TEMPLATE_VARS = ("audio_data_uri", "audio_base64", "audio_mime",
                 "language", "lang_short")


def _asr_model(speech: dict) -> str:
    """顶层 model：唯一入口；${ENV} 未解析的占位不算有效值。"""
    value = str((speech or {}).get("model") or "").strip()
    return "" if value.startswith("${") else value


def effective_asr_model(speech: dict) -> str:
    """当前 ASR 实际会用的模型名（页面展示 / 就绪判断用）。

    顶层 model 优先，会覆盖 json_template / stt_extra 里写死的同名值；
    留空时回退到 json_template 的 model。Azure Speech 无模型概念，返回空串。
    """
    speech = speech or {}
    top = _asr_model(speech)
    if top:
        return top
    if (speech.get("type") == GENERIC_STT
            and str(speech.get("stt_body") or "").strip() == "json_template"):
        try:
            obj = json.loads(str(speech.get("stt_json_template") or "").strip())
        except (TypeError, ValueError):
            return ""
        if isinstance(obj, dict):
            return str(obj.get("model") or "")
    return ""


def render_stt_template(template: str, raw: bytes, language: str,
                        model: str = "") -> bytes:
    """JSON 请求体模板：音频 / 语种占位符替换后作为完整请求体。

    占位符写在引号里，如 "data": "{{audio_data_uri}}"。替换值只有 base64、
    Data URI 和语种代码，不含引号与反斜杠，替换后仍是合法 JSON。用于百炼
    qwen3-asr-flash 这类 chat/completions 的嵌套 input_audio 结构。
    model 非空时覆盖模板里写死的同名值，让页面「模型名」改一次就生效。
    """
    text = str(template or "").strip()
    if not text:
        raise InterviewError("ASR 请求体模板 stt_json_template 为空")
    b64 = base64.b64encode(raw).decode("ascii")
    zh = str(language).lower().startswith("zh")
    values = {
        "audio_data_uri": "data:audio/wav;base64," + b64,
        "audio_base64": b64,
        "audio_mime": "audio/wav",
        "language": "zh-CN" if zh else "en-US",
        "lang_short": "zh" if zh else "en",
    }
    for name, value in values.items():
        text = text.replace("{{%s}}" % name, value)
    unknown = sorted(set(re.findall(r"\{\{([A-Za-z_]+)\}\}", text)))
    if unknown:
        raise InterviewError(
            "ASR 请求体模板含未知占位符 %s；可用：%s"
            % (", ".join(unknown), ", ".join(TEMPLATE_VARS)))
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InterviewError(
            "ASR 请求体模板不是合法 JSON：%s；占位符需写在引号内" % exc) from exc
    if not isinstance(obj, dict):
        raise InterviewError("ASR 请求体模板需为 JSON 对象（如 {...}）")
    value = str(model or "").strip()
    if value and not value.startswith("${"):
        obj["model"] = value
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def stt_generic_request(raw: bytes, speech: dict, language: str) -> tuple[str, dict, bytes, str]:
    """任意 HTTP ASR：四种请求体格式 + 可配请求头 / 语种参数 / 额外字段。"""
    url, headers, extra = stt_generic_config(speech)
    model = _asr_model(speech)
    if model:
        # 顶层「模型名」优先于额外字段里写死的 model，页面改一次即可生效
        extra["model"] = model
    qname = str(speech.get("stt_lang_query") or "").strip()
    if qname:
        url = append_query(url, {
            qname: "zh-CN" if str(language).lower().startswith("zh") else "en-US"})
    method = str(speech.get("stt_method") or "POST").strip().upper()
    if method not in ("POST", "PUT"):
        raise InterviewError("ASR stt_method 只支持 POST / PUT，当前：%s" % method)
    body = str(speech.get("stt_body") or "raw_wav").strip()
    field = str(speech.get("audio_field") or "").strip()
    payload = raw
    if body == "multipart":
        field = field or "file"
        fields = [(str(k), json.dumps(v, ensure_ascii=False)
                   if isinstance(v, (dict, list)) else str(v))
                  for k, v in extra.items()]
        boundary = "----copilot" + uuid.uuid4().hex
        payload = _multipart(fields, field, raw, boundary)
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif body == "json_base64":
        field = field or "audio"
        obj = dict(extra)
        obj[field] = base64.b64encode(raw).decode("ascii")
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif body == "json_template":
        payload = render_stt_template(speech.get("stt_json_template"), raw,
                                      language, model=model)
        headers.setdefault("Content-Type", "application/json")
    elif body in ("raw_wav", "raw", ""):
        headers.setdefault("Content-Type", "audio/wav")
    else:
        raise InterviewError(
            "ASR stt_body 只支持 raw_wav / multipart / json_base64 / "
            "json_template，当前：%s" % body)
    headers.setdefault("Accept", "application/json")
    return url, headers, payload, method


def extract_by_path(payload, path: str) -> str:
    """按点分路径取文本，支持数组下标，如 data.result.0.text。"""
    node = payload
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return ""
        if node is None:
            return ""
    if isinstance(node, dict):
        for key in TEXT_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(node, list):
        return " ".join(str(x).strip() for x in node
                        if isinstance(x, str)).strip()
    return str(node or "").strip()


def parse_stt_generic(payload, response_path="") -> str:
    """通用响应解析：优先按 response_path，未填则找常见文本字段。"""
    if response_path:
        return extract_by_path(payload, str(response_path))
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    for key in TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for wrapper in ("data", "result", "output", "response"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict):
            text = parse_stt_generic(inner)
            if text:
                return text
    return ""


def parse_stt(payload: dict) -> str:
    status = str(payload.get("RecognitionStatus") or "")
    text = str(payload.get("DisplayText") or payload.get("NBest", [{}])[0].get("Display") or "").strip()
    if status and status != "Success":
        if status in ("InitialSilenceTimeout", "NoMatch", "EndOfDictation"):
            return ""
        raise InterviewError("语音识别返回 %s：%s"
                             % (status, payload.get("Message") or ""))
    return text


def parse_stt_openai(payload) -> str:
    """兼容 {"text": ...} 与部分网关的 {"data": {"text": ...}}。"""
    if not isinstance(payload, dict):
        return str(payload or "").strip()
    text = payload.get("text")
    if text is None:
        for wrapper in ("data", "result"):
            inner = payload.get(wrapper)
            if isinstance(inner, dict):
                text = inner.get("text")
                break
    return str(text or "").strip()


def recognize(raw: bytes, speech: dict, language: str, opener=urlopen) -> str:
    """ASR 只做 WAV → 文本，按 type 分发；不知道也不需要知道 LLM 是谁。"""
    stt_type = speech.get("type")
    if stt_type == OPENAI_STT:
        url, headers, payload = stt_openai_request(raw, speech, language)
        method = "POST"
    elif stt_type == GENERIC_STT:
        url, headers, payload, method = stt_generic_request(raw, speech, language)
    elif stt_type == AZURE_STT:
        url, headers = stt_request(raw, speech, language)
        payload, method = raw, "POST"
    else:
        raise InterviewError("当前 ASR 类型 %s 不支持实时识别" % stt_type)
    req = Request(url, data=payload, headers=headers, method=method)
    try:
        with opener(req, timeout=TIMEOUT_S) as resp:
            text = resp.read().decode("utf-8", "replace").strip()
    except HTTPError as exc:
        if stt_type != GENERIC_STT:
            raise
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        # 百炼 Qwen-Audio 等服务在音频里没有词时返回 400
        # ASR_RESPONSE_HAVE_NO_WORDS。VAD 误触发或纯静音会碰到，按空识别
        # 处理即可，不要把它当失败打断面试；其余错误照常抛出。
        if "NO_WORDS" in detail.upper():
            return ""
        raise
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 少数版本在无匹配时返回空 JSON 或纯文本
        return "" if text in ("{}", "null") else text.strip('"')
    if stt_type == OPENAI_STT:
        return parse_stt_openai(data)
    if stt_type == GENERIC_STT:
        return parse_stt_generic(data, speech.get("response_path", ""))
    return parse_stt(data)


def detect_lang(text: str) -> str:
    """中英二选一，按汉字占比判定；中文提问夹 SAP / BAdI 仍算中文。"""
    s = str(text or "").strip()
    if not s:
        return "en"
    han = len(re.findall(r"[\u4e00-\u9fff]", s))
    letters = len(re.findall(r"[A-Za-z]", s))
    return "zh" if han >= 2 and han / max(1, han + letters) > 0.25 else "en"


def recognize_auto(raw: bytes, speech: dict, opener=urlopen) -> tuple[str, str]:
    """只有切换语种会改变请求时，才对判为中文的结果再识别一次。"""
    text = recognize(raw, speech, "en-US", opener)
    lang = detect_lang(text)
    if lang == "zh":
        template = (str(speech.get("stt_json_template") or "")
                    if str(speech.get("stt_body") or "").strip() == "json_template" else "")
        if (speech.get("type") != GENERIC_STT
                or str(speech.get("stt_lang_query") or "").strip()
                or "{{language}}" in template or "{{lang_short}}" in template):
            text = recognize(raw, speech, "zh-CN", opener) or text
    return text, lang


def recognize_clip(raw: bytes, speech: dict, language: str) -> dict:
    """整段识别入口，网页与 Windows 后台采集共用。"""
    duration_ms = wav_info(raw)["duration_ms"]
    if speech.get("type") == "disabled":
        raise InterviewError("ASR 已停用，请在「服务」中选择语音识别服务")
    if speech.get("type") == REALTIME_STT:
        raise InterviewError("当前服务使用流式 ASR，请重新开始录音")
    started = time.perf_counter()
    if language == "auto":
        text, detected = recognize_auto(raw, speech)
    else:
        chinese = language.startswith("zh")
        text = recognize(raw, speech, "zh-CN" if chinese else "en-US")
        detected = "zh" if chinese else detect_lang(text)
    return {"text": text, "lang": detected, "duration_ms": duration_ms,
            "latency_ms": round((time.perf_counter() - started) * 1000)}


# ---------------------------------------------------------------- 文本旁路

TRANSLATE_SYSTEM = """你是面试现场翻译。把面试官说的英文原话译成中文，供候选人快速看懂。

规则：
1. 忠实原意，不补充、不解释、不改写问题，不加入任何回答内容。
2. SAP、ABAP、BTP、CAP、CPI、BAdI 等技术术语保留原文，不翻译。
3. 口语化短句，一句话说完；原文有多句就用中文分号或句号对应断开。
4. 只输出一个合法的 json 对象（JSON object），字段固定为 {"translation_zh": "..."}，不要解释、不要前后缀。"""


def translate_messages(text: str) -> list[dict]:
    return [{"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": str(text or "").strip()}]


def translate(text: str, cfg, opener=urlopen) -> str:
    """实战模式只做翻译，不生成答案。"""
    import persona
    raw = persona.call_llm(translate_messages(text), cfg, opener)
    try:
        data = json.loads(raw.strip().strip("`").replace("json", "", 1).strip())
        return str(data.get("translation_zh") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return str(json.loads(raw[start:end + 1]).get("translation_zh") or "").strip()
            except json.JSONDecodeError:
                pass
        return raw.strip()


REVIEW_SYSTEM = """你是面试复盘教练。根据下面这场面试的逐轮记录，输出中文复盘 Markdown。

结构固定为四节：## 问题清单（按轮列出，标明语种）、## 回答质量（挑 2–4 个具体问题，指出回答里缺的依据、数字或结果）、## 需要补的资料（人设和知识库里没有、导致回答含糊的事实）、## 下次练习重点（3 条以内，可执行）。

规则：只依据记录里出现过的内容，不臆测面试官意图，不编造候选人经历；没有回答记录的轮次按记录里的原因如实标注，不要猜测原因；不要写成鼓励式套话。"""


def review_messages(session: dict) -> list[dict]:
    lines = []
    for i, t in enumerate(session.get("turns", []), 1):
        lines.append("第 %d 轮 [%s] 原文：%s" %
                     (i, t.get("lang", ""), t.get("text", "")))
        if t.get("translation_zh"):
            lines.append("  译文：%s" % t["translation_zh"])
        if t.get("answer_zh"):
            lines.append("  中文回答：%s" % t["answer_zh"])
        if t.get("answer_en"):
            lines.append("  英文回答：%s" % t["answer_en"])
        if not (t.get("answer_zh") or t.get("answer_en")):
            lines.append("  （%s）" %
                         (t.get("skip_reason") or "本轮未生成回答"))
    return [{"role": "system", "content": REVIEW_SYSTEM},
            {"role": "user", "content": "\n".join(lines) or "（本场没有记录）"}]


# ---------------------------------------------------------------- 会话

class Session:
    """内存会话；练习模式结束落盘，实战模式只在内存，不写任何文件。"""

    def __init__(self, home):
        self.dir = Path(home) / "runtime" / "sessions"
        self.data = self._empty("practice")

    @staticmethod
    def _empty(mode: str) -> dict:
        return {"id": datetime.now().strftime("%Y%m%d-%H%M%S-%f"),
                "started_at": time.time(), "ended_at": None,
                "mode": mode, "active": False, "turns": []}

    def start(self, mode="practice") -> dict:
        self.data = self._empty("live" if mode == "live" else "practice")
        self.data["active"] = True
        return self.state()

    def reset(self) -> dict:
        """清空记录但不进入进行中状态。"""
        self.data = self._empty(self.mode)
        return self.state()

    @property
    def active(self) -> bool:
        return bool(self.data.get("active"))

    @property
    def mode(self) -> str:
        return self.data.get("mode", "practice")

    def add_turn(self, turn: dict) -> dict:
        item = {"seq": len(self.data["turns"]) + 1, "ts": time.time(),
                "lang": turn.get("lang", "en"),
                "text": str(turn.get("text") or "").strip(),
                "translation_zh": str(turn.get("translation_zh") or "").strip(),
                "answer_zh": str(turn.get("answer_zh") or "").strip(),
                "answer_en": str(turn.get("answer_en") or "").strip(),
                "degraded": bool(turn.get("degraded", False)),
                "generation_ms": turn.get("generation_ms"),
                "skip_reason": str(turn.get("skip_reason") or "").strip()}
        self.data["turns"].append(item)
        return item

    def history(self, limit=6) -> list[dict]:
        out = []
        for t in self.data["turns"][-limit:]:
            out.append({"role": "user", "content": t["text"]})
            if t["answer_zh"] or t["answer_en"]:
                out.append({"role": "assistant",
                            "content": t["answer_zh"] or t["answer_en"]})
        return out

    def state(self) -> dict:
        return dict(self.data, turns=list(self.data["turns"]),
                    count=len(self.data["turns"]))

    def end(self) -> dict:
        self.data["active"] = False
        self.data["ended_at"] = time.time()
        path = ""
        if self.mode == "practice" and self.data["turns"]:
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.dir / (self.data["id"] + ".json")
            target.write_text(json.dumps(self.state(), ensure_ascii=False, indent=2),
                              encoding="utf-8")
            path = str(target)
        return {"state": self.state(), "file": path}

    def export_md(self) -> str:
        d = self.state()
        ts = datetime.fromtimestamp(d["started_at"]).strftime("%Y-%m-%d %H:%M")
        out = ["# 面试记录 %s" % ts, "",
               "模式：%s　轮次：%d" %
               ("练习" if d["mode"] == "practice" else "实战", d["count"]), ""]
        for t in d["turns"]:
            out.append("## %d. [%s] %s" %
                       (t["seq"], "英文" if t["lang"] == "en" else "中文",
                        t["text"]))
            if t["translation_zh"]:
                out.append("")
                out.append("**译文**：" + t["translation_zh"])
            if t["answer_zh"]:
                out.append("")
                out.append("**中文回答**：" + t["answer_zh"])
            if t["answer_en"]:
                out.append("")
                out.append("**英文回答**：" + t["answer_en"])
            if not (t.get("answer_zh") or t.get("answer_en")):
                out.append("")
                out.append("*（%s）*" %
                           (t.get("skip_reason") or "本轮未生成回答"))
            out.append("")
        return "\n".join(out).rstrip() + "\n"
