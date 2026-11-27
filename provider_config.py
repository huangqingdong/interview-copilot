"""Speech / LLM provider 配置层。

providers.json 只存 ${ENV_VAR} 占位，真实密钥来自环境变量或 config/.env，
不写进任何配置文件。支持多套 profile、运行时热切换、配置文件改动自动重载。

标准库实现，无第三方依赖。接入层用法：

    from provider_config import ProviderConfig
    cfg = ProviderConfig()
    problems = cfg.check()            # 启动前校验
    speech = cfg.speech_sdk_kwargs()  # 喂给 Speech SDK
    llm = cfg.llm_request_kwargs()    # 喂给 OpenAI 兼容客户端
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "providers.json"
DOTENV_PATH = ROOT / "config" / ".env"

REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
AUTH_SCHEME = re.compile(r"^(?:Bearer|Basic)\s+", re.IGNORECASE)
ENV_OVERRIDE = {
    "profile": "COPILOT_PROFILE",
    "speech": "COPILOT_SPEECH",
    "llm": "COPILOT_LLM",
    "speech_key": "COPILOT_SPEECH_KEY",
    "llm_key": "COPILOT_LLM_KEY",
}
SECRET_KEYS = ("api_key", "subscription", "key", "token")
REQUEST_FIELDS = ("stt_headers", "stt_extra", "stt_json_template")
ADDRESS_FIELDS = ("stt_url", "base_url", "endpoint")
SECRET_LABELS = {
    "authorization", "proxyauthorization", "xauth", "cookie", "apikey", "key", "subscription",
    "subscriptionkey", "token", "accesstoken", "refreshtoken", "appkey",
    "accesskey", "secret", "clientsecret", "password", "credential", "sig", "signature",
}

# 页面上可编辑的字段；api_key 单独按密钥流程处理，不接受明文写进 providers.json
PROVIDER_FIELDS = {
    "speech": ("type", "stt_url", "base_url", "region", "endpoint",
               "model", "device", "stt_method", "stt_headers", "stt_body",
               "audio_field", "stt_extra", "stt_json_template",
               "response_path", "stt_lang_query", "note"),
    "llm": ("type", "base_url", "model", "api_version", "supports_json_mode",
            "temperature", "max_tokens", "timeout_s", "note"),
}
# 模型 Code 常带点号（qwen3.8-flash / kimi-k3.1-pro），直接拿它当服务 ID
# 才能和控制台对得上；环境变量名会另行把非字母数字替换成下划线。
ID_RE = re.compile(r"[A-Za-z0-9_.-]+")


class ConfigError(Exception):
    pass


class MissingSecret(ConfigError):
    pass


def _is_secret_label(name: str) -> bool:
    label = re.sub(r"[^a-z0-9]", "", name.lower())
    return label in SECRET_LABELS or label.endswith(("apikey", "subscriptionkey", "authtoken"))


def _literal_secret(value) -> bool:
    if not value:
        return False
    if not isinstance(value, str):
        return True
    value = AUTH_SCHEME.sub("", value.strip())
    return bool(value and not REF.fullmatch(value))


def _has_secret_value(obj) -> bool:
    if isinstance(obj, dict):
        return any((_is_secret_label(str(key)) and _literal_secret(value))
                   or _has_secret_value(value) for key, value in obj.items())
    if isinstance(obj, list):
        return any(_has_secret_value(value) for value in obj)
    if isinstance(obj, str):
        return bool(AUTH_SCHEME.match(obj.strip()) and _literal_secret(obj))
    return False


def inline_secret_fields(item: dict) -> list[str]:
    """只返回字段名，避免在校验错误里回显凭据。"""
    fields = [key for key, value in item.items()
              if _is_secret_label(key) and _literal_secret(value)]
    for key in REQUEST_FIELDS:
        raw = item.get(key)
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                if any(_is_secret_label(label) for label in re.findall(r'["\']([^"\']+)["\']\s*:', raw)):
                    fields.append(key)
                continue
        if _has_secret_value(raw):
            fields.append(key)
    for key in ADDRESS_FIELDS:
        value = item.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            parts = urlsplit(value)
        except ValueError:
            continue
        if (_literal_secret(parts.username) or _literal_secret(parts.password) or
                any(_is_secret_label(name) and _literal_secret(secret)
                    for name, secret in parse_qsl(parts.query))):
            fields.append(key)
    return fields


def _validate_secret_references(item: dict):
    fields = inline_secret_fields(item)
    if fields:
        raise ConfigError("请在「新密钥」里填写密钥，并在这些字段使用 ${环境变量名} 引用："
                          + "、".join(fields))


def public_provider(item: dict) -> dict:
    """可回显的原始配置，保留占位符并遮盖旧配置中内嵌的凭据。"""
    output = dict(item)
    for field in inline_secret_fields(item):
        output[field] = "<set:%d>" % len(str(item[field])) if field == "api_key" else "***"
    return output


def load_dotenv(path: Path = DOTENV_PATH) -> None:
    """读取 .env；已存在的环境变量优先，空值不写入，文件不存在则跳过。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value and name not in os.environ:
            os.environ[name] = value


def resolve(value, missing: list[str]):
    """递归替换 ${VAR}；缺失变量记为缺失并置空，由 check() 汇总报告。"""
    if isinstance(value, dict):
        return {k: resolve(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, missing) for v in value]
    if isinstance(value, str):
        def sub(match):
            name = match.group(1)
            got = os.environ.get(name, "")
            if not got:
                missing.append(name)
                return ""
            return got
        return REF.sub(sub, value)
    return value


def redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SECRET_KEYS:
                out[k] = f"<set:{len(str(v))}>" if v else ""
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _is_ref(raw_item, field):
    """字段在配置文件里是否为 ${VAR} 占位；占位导致的空值已由 missing 报告。"""
    value = raw_item.get(field, "")
    return isinstance(value, str) and REF.fullmatch(value) is not None


def _ref_names(raw_item, *fields):
    """取出这些字段引用的环境变量名，用于跳过「当前方案用不到的字段」。"""
    names = set()
    for field in fields:
        value = raw_item.get(field, "")
        match = REF.fullmatch(value) if isinstance(value, str) else None
        if match:
            names.add(match.group(1))
    return names


class ProviderConfig:
    def __init__(self, path=CONFIG_PATH, autoload_dotenv=True):
        self.path = Path(path)
        if autoload_dotenv:
            load_dotenv(self.path.parent / ".env")
        self._stamp = None
        self._raw = {}
        self._overrides = {}
        self._reload(force=True)

    def _reload(self, force=False):
        stat = self.path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if not force and stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件 JSON 有误: {self.path} -> {exc}") from exc
        for field in ("active", "profiles", "speech_providers", "llm_providers"):
            if field not in data:
                raise ConfigError(f"配置文件缺少字段: {field}")
        # 旧配置一次性迁移：把「当前方案」展开成两侧独立的生效项。
        # 迁移后 selection 才是唯一生效来源，profile 只是可套用的预设。
        if "selection" not in data:
            pair = data["profiles"].get(data.get("active"), {})
            data["selection"] = {"speech": pair.get("speech", ""),
                                 "llm": pair.get("llm", "")}
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        self._raw = data
        self._stamp = stamp

    @property
    def raw(self):
        self._reload()
        return self._raw

    @property
    def active_profile(self):
        return (self._overrides.get("profile")
                or os.environ.get(ENV_OVERRIDE["profile"])
                or self.raw["active"])

    def _ids(self):
        """两侧各自的生效来源：进程内覆盖 > 环境变量 > selection > profile 兜底。

        显式指定 COPILOT_PROFILE / set_overrides(profile=...) 视为「临时整体套用
        该方案」，此时以 profile 为准；正常情况只读 selection，改任一侧都不影响
        另一侧、也不改写任何 profile 定义。selection 里空字符串表示该侧停用。
        """
        profiles = self.raw["profiles"]
        name = self.active_profile
        if name not in profiles:
            raise ConfigError(f"未知 profile「{name}」，可用: {', '.join(profiles)}")
        pair = profiles[name]
        forced_profile = (self._overrides.get("profile")
                          or os.environ.get(ENV_OVERRIDE["profile"]))
        base = pair if forced_profile else self.raw.get("selection", pair)
        return (self._overrides.get("speech")
                or os.environ.get(ENV_OVERRIDE["speech"])
                or base.get("speech", ""),
                self._overrides.get("llm")
                or os.environ.get(ENV_OVERRIDE["llm"])
                or base.get("llm", ""))

    def set_overrides(self, profile=None, speech=None, llm=None):
        """进程内临时覆盖，不写文件；传 None 表示不覆盖该项。"""
        for k, v in (("profile", profile), ("speech", speech), ("llm", llm)):
            if v:
                self._overrides[k] = v
        return self

    def clear_overrides(self):
        self._overrides.clear()
        return self

    def _build(self, kind, strict=True):
        self._reload()
        speech_id, llm_id = self._ids()
        item_id = speech_id if kind == "speech" else llm_id
        if not item_id:          # 该侧被停用
            return {"id": "", "type": "disabled", "api_key": "",
                    "base_url": "", "model": "", "region": ""}, []
        pool = self.raw[f"{kind}_providers"]
        if item_id not in pool:
            raise ConfigError(
                f"未知 {kind} provider「{item_id}」，可用: {', '.join(pool)}")
        merged = dict(self.raw.get("defaults", {}).get(kind, {}))
        merged.update(copy.deepcopy(pool[item_id]))
        key_override = os.environ.get(ENV_OVERRIDE[f"{kind}_key"])
        if key_override:
            merged["api_key"] = key_override
        missing: list[str] = []
        cfg = resolve(merged, missing)
        cfg["id"] = item_id
        missing = sorted(set(missing) - self._unused_refs(kind, item_id, cfg))
        if strict and missing:
            raise MissingSecret(
                f"[{kind}:{item_id}] 缺少环境变量: {', '.join(missing)}")
        return cfg, missing

    def _unused_refs(self, kind, item_id, cfg):
        """当前 ASR 协议用不到的 ${VAR} 字段，其缺失不应算配置错误。"""
        if kind != "speech":
            return set()
        raw_item = self.raw["speech_providers"].get(item_id, {})
        stt_type = cfg.get("type")
        if stt_type == "azure_speech_translation" and (
                cfg.get("endpoint") or cfg.get("stt_url")):
            return _ref_names(raw_item, "region")
        if stt_type == "openai_compatible_stt":
            return _ref_names(raw_item, "region", "endpoint", "stt_url")
        if stt_type == "generic_http_stt":
            # 通用 HTTP 只认 stt_url；其它协议的字段都是模板残留，不该报缺环境变量
            return _ref_names(raw_item, "region", "endpoint", "base_url",
                              "model", "device")
        return set()

    @property
    def speech(self):
        return self._build("speech")[0]

    @property
    def llm(self):
        return self._build("llm")[0]

    def speech_sdk_kwargs(self):
        """喂给语音层的最小参数集；地址优先级 stt_url > endpoint > region。"""
        cfg = self.speech
        if cfg.get("type") == "disabled":
            return {"type": "disabled"}
        if cfg["type"] == "faster_whisper":
            return {"type": "faster_whisper",
                    "model": cfg.get("model", ""),
                    "device": cfg.get("device", "auto"),
                    "source_languages": cfg.get("source_languages", []),
                    "vad_silence_ms": cfg.get("vad_silence_ms", 500)}
        if cfg["type"] == "openai_compatible_stt":
            return {"type": cfg["type"],
                    "base_url": cfg.get("base_url", ""),
                    "stt_url": cfg.get("stt_url", ""),
                    "api_key": cfg.get("api_key", ""),
                    "model": cfg.get("model", ""),
                    "source_languages": cfg.get("source_languages", []),
                    "vad_silence_ms": cfg.get("vad_silence_ms", 500)}
        if cfg["type"] == "qwen_realtime_stt":
            return {k: cfg[k] for k in ("type", "stt_url", "api_key", "model",
                                       "source_languages", "vad_silence_ms") if k in cfg}
        if cfg["type"] == "generic_http_stt":
            kw = {"type": cfg["type"], "api_key": cfg.get("api_key", "")}
            for k in ("stt_url", "model", "stt_method", "stt_headers", "stt_body",
                      "audio_field", "stt_extra", "stt_json_template",
                      "response_path", "stt_lang_query", "source_languages",
                      "vad_silence_ms"):
                if k in cfg:
                    kw[k] = cfg[k]
            return kw
        kw = {"type": "azure_speech_translation", "subscription": cfg["api_key"]}
        if cfg.get("stt_url"):
            kw["stt_url"] = cfg["stt_url"]
        elif cfg.get("endpoint"):
            kw["endpoint"] = cfg["endpoint"]
        else:
            kw["region"] = cfg["region"]
        for k in ("source_languages", "translation_target", "phrase_list", "vad_silence_ms"):
            if k in cfg:
                kw[k] = cfg[k]
        return kw

    def llm_request_kwargs(self):
        """喂给 OpenAI 兼容客户端的最小参数集。"""
        cfg = self.llm
        keys = ("type", "base_url", "api_key", "model", "temperature",
                "max_tokens", "response_format", "timeout_s", "answer_schema", "api_version")
        return {k: cfg[k] for k in keys if k in cfg}

    def use(self, profile):
        """一键套用预设：两侧同时设为该方案的服务，之后仍可各自独立改。"""
        self._reload()
        if profile not in self._raw["profiles"]:
            raise ConfigError(
                f"未知 profile「{profile}」，可用: {', '.join(self._raw['profiles'])}")
        pair = self._raw["profiles"][profile]
        self._raw["active"] = profile
        self._raw["selection"] = {"speech": pair.get("speech", ""),
                                  "llm": pair.get("llm", "")}
        for k in ("speech", "llm"):
            self._overrides.pop(k, None)
        self._write()
        return self

    # ---------------------------------------------------------- 页面配置写回

    @property
    def dotenv_path(self):
        return self.path.parent / ".env"

    def _write(self):
        """把当前 _raw 落盘并重置热重载戳。"""
        for kind in PROVIDER_FIELDS:
            _validate_secret_references(self._raw.get("defaults", {}).get(kind, {}))
            for item in self._raw[kind + "_providers"].values():
                _validate_secret_references(item)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        self._stamp = None
        self._reload(force=True)

    def effective_ids(self):
        """实际生效的 (speech_id, llm_id)，含环境变量与进程内覆盖。"""
        return self._ids()

    def set_env_var(self, name, value):
        """写入 config/.env（保留其它行与注释），并立即注入当前进程。"""
        path = self.dotenv_path
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        out, replaced = [], False
        assignment = "%s=%s" % (name, json.dumps(str(value), ensure_ascii=False))
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                out.append(line)
                continue
            if stripped.split("=", 1)[0].strip() == name:
                out.append(assignment)
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(assignment)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.environ[name] = str(value)
        return path

    @staticmethod
    def _secret_env(kind, item_id):
        token = re.sub(r"[^A-Za-z0-9]", "_", str(item_id)).upper()
        return "COPILOT_%s_%s_KEY" % (kind.upper(), token)

    def set_provider(self, kind, item_id, data, secret="", persist_secret=True):
        """新增或更新一套服务；明文密钥转成 ${ENV} 占位，不写进 providers.json。"""
        if kind not in PROVIDER_FIELDS:
            raise ConfigError("未知服务类型：%s" % kind)
        item_id = str(item_id or "").strip()
        if not ID_RE.fullmatch(item_id):
            raise ConfigError("服务 ID 只能用英文字母、数字、下划线、连字符和点号")
        if not isinstance(data, dict):
            raise ConfigError("服务内容应为对象")
        self._reload()
        pool = self._raw["%s_providers" % kind]
        item = dict(pool.get(item_id, {}))
        for field in PROVIDER_FIELDS[kind]:
            if field not in data:
                continue
            value = data[field]
            # 页面回显时对写死的密钥打了码；没改动就保留服务端原值，别把 *** 写回去
            if field in ("stt_headers", "stt_extra", "stt_url",
                         "stt_json_template") and "***" in str(value):
                continue
            item[field] = value
        item.pop("id", None)
        item.setdefault("api_key", "")
        if not item.get("type"):
            item["type"] = ("openai_compatible_stt" if kind == "speech"
                            else "openai_compatible")
        if kind == "llm":
            item["supports_json_mode"] = bool(item.get("supports_json_mode", True))
        secret = str(secret or "").strip()
        if secret:
            env_name = self._secret_env(kind, item_id)
            item["api_key"] = "${%s}" % env_name
        _validate_secret_references(item)
        if secret:
            if persist_secret:
                self.set_env_var(env_name, secret)
            else:
                os.environ[env_name] = secret
        pool[item_id] = item
        self._write()
        return pool[item_id]

    def delete_provider(self, kind, item_id, drop_refs=False):
        """删除服务；drop_refs=True 时把引用它的方案该侧置为停用。

        只改 profiles 的定义，不动 active / selection，避免删一个模型把用户
        当前生效的 ASR / LLM 组合也改掉。
        """
        if kind not in PROVIDER_FIELDS:
            raise ConfigError("未知服务类型：%s" % kind)
        self._reload()
        pool = self._raw["%s_providers" % kind]
        if item_id not in pool:
            raise ConfigError("未知服务「%s」" % item_id)
        used = [name for name, pair in self._raw["profiles"].items()
                if pair.get(kind) == item_id]
        if used and not drop_refs:
            raise ConfigError("服务 %s 仍被方案引用（%s），请先修改这些方案"
                              % (item_id, ", ".join(used)))
        if self._raw.get("selection", {}).get(kind) == item_id:
            raise ConfigError("服务 %s 正在生效，请先在顶栏换一个服务或选「停用」"
                              % item_id)
        if len(pool) <= 1:
            raise ConfigError("至少保留一个 %s 服务" % kind)
        del pool[item_id]
        for name in used:
            self._raw["profiles"][name][kind] = ""
        self._write()

    def activate(self, kind, item_id):
        """只改本侧的生效项；不写 profile、不影响另一侧。

        item_id 为空 / none / __off__ 表示该侧停用，另一侧照常工作。
        """
        if kind not in PROVIDER_FIELDS:
            raise ConfigError("未知服务类型：%s" % kind)
        self._reload()
        item_id = "" if str(item_id).strip().lower() in (
            "", "none", "off", "__off__") else str(item_id).strip()
        if item_id and item_id not in self._raw["%s_providers" % kind]:
            raise ConfigError("未知服务「%s」" % item_id)
        sel = self._raw.setdefault("selection", {})
        sel[kind] = item_id
        self._overrides.pop(kind, None)
        self._write()
        return dict(sel)

    def set_profile(self, name, speech, llm, description=""):
        """新增或覆盖一个方案，并设为当前。"""
        self._reload()
        name = str(name or "").strip()
        if not ID_RE.fullmatch(name):
            raise ConfigError("方案名只能用英文字母、数字、下划线和连字符")
        def norm(kind, value):
            raw = str(value or "").strip()
            if raw.lower() in ("", "none", "off", "__off__"):
                return ""          # 该侧停用，另一侧照常生效
            if raw not in self._raw["%s_providers" % kind]:
                raise ConfigError("未知 %s 服务「%s」" % (kind, raw))
            return raw
        speech, llm = norm("speech", speech), norm("llm", llm)
        self._raw["profiles"][name] = {
            "speech": speech, "llm": llm,
            "description": str(description or "")}
        self._raw["active"] = name
        # 「保存并启用」同时把两侧独立生效项指向本方案
        self._raw["selection"] = {"speech": speech, "llm": llm}
        self._write()
        return name

    def delete_profile(self, name):
        self._reload()
        profiles = self._raw["profiles"]
        if name not in profiles:
            raise ConfigError("未知方案「%s」" % name)
        if len(profiles) <= 1:
            raise ConfigError("至少保留一个方案")
        del profiles[name]
        if self._raw["active"] == name:
            self._raw["active"] = next(iter(profiles))
        self._write()

    def secrets(self):
        """各服务密钥的来源与状态，只返回长度，不返回明文。"""
        self._reload()
        out = {}
        for kind in ("speech", "llm"):
            for item_id, item in self._raw["%s_providers" % kind].items():
                ref = str(item.get("api_key") or "")
                match = REF.fullmatch(ref)
                env_name = match.group(1) if match else ""
                value = os.environ.get(env_name, "") if env_name else ref
                out["%s:%s" % (kind, item_id)] = {
                    "env": env_name, "set": bool(value),
                    "length": len(value), "inline": bool(value and not env_name)}
        return out

    def check(self):
        """返回问题列表；空列表表示当前组合可用。"""
        self._reload()
        problems = []
        try:
            speech_id, llm_id = self._ids()
        except ConfigError as exc:
            return [str(exc)]
        for kind, item_id in (("speech", speech_id), ("llm", llm_id)):
            if not item_id:      # 停用是显式选择，不算配置问题
                continue
            try:
                cfg, missing = self._build(kind, strict=False)
            except ConfigError as exc:
                problems.append(str(exc))
                continue
            t = cfg.get("type")
            raw_item = self.raw[f"{kind}_providers"].get(item_id, {})
            # 字段里写了 ${VAR} 但当前地址方案用不到时，不该报成「缺环境变量」。
            # 典型场景：只用 stt_url / endpoint 自定义地址，region 仍是模板占位。
            unused_refs = self._unused_refs(kind, item_id, cfg)
            for name in missing:
                if name not in unused_refs:
                    problems.append(f"[{kind}:{item_id}] 缺少环境变量 {name}")

            if kind == "speech" and t == "azure_speech_translation":
                if not cfg.get("api_key") and not _is_ref(raw_item, "api_key"):
                    problems.append(f"[speech:{item_id}] api_key 为空")
                if (not (cfg.get("region") or cfg.get("endpoint") or cfg.get("stt_url"))
                        and not _is_ref(raw_item, "region")):
                    problems.append(
                        f"[speech:{item_id}] region / endpoint / 完整识别地址至少填一个")
            elif kind == "speech" and t == "openai_compatible_stt":
                for field in ("api_key", "model"):
                    if not cfg.get(field) and not _is_ref(raw_item, field):
                        problems.append(f"[speech:{item_id}] {field} 为空")
                if not (cfg.get("base_url") or cfg.get("stt_url")) \
                        and not _is_ref(raw_item, "base_url"):
                    problems.append(f"[speech:{item_id}] base_url / stt_url 至少填一个")
            elif kind == "speech" and t == "generic_http_stt":
                if not cfg.get("stt_url") and not _is_ref(raw_item, "stt_url"):
                    problems.append(f"[speech:{item_id}] 缺少完整识别地址 stt_url")
                # 通用 HTTP 不预设鉴权方式：密钥可放 headers / query，也可完全不需要
                for field in ("stt_headers", "stt_extra"):
                    value = str(cfg.get(field) or "").strip()
                    if value:
                        try:
                            if not isinstance(json.loads(value), dict):
                                problems.append(f"[speech:{item_id}] {field} 需为 JSON 对象")
                        except (TypeError, ValueError):
                            problems.append(f"[speech:{item_id}] {field} 不是合法 JSON")
                if str(cfg.get("stt_body") or "").strip() == "json_template":
                    tpl = str(cfg.get("stt_json_template") or "").strip()
                    if not tpl:
                        problems.append(f"[speech:{item_id}] 缺少请求体模板 stt_json_template")
                    else:
                        try:
                            if not isinstance(json.loads(tpl), dict):
                                problems.append(
                                    f"[speech:{item_id}] stt_json_template 需为 JSON 对象")
                        except (TypeError, ValueError):
                            problems.append(
                                f"[speech:{item_id}] stt_json_template 不是合法 JSON；"
                                "音频占位符要写在引号内")
            elif kind == "speech" and t == "faster_whisper":
                if not cfg.get("model"):
                    problems.append(f"[speech:{item_id}] 缺少 model")
            elif kind == "speech" and t == "qwen_realtime_stt":
                from interview import InterviewError, realtime_stt_url
                try:
                    realtime_stt_url(cfg)
                except InterviewError as exc:
                    problems.append(f"[speech:{item_id}] {exc}")
            elif kind == "speech":
                problems.append(f"[speech:{item_id}] 不支持的语音服务类型 {t}")
            else:
                for field in ("base_url", "api_key", "model"):
                    if not cfg.get(field) and not _is_ref(raw_item, field):
                        problems.append(f"[llm:{item_id}] {field} 为空")
                if not cfg.get("supports_json_mode"):
                    problems.append(
                        f"[llm:{item_id}] 警告：未声明 supports_json_mode，中英双版结构化输出可能失败")
        return problems

    def snapshot(self):
        """脱敏后的当前生效配置，可直接渲染到 UI。"""
        self._reload()
        try:
            self._ids()
        except ConfigError as exc:
            return {"error": str(exc)}
        view = {"profile": self.active_profile}
        for kind in PROVIDER_FIELDS:
            cfg = self._build(kind, strict=False)[0]
            raw = dict(self.raw.get("defaults", {}).get(kind, {}))
            raw.update(self.raw[kind + "_providers"].get(cfg["id"], {}))
            public = public_provider(raw)
            visible = redact(cfg)
            for key in (*REQUEST_FIELDS, *ADDRESS_FIELDS, "note"):
                if key in public:
                    visible[key] = public[key]
            view[kind] = visible
        return view


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Speech / LLM 独立配置（密钥只从环境变量读取）",
        epilog="两侧各自独立生效：select 单独改一侧，use 一键套用预设，off 停用。"
               "临时覆盖（不写文件）：COPILOT_PROFILE / COPILOT_SPEECH / COPILOT_LLM "
               "/ COPILOT_SPEECH_KEY / COPILOT_LLM_KEY")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出独立生效项、预设与 provider")
    p_use = sub.add_parser("use", help="一键套用预设（同时设置两侧）")
    p_use.add_argument("profile", nargs="?", help="profile 名；省略则显示当前")
    p_sel = sub.add_parser("select", help="单独设置某一侧，另一侧不受影响")
    p_sel.add_argument("--speech", help="语音服务 ID 或 off")
    p_sel.add_argument("--llm", help="模型服务 ID 或 off")
    sub.add_parser("show", help="显示当前生效配置（密钥脱敏）")
    sub.add_parser("check", help="校验当前配置与密钥是否齐全")
    args = parser.parse_args(argv)

    try:
        cfg = ProviderConfig()
        if args.cmd == "list":
            sel = cfg.raw.get("selection", {})
            print("当前生效  speech=%s  llm=%s" %
                  (sel.get("speech") or "已停用", sel.get("llm") or "已停用"))
            print(f"最近套用的预设: {cfg.active_profile}\n")
            print("profiles（预设，选中即套用两侧）:")
            for name, p in cfg.raw["profiles"].items():
                mark = "*" if name == cfg.active_profile else " "
                print(f"  {mark} {name:<20} speech={p['speech']:<14} "
                      f"llm={p['llm']:<10} {p.get('description', '')}")
            for kind in ("speech", "llm"):
                print(f"\n{kind}_providers: " + ", ".join(cfg.raw[f"{kind}_providers"]))
        elif args.cmd == "use":
            if args.profile:
                cfg.use(args.profile)
                print(f"switched to: {args.profile}")
            else:
                print(f"current profile: {cfg.active_profile}")
        elif args.cmd == "select":
            for kind, value in (("speech", args.speech), ("llm", args.llm)):
                if value is not None:
                    cfg.activate(kind, value)
            print(json.dumps(cfg.raw.get("selection", {}),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "show":
            print(json.dumps(cfg.snapshot(), ensure_ascii=False, indent=2))
        elif args.cmd == "check":
            snap = cfg.snapshot()
            if "error" in snap:
                print(f"配置错误: {snap['error']}", file=sys.stderr)
                return 2
            print(f"preset: {snap['profile']}  "
                  f"speech: {snap['speech']['id'] or '已停用'}  "
                  f"llm: {snap['llm']['id'] or '已停用'}")
            problems = cfg.check()
            if problems:
                print("问题:")
                for item in problems:
                    print(f"  - {item}")
                return 1
            print("OK：当前配置与密钥齐全")
    except (ConfigError, OSError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
