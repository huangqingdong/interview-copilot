"""配置层 + 人设层自检。不访问网络，不需要真实 key，可反复运行。

    python selftest.py
"""
from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import sys
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener as make_opener
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import persona
from provider_config import REF, ProviderConfig

FAKE_ENV = {
    "AZURE_SPEECH_KEY_CN": "fake-speech",
    "AZURE_SPEECH_REGION_CN": "chinaeast2",
    "QWEN_API_KEY": "fake-llm",
    "QWEN_BASE_URL": "https://example.invalid/v1",
    "QWEN_MODEL": "qwen-test",
    # 页面填过 key 后 providers.json 会改成 ${COPILOT_<KIND>_<ID>_KEY} 占位，
    # 并由真实 config/.env 提供值。这里用假值顶掉它，自检不读、也不打印真实密钥。
    "COPILOT_SPEECH_AZURE_CN_KEY": "fake-speech",
    "COPILOT_LLM_QWEN_KEY": "fake-llm",
    # 自检不依赖用户当前在页面上选了哪个服务，也不改写它的生效项
    "COPILOT_SPEECH": "azure-cn",
    "COPILOT_LLM": "qwen",
}

results: list[tuple[str, bool, str]] = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), str(detail)))


@contextmanager
def env(**values):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update({k: str(v) for k, v in values.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_opener(req, timeout=None):
    fake_opener.body = json.loads(req.data.decode("utf-8"))
    fake_opener.url = req.full_url
    return FakeResponse({"choices": [{"message": {"content":
        '{"answer_zh": "中文稿", "answer_en": "English draft"}'}}]})


# ---- provider 配置层 ----
with env(**FAKE_ENV):
    cfg = ProviderConfig()
    check("provider: 当前组合校验通过", cfg.check() == [], cfg.check())
    check("provider: speech kwargs 含术语表", "phrase_list" in cfg.speech_sdk_kwargs())
    check("provider: llm 输出 json_object",
          cfg.llm_request_kwargs().get("response_format") == "json_object")
    check("provider: snapshot 密钥脱敏",
          "<set:" in json.dumps(cfg.snapshot(), ensure_ascii=False))

    # 国内 LLM 预设：base_url / model 必须是字面默认值，不能依赖 .env 里的变量，
    # 否则「只在配置页填密钥」这条路会跑不通。
    pool = cfg.raw["llm_providers"]
    domestic = ("qwen", "deepseek", "glm", "doubao")
    check("provider: 内置 4 家国内 LLM", all(k in pool for k in domestic),
          list(pool))
    check("provider: 国内端点与模型为字面默认值",
          all("${" not in str(pool[k].get("base_url", ""))
              and "${" not in str(pool[k].get("model", ""))
              and pool[k].get("base_url", "").startswith("https://")
              for k in domestic))
    saved = {k: os.environ.pop(k, None)
             for k in ("QWEN_BASE_URL", "QWEN_MODEL", "DEEPSEEK_BASE_URL",
                       "DEEPSEEK_MODEL")}
    try:
        check("provider: 只填密钥即可用（不依赖 env 里的 URL / 模型名）",
              ProviderConfig().check() == [], ProviderConfig().check())
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    with env(GLM_API_KEY="fake-glm", ARK_API_KEY="fake-ark",
             DEEPSEEK_API_KEY="fake-ds"):
        urls = {}
        for k in domestic:
            c = ProviderConfig().set_overrides(llm=k)
            urls[k] = persona.build_request(
                [{"role": "user", "content": "x"}], c)[0]
        check("provider: 四家国内端点都能构造请求",
              all(u.endswith("/chat/completions")
                  and "//chat/completions" not in u for u in urls.values()),
              urls)

    # 百炼模型共用端点，每个模型可独立指定密钥。
    bailian_llm = ("qwen3.8-27b", "qwen3.8-flash", "kimi-k3",
                   "kimi-k3.1-pro", "qwen3.8-max-0902",
                   "deepseek-v4.1-flash-1017", "deepseek-v4.1-flash")
    check("provider: 百炼系 7 个模型预置共用端点并引用环境变量密钥",
          all(k in pool for k in bailian_llm)
          and all(pool[k].get("model") == k
                  and pool[k].get("type") == "openai_compatible"
                  and pool[k].get("base_url") == pool["qwen"]["base_url"]
                  and bool(REF.fullmatch(pool[k].get("api_key", "")))
                  and pool[k].get("supports_json_mode") is True
                  for k in bailian_llm),
          [k for k in bailian_llm if k not in pool])
    # note 也会被 resolve() 递归展开；写 ${KEY} 会把明文密钥带进页面回显。
    check("provider: 说明文字不含 ${...} 占位，避免密钥泄漏到页面",
          all("${" not in str(pool[k].get("note", "")) for k in bailian_llm))
    with env(COPILOT_LLM_QWEN_KEY="fake-qwen"):
        built = {}
        for k in bailian_llm:
            c = ProviderConfig().set_overrides(llm=k)
            u, _, b = persona.build_request(
                [{"role": "user", "content": "x"}], c)
            built[k] = (u, b.get("model"), b.get("response_format"),
                        b.get("temperature"))
        check("provider: 7 个百炼模型请求地址 / model / JSON mode 正确",
              all(u == pool["qwen"]["base_url"] + "/chat/completions"
                  and m == k and rf == {"type": "json_object"}
                  for k, (u, m, rf, _) in built.items()), built)
        check("provider: Kimi 固定 temperature=0（其余值百炼返回 400）",
              all(built[k][3] == 0 for k in ("kimi-k3", "kimi-k3.1-pro"))
              and all(built[k][3] == 0.3 for k in bailian_llm
                      if not k.startswith("kimi")), built)

# ---- Speech / LLM 解耦（临时目录，不碰真实 selection）----
import tempfile

with tempfile.TemporaryDirectory() as td:
    tdp = Path(td) / "config"
    tdp.mkdir()
    shutil.copy(ROOT / "config" / "providers.json", tdp / "providers.json")
    c = ProviderConfig(path=tdp / "providers.json")
    check("解耦: 配置带独立 selection",
          set(c.raw.get("selection", {})) == {"speech", "llm"},
          c.raw.get("selection"))
    c.set_provider("llm", "qwen3.8-flash", {"model": "qwen3.8-flash"})
    check("provider: 含点号的模型 Code 可作为服务 ID 编辑保存",
          "qwen3.8-flash" in ProviderConfig(
              path=tdp / "providers.json").raw["llm_providers"])
    original_profile = c.active_profile
    c.use("offline")
    check("provider: use 切换持久生效",
          ProviderConfig(path=tdp / "providers.json").active_profile == "offline")
    c.use(original_profile)
    preset = c.raw["profiles"][c.active_profile]
    c.activate("llm", "glm")
    check("解耦: 只切模型不碰语音",
          c.effective_ids() == (preset["speech"], "glm"), c.effective_ids())
    check("解耦: 不改写方案定义",
          c.raw["profiles"][c.active_profile]["llm"] == preset["llm"])
    c.activate("speech", "off")
    check("解耦: 停用语音不影响模型", c.effective_ids() == ("", "glm"))
    check("解耦: 停用侧不算配置问题",
          all("[speech:" not in x for x in c.check()), c.check())
    check("解耦: 停用侧 SDK 参数安全",
          c.speech_sdk_kwargs() == {"type": "disabled"})
    c.use("azure-cn-deepseek")
    check("解耦: 套用预设同时设两侧",
          c.raw["selection"] == {"speech": "azure-cn", "llm": "deepseek"})
    c.activate("llm", "__off__")
    check("解耦: 停用模型不影响语音", c.effective_ids() == ("azure-cn", ""))

# ---- 语音服务地址自定义（临时配置，不碰真实 selection）----
with tempfile.TemporaryDirectory() as td_addr:
    p_addr = Path(td_addr) / "providers.json"
    raw_addr = json.loads((ROOT / "config" / "providers.json").read_text(
        encoding="utf-8"))
    raw_addr["speech_providers"]["azure-cn"]["region"] = "${UNSET_REGION_VAR}"
    raw_addr["speech_providers"]["azure-cn"]["endpoint"] = \
        "https://xxx.cognitiveservices.azure.cn/"
    p_addr.write_text(json.dumps(raw_addr, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    with env(**FAKE_ENV):
        c_addr = ProviderConfig(path=p_addr)
        check("配置: 用 endpoint 时不误报缺 region",
              "UNSET_REGION_VAR" not in json.dumps(
                  c_addr.check(), ensure_ascii=False), c_addr.check())
        check("配置: 严格解析也能只靠 endpoint 构造语音参数",
              c_addr.speech["type"] == "azure_speech_translation")
    raw_addr["speech_providers"]["azure-cn"]["endpoint"] = ""
    raw_addr["speech_providers"]["azure-cn"]["stt_url"] = \
        "https://gw.example.invalid/stt/v1"
    p_addr.write_text(json.dumps(raw_addr, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    with env(**FAKE_ENV):
        c_url = ProviderConfig(path=p_addr)
        check("配置: 只填完整识别地址时严格解析不报缺 region",
              c_url.speech.get("stt_url", "").startswith("https://gw.")
              and not [x for x in c_url.check() if "UNSET_REGION_VAR" in x],
              c_url.check())
    c_addr.set_provider("speech", "custom-stt", {
        "type": "openai_compatible_stt",
        "base_url": "https://asr.example.invalid/v1",
        "model": "sensevoice", "stt_url": ""}, secret="sk-stt")
    c_addr.activate("speech", "custom-stt")
    item_addr = c_addr.raw["speech_providers"]["custom-stt"]
    check("配置: 页面可写入 stt_url / base_url 字段",
          item_addr["base_url"] == "https://asr.example.invalid/v1"
          and item_addr["model"] == "sensevoice"
          and item_addr["api_key"] == "${COPILOT_SPEECH_CUSTOM_STT_KEY}",
          item_addr)
    check("配置: 非 Azure 语音服务不误报 region / endpoint",
          not [x for x in c_addr.check() if "region" in x or "endpoint" in x],
          c_addr.check())

# ---- ASR / LLM 解耦：非 Azure 协议、单侧停用方案（临时配置）----
with tempfile.TemporaryDirectory() as td_asr:
    import interview as iv_types
    p_asr = Path(td_asr) / "providers.json"
    raw_asr = json.loads((ROOT / "config" / "providers.json").read_text(
        encoding="utf-8"))
    p_asr.write_text(json.dumps(raw_asr, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    # 清空进程级 service 覆盖，让 selection 真正生效
    clean = dict(FAKE_ENV, COPILOT_SPEECH="", COPILOT_LLM="")
    with env(**clean):
        c_asr = ProviderConfig(path=p_asr)
        types = iv_types.asr_types()
        check("解耦: ASR 协议清单含通用 HTTP",
              "generic_http_stt" in types and "azure_speech_translation" in types
              and types["generic_http_stt"]["available"] is True)
        check("解耦: 预设含非 Azure 组合与单侧停用",
              raw_asr["profiles"]["generic-asr-deepseek"]["speech"] == "generic-asr"
              and raw_asr["profiles"]["text-only-qwen"]["speech"] == ""
              and raw_asr["profiles"]["asr-only-qwen"]["llm"] == "")
        check("百炼: qwen3-asr-flash 预设与组合方案已注册",
              raw_asr["speech_providers"]["bailian-qwen-asr"]["stt_body"]
              == "json_template"
              and "chat/completions" in
              raw_asr["speech_providers"]["bailian-qwen-asr"]["stt_url"]
              and raw_asr["speech_providers"]["bailian-qwen-asr"]["response_path"]
              == "choices.0.message.content"
              and raw_asr["profiles"]["bailian-qwen-deepseek"]["speech"]
              == "bailian-qwen-asr")
        check("百炼: ASR 协议清单登记模板字段",
              "stt_json_template" in
              iv_types.asr_types()["generic_http_stt"]["fields"]
              and "model" in
              iv_types.asr_types()["generic_http_stt"]["fields"])
        c_asr.set_provider("speech", "model-asr", {
            "type": "generic_http_stt", "stt_url": "http://x/asr",
            "stt_body": "json_template", "model": "new-model",
            "stt_json_template": '{"model": "old-model", "audio": "{{audio_base64}}"}',
            "region": "", "endpoint": ""})
        c_asr.activate("speech", "model-asr")
        check("ASR 联动: 配置层把顶层 model 透传给语音层",
              c_asr.speech.get("model") == "new-model"
              and c_asr.speech_sdk_kwargs().get("model") == "new-model",
              c_asr.speech_sdk_kwargs())
        before_llm = c_asr.effective_ids()[1]
        c_asr.set_provider("speech", "my-asr", {
            "type": "generic_http_stt",
            "stt_url": "http://127.0.0.1:9000/recognize",
            "stt_method": "POST", "stt_body": "multipart",
            "stt_headers": "{\"Authorization\": \"Bearer ${COPILOT_SPEECH_MY_ASR_KEY}\"}",
            "stt_extra": "{\"model\": \"paraformer\"}",
            "response_path": "data.text", "stt_lang_query": "lang",
            "region": "", "endpoint": ""}, secret="sk-generic")
        c_asr.activate("speech", "my-asr")
        item_asr = c_asr.raw["speech_providers"]["my-asr"]
        check("解耦: 通用 HTTP ASR 字段全部落盘",
              item_asr["stt_url"].endswith("/recognize")
              and item_asr["response_path"] == "data.text"
              and item_asr["api_key"] == "${COPILOT_SPEECH_MY_ASR_KEY}",
              item_asr)
        check("解耦: 换 ASR 不影响另一侧生效项",
              c_asr.effective_ids() == ("my-asr", before_llm),
              c_asr.effective_ids())
        snapshot = json.dumps(c_asr.snapshot(), ensure_ascii=False)
        check("provider: 状态快照保留请求头引用，不回显解析后的密钥",
              "${COPILOT_SPEECH_MY_ASR_KEY}" in snapshot and "sk-generic" not in snapshot)
        probs_asr = c_asr.check()
        check("解耦: 通用 HTTP 不误报 region / endpoint / base_url",
              not [x for x in probs_asr if any(
                  k in x for k in ("region", "endpoint", "base_url"))], probs_asr)
        c_asr.set_provider("speech", "my-asr", {
            "stt_headers": "{\"Authorization\": \"***\"}"})
        check("解耦: 脱敏值不会覆盖服务端原值",
              "${COPILOT_SPEECH_MY_ASR_KEY}" in
              c_asr.raw["speech_providers"]["my-asr"]["stt_headers"])
        c_asr.set_profile("one-side", "__off__", "qwen", "只开 LLM")
        check("解耦: 方案可保存单侧停用",
              c_asr.raw["profiles"]["one-side"] == {
                  "speech": "", "llm": "qwen", "description": "只开 LLM"},
              c_asr.raw["profiles"]["one-side"])
        c_asr.set_provider("speech", "bad-json", {
            "type": "generic_http_stt", "stt_url": "http://x/asr",
            "stt_headers": "{oops"})
        c_asr.activate("speech", "bad-json")
        check("解耦: 非法请求头 JSON 被 check 报出",
              any("stt_headers" in x for x in c_asr.check()), c_asr.check())
        tpl = '{"audio": "{{audio_data_uri}}"}'
        c_asr.set_provider("speech", "tpl-asr", {
            "type": "generic_http_stt", "stt_url": "http://x/asr",
            "stt_body": "json_template", "stt_json_template": tpl})
        c_asr.activate("speech", "tpl-asr")
        check("百炼: JSON 请求体模板可落盘且校验通过",
              c_asr.raw["speech_providers"]["tpl-asr"]["stt_json_template"] == tpl
              and not [x for x in c_asr.check() if "stt_json_template" in x],
              c_asr.check())
        c_asr.set_provider("speech", "tpl-bad", {
            "type": "generic_http_stt", "stt_url": "http://x/asr",
            "stt_body": "json_template", "stt_json_template": "{oops"})
        c_asr.activate("speech", "tpl-bad")
        check("百炼: 非法请求体模板被 check 报出",
              any("stt_json_template" in x for x in c_asr.check()), c_asr.check())

        # ---- 模型管理：页面提交的常用参数、新增与删除 ----
        c_asr.set_provider("llm", "my-qwen", {
            "type": "openai_compatible",
            "base_url": "https://example.invalid/v1",
            "model": "qwen-test", "supports_json_mode": True,
            "temperature": 0.4, "max_tokens": 800, "timeout_s": 30})
        mm_item = c_asr.raw["llm_providers"]["my-qwen"]
        check("模型管理: LLM 常用参数按数值落盘",
              mm_item["temperature"] == 0.4
              and mm_item["max_tokens"] == 800
              and mm_item["timeout_s"] == 30
              and mm_item["model"] == "qwen-test", mm_item)
        c_asr.set_provider("speech", "my-asr-copy", {
            "type": "generic_http_stt", "model": "qwen3-asr-flash",
            "stt_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "stt_method": "POST", "stt_body": "json_template",
            "stt_headers": '{"Authorization": "Bearer ${COPILOT_SPEECH_MY_ASR_COPY_KEY}"}',
            "stt_json_template": '{"model": "qwen3-asr-flash"}',
            "response_path": "choices.0.message.content"}, secret="sk-copy")
        check("模型管理: 复制模型带全套参数且密钥独立",
              c_asr.raw["speech_providers"]["my-asr-copy"]["stt_body"]
              == "json_template"
              and c_asr.raw["speech_providers"]["my-asr-copy"]["api_key"]
              == "${COPILOT_SPEECH_MY_ASR_COPY_KEY}")
        c_asr.delete_provider("speech", "my-asr-copy")
        c_asr.delete_provider("llm", "my-qwen")
        check("模型管理: 自建模型可删除",
              "my-asr-copy" not in c_asr.raw["speech_providers"]
              and "my-qwen" not in c_asr.raw["llm_providers"])
        cur_llm = c_asr.effective_ids()[1]
        try:
            c_asr.delete_provider("llm", cur_llm)
            rejected = False
        except Exception:
            rejected = True
        check("模型管理: 生效中的模型拒绝删除", rejected, cur_llm)

        # 被方案引用：默认仍拒绝；drop_refs 时清理方案定义但不碰生效项
        before_sel = dict(c_asr.raw["selection"])
        c_asr.set_profile("mm-ref", "my-asr", "deepseek", "引用测试")
        c_asr.activate("speech", before_sel["speech"])
        c_asr.activate("llm", before_sel["llm"])
        try:
            c_asr.delete_provider("speech", "my-asr")
            guard = False
        except Exception:
            guard = True
        check("模型管理: 不清理引用时仍拒绝删除", guard)
        c_asr.delete_provider("speech", "my-asr", drop_refs=True)
        check("模型管理: 清理引用后可删除且生效项不变",
              "my-asr" not in c_asr.raw["speech_providers"]
              and c_asr.raw["profiles"]["mm-ref"]["speech"] == ""
              and c_asr.raw["profiles"]["mm-ref"]["llm"] == "deepseek"
              and c_asr.raw["selection"] == before_sel,
              c_asr.raw["selection"])

# ---- 人设层 ----
persona_fixture = tempfile.TemporaryDirectory()
persona_path = Path(persona_fixture.name) / "persona.json"
store = persona.PersonaStore(path=persona_path)
store.init()
check("persona: 模板可加载", len(store.list()) >= 2)

current = store.current()
check("persona: 当前人设有关键经历", bool(current.get("highlights")))

msgs_en = persona.build_messages(
    "Tell me about a complex delivery you led", source_lang="en", store=store)
system_en = msgs_en[0]["content"]
check("persona: 人设注入 system prompt",
      current["label"] in system_en and "边界与约束" in system_en)
check("persona: 英文轮要求中英双版", "两版都要给" in system_en)
check("persona: 明令禁止编造经历", "不得编造" in system_en)

# DeepSeek 等国内端点的 json_object 模式要求 prompt 里出现小写 "json"，
# 否则会直接返回 400；三条用 JSON mode 的链路都要覆盖到。
import interview as iv

check("prompt: 回答链路含小写 json", "json" in system_en)
check("prompt: 翻译链路含小写 json",
      "json" in iv.translate_messages("Hi")[0]["content"])
check("prompt: 人设生成链路含小写 json", "json" in persona.BUILD_SYSTEM)

msgs_zh = persona.build_messages(
    "介绍一下你主导过的复杂交付", source_lang="zh", store=store)
check("persona: 中文轮只出中文", "固定为空字符串" in msgs_zh[0]["content"])
check("persona: 问题原文进 user", "介绍一下你主导过的复杂交付" in msgs_zh[1]["content"])

with env(COPILOT_PERSONA="btp-cap-dev"):
    other = persona.build_messages("What is CAP?", source_lang="en", store=store)
check("persona: 环境变量临时换人设",
      "BTP CAP" in other[0]["content"] and other[0]["content"] != system_en)

original_persona = store.active
store.use("btp-cap-dev")
check("persona: use 切换持久生效", persona.PersonaStore(path=persona_path).active == "btp-cap-dev")
store.use(original_persona)

# ---- 回答解析容错 ----
r1 = persona.parse_answer('{"answer_zh": "中文", "answer_en": "English"}', "en")
check("parse: 标准 JSON", r1["answer_zh"] == "中文"
      and r1["answer_en"] == "English" and not r1["degraded"])
r2 = persona.parse_answer(
    '```json\n{"answer_zh": "中文", "answer_en": "English"}\n```', "en")
check("parse: 去掉代码围栏", not r2["degraded"] and r2["answer_en"] == "English")
r3 = persona.parse_answer("模型直接给了纯文本", "en")
check("parse: 纯文本降级不抛异常",
      r3["degraded"] and r3["answer_zh"] == "模型直接给了纯文本")
r4 = persona.parse_answer('{"answer_zh": "中文", "answer_en": "English"}', "zh")
check("parse: 中文轮丢弃英文版", r4["answer_en"] == "" and r4["answer_zh"] == "中文")


def no_envelope(result):
    joined = " ".join(str(result.get(k) or "") for k in
                      ("translation_zh", "answer_zh", "answer_en"))
    return not any(k in joined for k in
                   ("translation_zh", "answer_zh", "answer_en"))


# 推理档模型 reasoning 吃光 max_tokens 时正文只剩半截 JSON，旧逻辑把整段
# 报文塞进 answer_zh，界面会露出 translation_zh: 这类字段名。
r5 = persona.parse_answer(
    '{"translation_zh":"请谈谈你在 SAP 项目中的经验。",'
    '"answer_zh":"我在零售行业做了十年 ABAP', "en")
r6 = persona.parse_answer(
    '[translation_zh:"考试。一旦报名，24 小时窗口就开始了。",'
    '"answer_zh":"这个具体流程我没有直接做过', "en")
check("parse: 半截 JSON 按字段抢救，译文与回答各归其位",
      r5["degraded"] and r5["translation_zh"].startswith("请谈谈")
      and r5["answer_zh"].startswith("我在零售") and no_envelope(r5))
check("parse: 字段名裸露时不把报文结构显示给用户",
      r6["degraded"] and r6["translation_zh"].startswith("考试")
      and r6["answer_zh"].startswith("这个具体") and no_envelope(r6))
check("配置: 默认 max_tokens 足够推理档模型输出正文",
      json.loads((ROOT / "config" / "providers.json")
                 .read_text(encoding="utf-8"))["defaults"]["llm"]["max_tokens"]
      >= 1200)

# ---- 请求构造与端到端 mock ----
with env(**FAKE_ENV):
    cfg = ProviderConfig()
    url, headers, body = persona.build_request(msgs_en, cfg)
    check("request: OpenAI 兼容 URL",
          url == cfg.llm["base_url"].rstrip("/") + "/chat/completions", url)
    check("request: Bearer 鉴权", headers.get("Authorization") == "Bearer fake-llm")
    check("request: JSON mode", body.get("response_format") == {"type": "json_object"})
    _, _, plain_body = persona.build_request(msgs_en, cfg, json_mode=False)
    check("request: 复盘可关闭强制 JSON 输出", "response_format" not in plain_body)
    answer = persona.generate_answer(
        "Tell me about a complex delivery you led", cfg=cfg, store=store,
        source_lang="en", opener=fake_opener)
    check("端到端: mock 返回中英双版",
          answer["answer_zh"] == "中文稿" and answer["answer_en"] == "English draft")
    check("端到端: 人设随请求发出",
          current["label"] in fake_opener.body["messages"][0]["content"])

aoai_env = {
    "AOAI_ENDPOINT": "https://aoai.example.invalid",
    "AOAI_API_KEY": "fake-aoai",
    "AOAI_DEPLOYMENT": "gpt4o",
    "AOAI_API_VERSION": "2024-10-21",
    "AZURE_SPEECH_KEY_GLOBAL": "fake-speech",
    "AZURE_SPEECH_REGION_GLOBAL": "eastasia",
}
with env(**aoai_env, COPILOT_PROFILE="azure-global-aoai"):
    cfg_aoai = ProviderConfig()
    url2, headers2, _ = persona.build_request(msgs_en, cfg_aoai)
    check("request: AOAI 部署 URL",
          "/openai/deployments/gpt4o/chat/completions" in url2, url2)
    check("request: AOAI 用 api-key 头", headers2.get("api-key") == "fake-aoai")

# ---- 知识库层（临时目录，不污染 kb/）----
import tempfile

import knowledge_base

with tempfile.TemporaryDirectory() as tmp:
    kb_root = Path(tmp)
    (kb_root / "notes.md").write_text(
        "# 交货流程改造\n\n"
        "负责销售组织的交货类型梳理，先整理四种交货类型的差异判定表，"
        "与业务确认后按类型拆分增强点，用统一出口做回归。上线后异常单据明显减少。\n\n"
        "# 费用报销应用\n\n"
        "基于 BTP CAP 开发费用报销应用，负责服务层与审批流集成，"
        "通过 CPI 对接外部费用系统，处理凭证生成与状态回传。\n",
        encoding="utf-8")
    (kb_root / "ignored.txt.example").write_text("不该被索引", encoding="utf-8")
    kb = knowledge_base.KnowledgeBase(kb_root)
    check("kb: 只索引受支持的扩展名", kb.stats()["files"] == 1, kb.stats())
    check("kb: 按标题与段落切块", kb.stats()["chunks"] >= 2, kb.stats())
    hits = kb.search("介绍一下交货类型改造", k=2)
    check("kb: 中文检索命中相关片段",
          bool(hits) and "判定表" in hits[0]["text"], hits)
    hits_en = kb.search("Tell me about the expense app built with BTP CAP", k=2)
    check("kb: 中英混排检索命中",
          bool(hits_en) and "CPI" in hits_en[0]["text"], hits_en)
    check("kb: 不相关查询不返回噪声",
          kb.search("量子计算与超导材料", k=2) == [])

    (kb_root / "extra.txt").write_text(
        "# 团队管理\n\n带领 ABAP 开发团队负责代码评审。", encoding="utf-8")
    check("kb: 新增资料自动重建索引",
          kb.refresh() and kb.stats()["files"] == 2)

    msg_kb = persona.build_messages("介绍一下交货类型改造", source_lang="zh",
                                    store=store, kb=kb)
    sys_kb = msg_kb[0]["content"]
    check("kb: 命中片段注入 system prompt",
          "【资料依据" in sys_kb and "判定表" in sys_kb)
    check("kb: 有资料时收紧事实来源", "【资料依据】或画像" in sys_kb)
    check("kb: 禁止在回答里提及文件名", "不要提及文件名" in sys_kb)

    msg_no_kb = persona.build_messages("介绍一下交货类型改造", source_lang="zh",
                                       store=store, kb=kb, use_kb=False)
    check("kb: 关闭检索后不留空段",
          "【资料依据" not in msg_no_kb[0]["content"])

    with env(**FAKE_ENV):
        cfg_kb = ProviderConfig()
        result = persona.generate_answer(
            "介绍一下交货类型改造", cfg=cfg_kb, store=store,
            source_lang="zh", opener=fake_opener, kb=kb)
        check("kb: 端到端资料随请求发出",
              "判定表" in fake_opener.body["messages"][0]["content"])
        check("kb: 中文轮仍只出中文", result["answer_en"] == "")

        generated_payload = {
            "label": "资料生成：SAP 技术顾问",
            "target_role": "SAP 技术顾问",
            "years": "10 年以上",
            "domain": "零售行业 SAP 交付",
            "stack": ["ABAP", "BTP CAP"],
            "highlights": ["负责交货类型改造与回归验证"],
        }

        def build_opener(req, timeout=None):
            build_opener.body = json.loads(req.data.decode("utf-8"))
            return FakeResponse({"choices": [{"message": {"content":
                json.dumps(generated_payload, ensure_ascii=False)}}]})

        tmp_store = persona.PersonaStore(path=kb_root / "generated-persona.json")
        built = persona.build_persona_from_kb(
            cfg_kb, store=tmp_store, kb=kb, name="from-kb", opener=build_opener)
        check("build: 从资料生成事实字段",
              built["target_role"] == "SAP 技术顾问"
              and built["highlights"] == ["负责交货类型改造与回归验证"], built)
        check("build: 保留手写 constraints / style",
              built["constraints"] == store.current()["constraints"]
              and built["style"] == store.current()["style"])
        check("build: 资料片段随请求发出",
              "判定表" in build_opener.body["messages"][1]["content"])
        check("build: 生成人设已设为当前", tmp_store.active == "from-kb")

        # .docx 资料（标准库解包，不依赖 python-docx）
        docx_path = kb_root / "resume.docx"
        docx_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>团队管理与交付规范</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>负责 ABAP 团队代码评审</w:t></w:r></w:p>'
            '</w:body></w:document>')
        with zipfile.ZipFile(docx_path, "w") as zf:
            zf.writestr("word/document.xml", docx_xml)
        docx_text = knowledge_base.read_document(docx_path)
        check("docx: 标准库解析段落文本",
              "团队管理与交付规范" in docx_text and "代码评审" in docx_text)
        kb.refresh(force=True)
        hits_docx = kb.search("代码评审怎么做", k=2)
        check("docx: 内容进入资料索引",
              bool(hits_docx) and "代码评审" in hits_docx[0]["text"], hits_docx)

        msgs_build = persona.build_persona_messages(kb, user_prompt="突出团队管理")
        check("build: 手写提示词注入生成要求",
              "【用户对人设的要求】" in msgs_build[1]["content"]
              and "突出团队管理" in msgs_build[1]["content"])

# ---- 手写提示词（custom_prompt）与删除人设 ----
with tempfile.TemporaryDirectory() as tmp2:
    p_store = persona.PersonaStore(path=Path(tmp2) / "persona.json")
    base_persona = dict(store.current())
    base_persona.pop("id", None)
    base_persona["custom_prompt"] = "突出交付规范，少讲工具名"
    p_store.save("with-prompt", base_persona)

    sys_custom = persona.build_messages(
        "Tell me about your team", source_lang="en",
        store=p_store, use_kb=False)[0]["content"]
    check("提示词: 注入 system prompt",
          "【候选人自定义要求】" in sys_custom and "突出交付规范" in sys_custom)
    check("提示词: 不放松事实约束", "不得据此引入" in sys_custom)
    check("提示词: 不混进画像字段",
          "自定义要求" not in persona.render_persona(p_store.current()))
    check("提示词: 空值不注入段落",
          persona.render_custom_prompt({"custom_prompt": ""}) == "")

    class DummyKB:
        chunks = [{"source": "r.md", "heading": "",
                   "text": "带领 ABAP 团队负责代码评审"}]

        def refresh(self):
            return False

    with env(**FAKE_ENV):
        def prompt_opener(req, timeout=None):
            prompt_opener.body = json.loads(req.data.decode("utf-8"))
            return FakeResponse({"choices": [{"message": {"content":
                json.dumps({"label": "资料生成", "target_role": "SAP 顾问"},
                           ensure_ascii=False)}}]})

        built_prompt = persona.build_persona_from_kb(
            ProviderConfig(), store=p_store, kb=DummyKB(), name="from-kb",
            opener=prompt_opener)
        check("build: 保留手写提示词",
              built_prompt["custom_prompt"] == "突出交付规范，少讲工具名")
        check("build: 默认用手写提示词作生成要求",
              "突出交付规范" in prompt_opener.body["messages"][1]["content"])

    p_store.delete("from-kb")
    check("persona: 删除人设", "from-kb" not in p_store.list())
    for name in list(p_store.list()):
        if len(p_store.list()) <= 1:
            break
        p_store.delete(name)
    check("persona: 可删至只剩一套", len(p_store.list()) == 1)
    try:
        p_store.delete(p_store.active)
        check("persona: 拒绝删掉最后一套", False, "未抛异常")
    except persona.PersonaError:
        check("persona: 拒绝删掉最后一套", True)

# ---- 窗口隐身能力层 ----
import stealth

caps = stealth.capabilities()
check("stealth: 能力探测字段完整",
      caps.get("self_owned_only") is True and "flag" in caps, caps)
check("stealth: 正确识别系统支持的 affinity 标志",
      (not stealth.IS_WINDOWS) or caps["flag"] in
      ("WDA_EXCLUDEFROMCAPTURE", "WDA_MONITOR"), caps)
check("stealth: 默认显示名是中性词",
      stealth.DEFAULT_DISPLAY_NAME == "Notes")
check("stealth: 枚举窗口不抛异常",
      isinstance(stealth.enum_windows(), list))
check("stealth: 无改动时还原安全",
      stealth.WindowShield().restore() is None)

if stealth.IS_WINDOWS:
    import tkinter as tk
    probe = tk.Tk()
    probe.title("selftest-probe")
    probe.withdraw()          # 不干扰用户桌面
    probe.update()
    rows = stealth.enum_windows(pid=os.getpid(), visible_only=False)
    mine = [r for r in rows if r["own"] and r["title"] == "selftest-probe"]
    check("stealth: 找到自进程窗口", bool(mine), len(rows))
    if mine:
        hwnd = mine[0]["hwnd"]
        shield = stealth.WindowShield()
        shield._remember(hwnd)   # 记住原始状态
        ok, _ = stealth.set_capture_exclusion(hwnd, True)
        check("stealth: 自进程窗口可排除捕获",
              ok and stealth.get_affinity(hwnd) == 0x11,
              stealth.get_affinity(hwnd))
        check("stealth: 中性标题可设置",
              stealth.set_title(hwnd, "Notes")
              and any(r["title"] == "Notes" for r in
                      stealth.enum_windows(pid=os.getpid(), visible_only=False)))
        check("stealth: 任务栏隐藏可设置", stealth.set_toolwindow(hwnd, True))
        shield.restore()
        check("stealth: 还原 affinity 与标题",
              stealth.get_affinity(hwnd) == 0
              and any(r["title"] == "selftest-probe" for r in
                      stealth.enum_windows(pid=os.getpid(), visible_only=False)))
    probe.destroy()

# ---- 本地应用 API 冒烟（临时数据目录，不碰真实配置）----
import app as webapp

local_opener = make_opener(ProxyHandler({}))


def http(method, path, payload=None, base="", headers=None):
    raw = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
           if payload is not None else None)
    request_headers = {"Content-Type": "application/json"} if raw else {}
    request_headers.update(headers or {})
    req = Request(base + path, data=raw, method=method, headers=request_headers)
    try:
        with local_opener.open(req, timeout=8) as resp:
            text = resp.read().decode("utf-8")
            body = (json.loads(text) if resp.headers.get_content_type()
                    == "application/json" else text)
            return resp.status, body
    except HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, text


with tempfile.TemporaryDirectory() as tmp3:
    home = Path(tmp3)
    (home / "config").mkdir()
    (home / "kb").mkdir()
    shutil.copy(ROOT / "config" / "providers.json",
                home / "config" / "providers.json")
    (home / "config" / ".env").write_text(
        "# local test\nSELFTEST_EXISTING=value\n", encoding="utf-8")
    ctx = webapp.AppContext(home)
    srv = webapp.make_server(home, port=0, ctx=ctx)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    worker = threading.Thread(target=srv.serve_forever, daemon=True)
    worker.start()
    try:
        code, health = http("GET", "/api/health", base=base)
        check("api: 健康检查", code == 200 and health.get("ok") is True)
        check("api: 桌面版拒绝非本机 Host",
              http("GET", "/api/state", base=base, headers={"Host": "other.example"})[0] == 403)
        check("api: 跨站请求不能修改桌面版会话",
              http("POST", "/api/interview/clear", {}, base,
                   headers={"Origin": "http://other.example"})[0] == 403)
        check("api: 同源页面可修改会话",
              http("POST", "/api/interview/clear", {}, base,
                   headers={"Origin": base})[0] == 200)
        code, html = http("GET", "/", base=base)
        check("api: 首页可访问且显示名占位已替换",
              code == 200 and "__DISPLAY_NAME__" not in html
              and "<title>Notes</title>" in html, html[:120] if code != 200 else "")
        code, css = http("GET", "/style.css", base=base)
        check("api: 样式可访问", code == 200 and ".topbar" in css)
        code, st = http("GET", "/api/state", base=base)
        check("api: 状态含人设/资料/服务",
              code == 200 and "persona" in st and "kb" in st and "provider" in st)

        doc = "# 交货类型\n\n负责四种交货类型的差异判定表与回归验证。\n"
        b64 = base64.b64encode(doc.encode("utf-8")).decode("ascii")
        code, up = http("POST", "/api/kb/upload",
                        {"filename": "notes.md", "content_b64": b64}, base)
        check("api: 拖入资料写盘并重建索引",
              code == 200 and up["state"]["kb"]["stats"]["files"] == 1,
              up.get("error", ""))
        code, bad = http("POST", "/api/kb/upload",
                         {"filename": "a.pdf", "content_b64": b64}, base)
        check("api: 拒绝不支持的格式", code == 400 and "error" in bad)
        code, bad2 = http("POST", "/api/kb/upload",
                          {"filename": "../../evil.txt", "content_b64": b64}, base)
        check("api: 目录穿越被拦截",
              code == 200 and not (home / "evil.txt").exists()
              and (home / "kb" / "evil.txt").exists())

        payload = {"name": "my-persona", "data": {
            "label": "SAP 技术顾问", "stack": "ABAP\nBTP CAP",
            "highlights": "负责交货类型改造", "constraints": "不透露客户名",
            "style": "先结论后依据", "custom_prompt": "突出交付规范",
            "examples": "介绍一个项目 | 我主导过交货流程改造"}}
        code, saved = http("POST", "/api/persona/save", payload, base)
        check("api: 保存人设并激活",
              code == 200 and saved["state"]["persona"]["active"] == "my-persona")
        cur = saved["state"]["persona"]["current"]
        check("api: 表单文本域整理为数组",
              cur["stack"] == ["ABAP", "BTP CAP"]
              and cur["examples"][0]["q"] == "介绍一个项目", cur)
        check("api: 手写提示词已落盘", cur["custom_prompt"] == "突出交付规范")

        code, prev = http("POST", "/api/persona/preview",
                          {"question": "介绍一下交货类型改造", "lang": "zh"}, base)
        sysmsg = prev["messages"][0]["content"] if code == 200 else ""
        check("api: 预览注入提示词与资料",
              "突出交付规范" in sysmsg and "【资料依据" in sysmsg
              and "判定表" in sysmsg,
              "code=%s keys=%s syslen=%d preview=%s" %
              (code, list(prev) if isinstance(prev, dict) else type(prev),
               len(sysmsg), str(prev)[:180]))

        code, used = http("POST", "/api/provider/use",
                          {"profile": "azure-cn-deepseek"}, base)
        check("api: 服务组合切换", code == 200
              and used["state"]["provider"]["active"] == "azure-cn-deepseek")
        code, dele = http("POST", "/api/kb/delete",
                          {"filename": "notes.md"}, base)
        names = [f["name"] for f in dele["state"]["kb"]["files"]]
        check("api: 删除资料", code == 200 and "notes.md" not in names)

        # ---- 页面配置：服务 / 方案 / 密钥 ----
        code, st2 = http("GET", "/api/state", base=base)
        check("api: 状态带服务定义与密钥状态",
              "llm_providers" in st2["provider"]
              and "secrets" in st2["provider"]
              and "active_ids" in st2["provider"])

        code, added = http("POST", "/api/provider/save", {
            "kind": "llm", "id": "my-llm",
            "data": {"type": "openai_compatible",
                     "base_url": "https://llm.example.invalid/v1",
                     "model": "my-model", "supports_json_mode": True,
                     "note": "页面新增"},
            "secret": "sk-page-test", "persist_secret": True}, base)
        check("api: 页面新增模型服务",
              code == 200 and "my-llm" in added["state"]["provider"]["llm_providers"],
              added.get("error", ""))
        saved_llm = added["state"]["provider"]["llm_providers"]["my-llm"]
        check("api: 密钥只留占位不进 providers.json",
              saved_llm["api_key"] == "${COPILOT_LLM_MY_LLM_KEY}", saved_llm)
        check("api: 响应不回显明文密钥",
              "sk-page-test" not in json.dumps(added, ensure_ascii=False))
        env_text = (home / "config" / ".env").read_text(encoding="utf-8")
        check("api: 密钥写入 .env 且保留其它行与注释",
              "sk-page-test" in env_text and "# local test" in env_text
              and "SELFTEST_EXISTING=value" in env_text)
        sec = added["state"]["provider"]["secrets"].get("llm:my-llm", {})
        check("api: 密钥状态可查询", sec.get("set") is True and sec.get("env"))

        inline_key = "selftest-embedded-secret"
        for field, value in (
                ("stt_headers", json.dumps({"X-Gateway": "Bearer " + inline_key})),
                ("stt_url", "https://asr.example.invalid/recognize?access_token=" + inline_key),
                ("stt_json_template", json.dumps({"request": {"app_key": inline_key}})),
        ):
            code, rejected = http("POST", "/api/provider/save", {
                "kind": "speech", "id": "embedded-asr", "data": {field: value},
                "secret": inline_key, "persist_secret": True}, base)
            check("api: 拒绝将明文密钥写入 " + field,
                  code == 400 and field in rejected.get("error", "")
                  and inline_key not in str(rejected)
                  and inline_key not in (home / "config" / ".env").read_text(encoding="utf-8")
                  and inline_key not in ctx.cfg.path.read_text(encoding="utf-8"))

        code, mem = http("POST", "/api/provider/save", {
            "kind": "llm", "id": "mem-llm",
            "data": {"type": "openai_compatible",
                     "base_url": "https://mem.example.invalid/v1", "model": "m"},
            "secret": "sk-memory-only", "persist_secret": False}, base)
        check("api: 仅本次进程的密钥不写 .env",
              code == 200 and "sk-memory-only" not in
              (home / "config" / ".env").read_text(encoding="utf-8"))

        code0, st0 = http("GET", "/api/state", base=base)
        active_name = st0["provider"]["active"]
        preset_llm = st0["provider"]["profiles"][active_name]["llm"]
        code, act = http("POST", "/api/provider/activate",
                         {"kind": "llm", "id": "my-llm"}, base)
        check("api: 顶栏切换模型立即生效",
              code == 200 and act["state"]["provider"]["active_ids"]["llm"] == "my-llm")
        check("api: 切换只写独立生效项，不改写方案定义",
              act["state"]["provider"]["selection"]["llm"] == "my-llm"
              and act["state"]["provider"]["profiles"][active_name]["llm"] == preset_llm)
        check("api: 切模型不影响语音侧",
              act["state"]["provider"]["active_ids"]["speech"] ==
              st0["provider"]["active_ids"]["speech"])
        code, offl = http("POST", "/api/provider/activate",
                          {"kind": "llm", "id": "__off__"}, base)
        p = offl["state"]["provider"]
        check("api: 停用模型不影响语音",
              code == 200 and p["active_ids"]["llm"] == ""
              and p["active_ids"]["speech"] != ""
              and offl["state"]["llm_ready"] is False)
        check("api: 停用侧不报缺失密钥",
              not [x for x in p["problems"] if "[llm:" in x], p["problems"])
        code, offs = http("POST", "/api/provider/activate",
                          {"kind": "speech", "id": "off"}, base)
        check("api: 停用语音提示明确",
              code == 200 and "停用" in
              offs["state"]["interview"]["speech"]["reason"],
              offs["state"]["interview"]["speech"])
        http("POST", "/api/provider/activate", {"kind": "speech", "id": "azure-cn"}, base)
        http("POST", "/api/provider/activate", {"kind": "llm", "id": "my-llm"}, base)

        code, prof = http("POST", "/api/profile/save", {
            "name": "my-combo", "speech": "azure-cn", "llm": "my-llm",
            "description": "页面新建组合"}, base)
        check("api: 页面新增方案并激活",
              code == 200 and prof["state"]["provider"]["active"] == "my-combo"
              and prof["state"]["provider"]["profiles"]["my-combo"]["llm"] == "my-llm")
        code, refused = http("POST", "/api/provider/delete",
                             {"kind": "llm", "id": "my-llm"}, base)
        check("api: 拒绝删除被引用的服务",
              code == 400 and "被方案引用" in refused.get("error", ""), refused)
        code, delp = http("POST", "/api/profile/delete",
                          {"name": "my-combo"}, base)
        check("api: 删除方案", code == 200
              and "my-combo" not in delp["state"]["provider"]["profiles"])
        http("POST", "/api/profile/save", {
            "name": "azure-cn-deepseek", "speech": "azure-cn", "llm": "qwen",
            "description": ""}, base)
        code, dell = http("POST", "/api/provider/delete",
                          {"kind": "llm", "id": "my-llm"}, base)
        check("api: 解除引用后可删服务", code == 200
              and "my-llm" not in dell["state"]["provider"]["llm_providers"])

        # ---- 屏幕共享 / 隐身 ----
        code, st3 = http("GET", "/api/state", base=base)
        check("api: 状态含 ui / access / stealth",
              code == 200 and {"ui", "access", "stealth"} <= set(st3))
        check("api: 默认模式与监听范围",
              st3["access"]["mode"] == "browser"
              and st3["access"]["lan"] is False
              and st3["ui"]["display_name"] == "Notes")
        check("api: 明确标注跨进程限制",
              st3["stealth"]["capabilities"]["self_owned_only"] is True
              and "跨进程" in st3["stealth"]["capabilities"]["note"])

        code, ur = http("POST", "/api/ui/save",
                        {"display_name": "Memo"}, base)
        check("api: 显示名保存并生效",
              code == 200 and ur["state"]["ui"]["display_name"] == "Memo")
        code, html2 = http("GET", "/", base=base)
        check("api: 首页标题与品牌同步中性名",
              "<title>Memo</title>" in html2
              and "Interview Copilot" not in html2
              and "__DISPLAY_NAME__" not in html2)
        check("api: 显示名写入 app.json",
              json.loads((home / "config" / "app.json").read_text(
                  encoding="utf-8"))["display_name"] == "Memo")
        http("POST", "/api/ui/save",
             {"display_name": "<script>alert(1)</script>"}, base)
        code, html3 = http("GET", "/", base=base)
        check("api: 显示名做 HTML 转义",
              "<script>alert(1)</script>" not in html3
              and "&lt;script&gt;" in html3)
        code, empty = http("POST", "/api/ui/save",
                           {"display_name": "   "}, base)
        check("api: 空显示名回退默认值",
              empty["state"]["ui"]["display_name"] == "Notes")

        code, sst = http("GET", "/api/stealth/status", base=base)
        check("api: 隐身状态可查询",
              code == 200 and "windows" in sst and "mode" in sst)
        code, applied = http("POST", "/api/stealth/apply", {}, base)
        check("api: 应用隐身如实说明浏览器窗口限制",
              code == 200 and "浏览器" in applied["result"]["note"])
        code, latest = http("GET", "/api/answer/latest", base=base)
        check("api: 最近回答初始为空",
              code == 200 and latest["answer"] is None)
        code, hid = http("POST", "/api/stealth/hide", {}, base)
        check("api: 隐藏本机窗口", code == 200 and hid["ok"] is True)
        code, shw = http("POST", "/api/stealth/show", {}, base)
        check("api: 恢复本机窗口", code == 200 and shw["ok"] is True)

        old_ips = ctx.lan_ips
        ctx.lan, ctx.token, ctx.lan_ips = True, "t", ["192.168.1.9", "10.0.0.2"]
        st_lan = ctx.state()
        check("api: 多网卡时列出全部手机地址",
              len(st_lan["access"]["lan_urls"]) == 2
              and st_lan["access"]["lan_url"] == st_lan["access"]["lan_urls"][0]
              and st_lan["access"]["lan_urls"][1].startswith("http://10.0.0.2:"))
        ctx.lan, ctx.token, ctx.lan_ips = False, "", old_ips

        class FakeReq:
            def __init__(self, host, ip, path="/api/state", token=""):
                self.ctx = ctx
                self.headers = {"Host": host}
                if token:
                    self.headers["X-Copilot-Token"] = token
                self.client_address = (ip, 50000)
                self.path = path

        old_lan, old_token, old_hosts = (ctx.lan, ctx.token, ctx.allowed_hosts)
        ctx.lan, ctx.token = True, "unit-token"
        ctx.allowed_hosts = {"127.0.0.1", "localhost", "192.168.1.9"}
        auth = webapp.Handler._authorized
        check("鉴权: 非本机无令牌拒绝",
              auth(FakeReq("192.168.1.9:8765", "192.168.50.7")) is False)
        check("鉴权: 正确令牌放行",
              auth(FakeReq("192.168.1.9:8765", "192.168.50.7",
                           token="unit-token")) is True)
        check("鉴权: 错误令牌拒绝",
              auth(FakeReq("192.168.1.9:8765", "192.168.50.7",
                           token="wrong")) is False)
        check("鉴权: URL 查询带令牌放行",
              auth(FakeReq("192.168.1.9:8765", "192.168.50.7",
                           path="/?token=unit-token")) is True)
        check("鉴权: Host 不在白名单拒绝",
              auth(FakeReq("evil.example:8765", "127.0.0.1")) is False)
        check("鉴权: 本机免令牌",
              auth(FakeReq("127.0.0.1:8765", "127.0.0.1")) is True)
        check("鉴权: 静态资源无需令牌",
              auth(FakeReq("192.168.1.9:8765", "192.168.50.7",
                           path="/app.js")) is True)
        ctx.lan, ctx.token, ctx.allowed_hosts = old_lan, old_token, old_hosts

        check("校验: 显示名去控制字符并限长",
              webapp.sanitize_display_name("A\x07B" + "x" * 80).startswith("AB")
              and len(webapp.sanitize_display_name("A" * 80)) == 40)
        check("校验: 局域网 IP 探测不抛异常",
              isinstance(webapp.local_ips(), list))
        with (patch.object(webapp.socket, "gethostbyname_ex",
                           return_value=("", [], ["172.31.176.1", "192.168.31.188", "10.48.134.28"])),
              patch.object(webapp, "physical_ipv4", return_value=["192.168.31.188"]),
              patch.object(webapp.socket, "socket") as route_socket):
            route_socket.return_value.__enter__.return_value.getsockname.return_value = (
                "10.48.134.28", 12345)
            check("启动器: 手机推荐实体网卡，即使默认路由经 VPN",
                  webapp.local_ips() == ["192.168.31.188", "10.48.134.28", "172.31.176.1"])

        for name, needle in (
                ("start-hidden.vbs", "--share-safe"),
                ("start-phone-only.vbs", "--share-safe"),
                ("start-desktop-setup.bat", "--setup"),
                ("stop-hidden.vbs", "pythonw.exe")):
            path = ROOT / name
            check("启动器: " + name,
                  path.exists() and needle in path.read_text(encoding="utf-8"))
        ignores = (ROOT / ".gitignore").read_text(encoding="utf-8")
        check("忽略: 访问地址 / 日志 / 浏览器 profile 不入库",
              all(x in ignores for x in ("config/access.txt", "logs/",
                                         "runtime/", "config/app.json")))
        page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        check("界面: 引入面试脚本与三个视图",
              "/interview.js" in page and 'id="viewInterview"' in page
              and 'id="interviewView"' in page)
        check("界面: hidden 有兜底规则",
              "[hidden] { display: none !important; }" in css
              and ".iv-turn" in css)
        app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        pc_src = (ROOT / "provider_config.py").read_text(encoding="utf-8")
        check("界面: 语音地址与新协议在配置页可编辑",
              "stt_url" in app_js and "openai_compatible_stt" in app_js
              and 'id="speechUrlHint"' in page)
        check("界面: JSON 模板字段可按请求体格式动态显隐",
              "stt_json_template" in app_js and "json_template" in app_js
              and "data-bodies" in app_js and "<textarea" in app_js
              and "stt_json_template" in pc_src)
        check("界面: ASR 与 LLM 独立分区且通用协议可编辑",
              "generic_http_stt" in app_js and "asrTypeHint" in page
              and "applyAsrFieldVisibility" in app_js
              and "ASR 与 LLM 是两个独立服务池" in page)
        iv_js = (ROOT / "web" / "interview.js").read_text(encoding="utf-8")
        check("界面: 共享提示区分标签页 / 窗口 / 自身窗口",
              "必须先点选" in iv_js and "使用系统音频共享" in iv_js
              and "Notes 的本工具窗口" in iv_js
              and "屏幕共享音频（标签页 / 窗口 / 整个屏幕）" in page)
        check("界面: 视频轨只禁用不 stop，失败原因不被覆盖",
              "getVideoTracks().forEach(t => { t.enabled = false; })" in iv_js
              and "getVideoTracks().forEach(t => t.stop())" not in iv_js
              and 0 < iv_js.find("await releaseCapture();")
              < iv_js.find("setLive(reason, true);"))
        check("界面: LLM 未就绪时给出具体原因与可切换服务",
              "已打开翻译 / 回答，但 LLM 未就绪" in iv_js
              and "readyLlmIds" in iv_js and "llmProblems" in iv_js
              and "已填好密钥的服务" in iv_js
              and "本轮无回答（实战模式 / 模型未启用）" not in iv_js)
        check("界面: 翻译 / 回答两个开关默认关闭且可持久化",
              'id="ivTranslate"' in page and 'id="ivAnswer"' in page
              and 'aria-pressed="false"' in page
              and "let wantTranslate = false, wantAnswer = false" in iv_js
              and "copilot.iv.toggles" in iv_js
              and "classList.toggle('on'" in iv_js)
        check("界面: 提交轮次按开关传参，模式不再干预输出",
              "translate: wantTranslate, answers: wantAnswer" in iv_js
              and "ivMode').value === 'practice'" not in iv_js
              and "实战模式只给译文" not in iv_js)
        check("界面: 单语答案渲染，中英文任一为空不算有回答",
              "!a.answer_zh && !a.answer_en" in iv_js
              and "English answer" in iv_js)
        check("界面: 提交轮次直达后端，由服务端读取当前配置",
              "req('/api/state')" not in iv_js
              and "req('/api/interview/turn', body, signal)" in iv_js
              and "历史轮次不会回溯补答案" in iv_js)
        check("界面: 默认简洁视图，维护项收进高级设置",
              'id="quickView"' in page and 'id="advancedDetails"' in page
              and 'id="modeToggle"' in page and 'id="viewSetup"' in page
              and 'class="left adv-only"' in page
              and "body.simple .adv-only" in css
              and "ASR 与 LLM 是两个独立服务池" in page)
        check("界面: 简洁视图默认进面试页且只改密钥",
              "loadSimpleMode" in app_js and "function saveQuickKeys" in app_js
              and "data: {}" in app_js and "function applyMode" in app_js
              and "switchView(forced || simpleMode ? 'interview' : 'persona')" in app_js
              and "qv === 'config'" in app_js)
        check("界面: 隐身提示用 classList 切色，不抹掉 adv-only",
              "badge.classList.toggle('bad', bad)" in app_js
              and 'id="stealthState" class="state adv-only"' in page)
        check("界面: 顶栏 ASR / LLM 状态各自独立着色",
              'id="keyState" class="state state-group"' in page
              and "ks.innerHTML" in app_js
              and "part('ASR'" in app_js and "part('LLM'" in app_js
              and "ks.className" not in app_js
              and all(x in css for x in (
                  ".state-part.ok", ".state-part.bad", ".state-part.off")))
        check("界面: ASR 模型名输入框在通用 HTTP 下可见且各处显示实际模型",
              "模型名（填了就覆盖请求体模板" in app_js
              and "['openai_compatible_stt', 'generic_http_stt', 'qwen_realtime_stt', 'faster_whisper']" in app_js
              and "sp.model" in app_js and "speech.model" in app_js
              and "['ASR 模型', speech.model" in iv_js)
        check("界面: 设置页提供模型新增 / 编辑 / 删除入口",
              'class="modelmgr"' in page and ".mm-row" in css
              and all('id="' + x + '"' in page for x in (
                  "mmNewBtn", "mmAsrList", "mmLlmList", "mmEditor",
                  "mmKind", "mmPreset", "mmId", "mmModel", "mmUrl", "mmKey",
                  "mmProto", "mmTemp", "mmMaxTokens", "mmTimeout",
                  "mmJsonMode", "mmSaveBtn", "mmCancelBtn")))
        check("界面: 模型管理含模板复制 / 常用参数 / 删除保护",
              all(x in app_js for x in (
                  "function renderModels", "function openModelEditor",
                  "function saveModel", "function deleteModel",
                  "function mmPresets", "function mmAddrField",
                  "function mmEnv", "function mmSuggestId", "MM_BLANK"))
              and "data-del-model" in app_js and "usedBy.length" in app_js
              and "Number.isFinite(n)" in app_js
              and "k !== 'api_key'" in app_js
              and "renderModels();" in app_js
              and "drop_refs: true" in app_js)
        check("模型管理: 删除接口可清理方案引用且不改生效项",
              'drop_refs=bool(data.get("drop_refs"))' in
              (ROOT / "app.py").read_text(encoding="utf-8")
              and "drop_refs=False" in pc_src
              and "不动 active / selection" in pc_src)
        # 后端 ID_RE 放宽后，前端 saveModel 的同款校验与报错文案必须同步，
        # 否则编辑 qwen-audio-3.1-asr-flash 这类带点号 ID 会先被前端拦下。
        check("模型管理: 前后端 ID 校验一致，带点号模型可编辑保存",
              "/^[A-Za-z0-9_.-]+$/.test(id)" in app_js
              and "/[^A-Za-z0-9_.-]/g" in app_js
              and 'ID_RE = re.compile(r"[A-Za-z0-9_.-]+")' in pc_src
              and "下划线、连字符和点号" in app_js
              and "下划线、连字符和点号" in pc_src)
        # 百炼云系服务在顶栏下拉 / 模型管理 / 快速设置三处都要带前缀，
        # 只改显示文本，option 的 value 仍是服务 ID。
        check("界面: 百炼云服务显示名带「百炼云 ·」前缀",
              "function isBailian" in app_js and "function modelLabel" in app_js
              and "百炼云 · " in app_js
              and "/dashscope|maas\\.aliyuncs\\.com/i.test(u)" in app_js
              and "n => modelLabel('speech', n)" in app_js
              and "n => modelLabel('llm', n)" in app_js
              and "esc(modelLabel(kind, id))" in app_js
              and "modelLabel('speech', asrId)" in app_js
              and "modelLabel('llm', llmId)" in app_js)
        check("界面: 旧后端半截 JSON 由前端兜底清理",
              "function salvageAnswer" in iv_js
              and "const a = salvageAnswer(t);" in iv_js
              and "建议调大 max_tokens" in iv_js)

        # ---- 面试链路：WAV / ASR / 会话 / 新 API（全部 mock，不联网）----
        import struct
        import interview as iv

        def make_wav(ms=500, rate=16000, ch=1, bits=16, fmt=1):
            n = int(rate * ms / 1000)
            pcm = struct.pack("<%dh" % n, *([120] * n))
            head = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
                    + struct.pack("<IHHIIHH", 16, fmt, ch, rate,
                                  rate * ch * bits // 8, ch * bits // 8, bits)
                    + b"data" + struct.pack("<I", len(pcm)))
            return head + pcm

        wav = make_wav()
        info = iv.wav_info(wav)
        check("面试: WAV 校验与时长", info["duration_ms"] == 500
              and len(info["pcm"]) == 16000, info["duration_ms"])
        for name, bad in (("非 RIFF", b"xxxx" + wav[4:]),
                          ("采样率不符", make_wav(rate=8000)),
                          ("非 PCM", make_wav(fmt=3))):
            try:
                iv.wav_info(bad)
                check("面试: 拒绝" + name, False)
            except iv.InterviewError:
                check("面试: 拒绝" + name, True)

        speech_cn = {"type": "azure_speech_translation", "api_key": "k",
                     "region": "chinaeast2", "endpoint": ""}
        speech_global = dict(speech_cn, region="eastasia")
        check("面试: 中国区 STT 域名",
              ".stt.speech.azure.cn" in iv.stt_endpoint(speech_cn))
        check("面试: 全球区 STT 域名",
              ".stt.speech.microsoft.com" in iv.stt_endpoint(speech_global))
        check("面试: 门户 endpoint 转成 STT 域名",
              ".stt.speech.azure.cn" in iv.stt_endpoint(dict(
                  speech_cn, endpoint="https://xxx.cognitiveservices.azure.cn/")))
        gw = "https://gw.example.invalid:8443/proxy/stt?subscription-key=abc"
        custom = dict(speech_cn, stt_url=gw)
        check("面试: stt_url 原样保留端口/路径/查询串",
              iv.stt_endpoint(custom) == gw, iv.stt_endpoint(custom))
        check("面试: 已有查询串时用 & 追加语种",
              "&language=zh-CN" in iv.stt_request(wav, custom, "zh-CN")[0],
              iv.stt_request(wav, custom, "zh-CN")[0])
        check("面试: 只填域名的 stt_url 自动补 STT 路径",
              iv.stt_endpoint(dict(
                  speech_cn, stt_url="https://gw.example.invalid")).endswith(iv.STT_PATH))
        check("面试: Azure 网关路径前缀不被丢弃",
              "/proxy" + iv.STT_PATH in iv.stt_endpoint(dict(
                  speech_cn, endpoint="https://apim.example.invalid/proxy")),
              iv.stt_endpoint(dict(speech_cn,
                                   endpoint="https://apim.example.invalid/proxy")))
        check("面试: 展示地址对查询串密钥打码",
              "abc" not in iv.mask_stt_url(gw)
              and "subscription-key" in iv.mask_stt_url(gw))
        try:
            iv.stt_endpoint(dict(speech_cn, stt_url="file:///etc/passwd"))
            check("面试: 拒绝非 http(s) 地址", False)
        except iv.InterviewError:
            check("面试: 拒绝非 http(s) 地址", True)

        oai = {"type": "openai_compatible_stt", "api_key": "k",
               "base_url": "https://asr.example.invalid/v1", "model": "sensevoice"}
        check("面试: OpenAI 兼容 STT 地址",
              iv.stt_effective_url(oai).endswith("/v1/audio/transcriptions"),
              iv.stt_effective_url(oai))
        _, oai_headers, oai_body = iv.stt_openai_request(wav, oai, "zh-CN")
        check("面试: multipart 含 WAV / 模型 / 语种",
              b"audio.wav" in oai_body and b"sensevoice" in oai_body
              and b'name="language"\r\n\r\nzh' in oai_body
              and oai_headers["Authorization"] == "Bearer k")
        check("面试: OpenAI 兼容 STT 结果解析",
              iv.parse_stt_openai({"text": "你好"}) == "你好"
              and iv.parse_stt_openai({"data": {"text": "你好"}}) == "你好")

        # ---- 通用 HTTP ASR：任意服务商，请求与响应都可配 ----
        generic = {
            "type": "generic_http_stt", "api_key": "k",
            "stt_url": "https://asr.example.invalid:9000/v1/recognize?appkey=abc",
            "stt_method": "PUT",
            "stt_headers": '{"Authorization": "Bearer k", "X-Engine": "paraformer"}',
            "stt_body": "multipart", "audio_field": "audio_file",
            "stt_extra": '{"model": "paraformer", "format": "wav"}',
            "response_path": "data.text", "stt_lang_query": "lang",
        }
        gurl, gheaders, gextra = iv.stt_generic_config(generic)
        check("ASR: 通用 HTTP 保留端口 / 路径 / 查询串",
              gurl.endswith(":9000/v1/recognize?appkey=abc")
              and iv.stt_effective_url(generic) == gurl, gurl)
        check("ASR: 通用 HTTP 请求头与额外字段",
              gheaders["X-Engine"] == "paraformer"
              and gextra["model"] == "paraformer")
        g_url, g_h, g_body, g_method = iv.stt_generic_request(wav, generic, "zh-CN")
        check("ASR: multipart 自定义音频字段名 / 额外字段 / 语种",
              g_method == "PUT" and "lang=zh-CN" in g_url
              and b'name="audio_file"' in g_body
              and b'name="model"' in g_body and b"paraformer" in g_body
              and g_h["Authorization"] == "Bearer k", g_url)
        check("ASR: raw_wav 直接发 PCM WAV",
              iv.stt_generic_request(
                  wav, dict(generic, stt_body="raw_wav"), "en-US")[2] == wav)
        j_url, j_h, j_body, _ = iv.stt_generic_request(
            wav, dict(generic, stt_body="json_base64",
                      audio_field="pcm"), "en-US")
        check("ASR: json_base64 把音频放进指定字段",
              json.loads(j_body)["pcm"] == base64.b64encode(wav).decode("ascii")
              and j_h["Content-Type"] == "application/json")
        check("ASR: 响应路径与常见字段自动解析",
              iv.parse_stt_generic({"data": {"text": "介绍一下你的项目"}},
                                   "data.text") == "介绍一下你的项目"
              and iv.parse_stt_generic({"transcript": "Hello"}) == "Hello"
              and iv.parse_stt_generic({"result": [{"text": "你好"}]},
                                       "result.0.text") == "你好")

        # ---- 百炼 qwen3-asr-flash：chat/completions + Data URI 嵌套请求体 ----
        bailian = {
            "type": "generic_http_stt", "api_key": "sk-bailian",
            "stt_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "stt_method": "POST",
            "stt_headers": '{"Authorization": "Bearer sk-bailian"}',
            "stt_body": "json_template",
            "stt_json_template": json.dumps({
                "model": "qwen3-asr-flash",
                "messages": [{"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {
                        "data": "{{audio_data_uri}}"}}]}],
                "stream": False,
                "asr_options": {"language": "{{lang_short}}",
                                "enable_itn": False}}, ensure_ascii=False),
            "response_path": "choices.0.message.content", "stt_lang_query": "",
        }
        b_url, b_h, b_body, b_method = iv.stt_generic_request(wav, bailian, "zh-CN")
        b_obj = json.loads(b_body)
        check("百炼: 模板生成 qwen3-asr-flash 嵌套请求体",
              b_method == "POST" and b_h["Content-Type"] == "application/json"
              and b_url.endswith("/compatible-mode/v1/chat/completions")
              and b_obj["model"] == "qwen3-asr-flash"
              and b_obj["messages"][0]["content"][0]["input_audio"]["data"]
              == "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii"),
              b_obj.get("model"))
        check("百炼: 语种占位符随识别语种切换",
              json.loads(iv.stt_generic_request(
                  wav, bailian, "en-US")[2])["asr_options"]["language"] == "en"
              and iv.render_stt_template(
                  '{"a": "{{audio_base64}}", "b": "{{audio_mime}}", '
                  '"c": "{{language}}"}', wav, "zh-CN").decode() ==
              json.dumps({"a": base64.b64encode(wav).decode("ascii"),
                          "b": "audio/wav", "c": "zh-CN"}))

        def tpl_rejected(text):
            try:
                iv.render_stt_template(text, wav, "zh-CN")
                return False
            except iv.InterviewError:
                return True

        check("百炼: 空模板 / 非法 JSON / 未知占位符都被拒绝",
              all(tpl_rejected(x) for x in
                  ('{"audio": "{{nope}}"}', "{oops", "", "[]")))

        # ---- 顶层 model 与请求体模板联动 ----
        check("ASR 联动: effective_asr_model 顶层优先 / 模板回退 / 占位忽略",
              iv.effective_asr_model(dict(bailian, model="qwen3-asr-flash-2026"))
              == "qwen3-asr-flash-2026"
              and iv.effective_asr_model(bailian) == "qwen3-asr-flash"
              and iv.effective_asr_model(dict(bailian, model="${UNSET_ASR_MODEL}"))
              == "qwen3-asr-flash"
              and iv.effective_asr_model({"type": "azure_speech_translation"}) == "",
              iv.effective_asr_model(bailian))
        linked = json.loads(iv.stt_generic_request(
            wav, dict(bailian, model="qwen3-asr-flash-2026"), "en-US")[2])
        check("ASR 联动: 顶层 model 覆盖模板且不动其它字段",
              linked["model"] == "qwen3-asr-flash-2026"
              and linked["asr_options"]["language"] == "en"
              and linked["messages"][0]["content"][0]["input_audio"]["data"]
              .startswith("data:audio/wav;base64,"), linked.get("model"))
        check("ASR 联动: 顶层留空 / 未解析占位时仍用模板默认值",
              json.loads(iv.stt_generic_request(wav, bailian, "en-US")[2])["model"]
              == "qwen3-asr-flash"
              and json.loads(iv.stt_generic_request(
                  wav, dict(bailian, model="${UNSET_ASR_MODEL}"), "en-US")[2])["model"]
              == "qwen3-asr-flash")
        check("ASR 联动: render_stt_template 覆盖后仍是合法 JSON",
              json.loads(iv.render_stt_template(
                  '{"model": "old", "a": "{{audio_base64}}"}',
                  wav, "zh-CN", model="new-model").decode())["model"] == "new-model")
        m_body = iv.stt_generic_request(
            wav, dict(generic, model="paraformer-v2"), "zh-CN")[2]
        check("ASR 联动: multipart 也以顶层模型名为准",
              b"\r\nparaformer-v2\r\n" in m_body
              and b"\r\nparaformer\r\n" not in m_body)
        check("ASR 联动: json_base64 也带上顶层模型名",
              json.loads(iv.stt_generic_request(
                  wav, dict(generic, stt_body="json_base64", audio_field="pcm",
                            model="paraformer-v2"), "en-US")[2])["model"]
              == "paraformer-v2")

        class BailianResponse:
            def read(self):
                return json.dumps({"choices": [
                    {"message": {"content": "Tell me about your SAP project"}}]},
                    ensure_ascii=False).encode("utf-8")
            def __enter__(self): return self
            def __exit__(self, *a): return False

        check("百炼: recognize 端到端取回 choices.0.message.content",
              iv.recognize(wav, bailian, "en-US",
                           lambda req, timeout=None: BailianResponse())
              == "Tell me about your SAP project")

        # ---- Qwen-Audio-3.1 非实时 ASR：DashScope 原生协议 ----
        qa_cfg = json.loads((ROOT / "config" / "providers.json")
                            .read_text(encoding="utf-8"))["speech_providers"][
                                "qwen-audio-3.1-asr-flash"]
        check("Qwen-Audio-3.1: 预设走原生端点 / output.text / 顶层模型名",
              qa_cfg["stt_url"].endswith(
                  "/api/v1/services/aigc/multimodal-generation/generation")
              and qa_cfg["response_path"] == "output.text"
              and qa_cfg["model"] == "qwen-audio-3.1-asr-flash"
              and qa_cfg["stt_body"] == "json_template"
              and "X-DashScope-SSE" in qa_cfg["stt_headers"]
              and "${" not in qa_cfg["note"],
              qa_cfg.get("stt_url"))
        q_url, q_h, q_body, q_method = iv.stt_generic_request(
            wav, dict(qa_cfg, api_key="sk-test"), "zh-CN")
        q_obj = json.loads(q_body)
        check("Qwen-Audio-3.1: 生成 input.messages + parameters 原生请求体",
              q_method == "POST" and q_h["X-DashScope-SSE"] == "disable"
              and q_obj["model"] == "qwen-audio-3.1-asr-flash"
              and q_obj["input"]["messages"][0]["content"][0]["input_audio"]["data"]
              == "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
              and q_obj["parameters"] == {"format": "wav", "sample_rate": "16000"}
              and "choices" not in q_obj, q_obj.get("model"))
        check("Qwen-Audio-3.1: 顶层模型名改一次即覆盖原生请求体",
              json.loads(iv.stt_generic_request(
                  wav, dict(qa_cfg, model="qwen-audio-3.1-asr-flash-v2"),
                  "zh-CN")[2])["model"] == "qwen-audio-3.1-asr-flash-v2")
        check("Qwen-Audio-3.1: 响应 output.text 可解析",
              iv.parse_stt_generic(
                  {"output": {"text": "介绍一下你的 SAP 项目"}},
                  "output.text") == "介绍一下你的 SAP 项目")

        class QA31Response:
            def read(self):
                return json.dumps(
                    {"output": {"text": "介绍一下你的 SAP 项目"}}).encode("utf-8")
            def __enter__(self): return self
            def __exit__(self, *a): return False

        check("Qwen-Audio-3.1: recognize 端到端取回 output.text",
              iv.recognize(wav, dict(qa_cfg, api_key="sk-test"), "zh-CN",
                           lambda req, timeout=None: QA31Response())
              == "介绍一下你的 SAP 项目")

        from urllib.error import HTTPError as _HTTPError
        import io as _io

        def no_words(detail):
            def opener(req, timeout=None):
                raise _HTTPError(req.full_url, 400, "Bad Request", {},
                                 _io.BytesIO(json.dumps(detail).encode("utf-8")))
            return opener

        check("ASR 容错: 无词 400 按空识别处理，不打断面试",
              iv.recognize(wav, dict(qa_cfg, api_key="sk-test"), "zh-CN",
                           no_words({"code": "CLIENT_ERROR",
                                     "message": "ASR_RESPONSE_HAVE_NO_WORDS"}))
              == "")
        try:
            iv.recognize(wav, dict(qa_cfg, api_key="sk-test"), "zh-CN",
                         no_words({"code": "InvalidApiKey", "message": "bad key"}))
            check("ASR 容错: 其它 400 照常抛出", False)
        except _HTTPError:
            check("ASR 容错: 其它 400 照常抛出", True)

        class GenericResponse:
            def read(self):
                return json.dumps(
                    {"data": {"text": "介绍一下你的 SAP 项目"}}).encode("utf-8")
            def __enter__(self): return self
            def __exit__(self, *a): return False

        generic_urls = []

        def generic_opener(req, timeout=None):
            generic_urls.append((req.full_url, req.get_method()))
            return GenericResponse()

        check("ASR: 通用 HTTP 走独立适配器而非 Azure",
              iv.recognize(wav, generic, "en-US", generic_opener)
              == "介绍一下你的 SAP 项目"
              and generic_urls[0][1] == "PUT"
              and "/v1/recognize" in generic_urls[0][0], generic_urls)
        try:
            iv.recognize(wav, {"type": "some_vendor_stt"}, "en-US",
                         generic_opener)
            check("ASR: 未知协议被拒绝，不再误走 Azure", False)
        except iv.InterviewError:
            check("ASR: 未知协议被拒绝，不再误走 Azure", True)

        class OaiResponse:
            def read(self):
                return json.dumps({"text": "介绍一下你的 SAP 项目"}).encode("utf-8")

            def __enter__(self): return self

            def __exit__(self, *a): return False

        oai_urls = []

        def oai_opener(req, timeout=None):
            oai_urls.append(req.full_url)
            return OaiResponse()

        check("面试: 非 Azure 服务走自定义识别地址",
              iv.recognize(wav, oai, "en-US", oai_opener) == "介绍一下你的 SAP 项目"
              and oai_urls[0].startswith(
                  "https://asr.example.invalid/v1/audio/transcriptions"))
        for name, cfg_bad in (("缺 key", dict(speech_cn, api_key="")),
                              ("非 Azure 类型", dict(speech_cn,
                                                    type="faster_whisper"))):
            try:
                iv.stt_endpoint(cfg_bad)
                check("面试: 拒绝" + name, False)
            except iv.InterviewError:
                check("面试: 拒绝" + name, True)
        url, headers = iv.stt_request(wav, speech_cn, "zh-CN")
        check("面试: STT 请求头与语种",
              "language=zh-CN" in url
              and headers["Ocp-Apim-Subscription-Key"] == "k"
              and "samplerate=16000" in headers["Content-Type"])
        check("面试: 解析识别结果",
              iv.parse_stt({"RecognitionStatus": "Success",
                            "DisplayText": "Hello"}) == "Hello")
        check("面试: 静音片段返回空串",
              iv.parse_stt({"RecognitionStatus": "InitialSilenceTimeout"}) == "")
        try:
            iv.parse_stt({"RecognitionStatus": "BadRequest"})
            check("面试: 识别错误向上抛出", False)
        except iv.InterviewError:
            check("面试: 识别错误向上抛出", True)

        class SttResponse:
            def __init__(self, text): self.text = text
            def read(self):
                return json.dumps({"RecognitionStatus": "Success",
                                   "DisplayText": self.text}).encode("utf-8")
            def __enter__(self): return self
            def __exit__(self, *a): return False

        calls = []

        def stt_opener(req, timeout=None):
            calls.append(req.full_url)
            return SttResponse("Tell me about your SAP project")

        def zh_opener(req, timeout=None):
            # 模拟按英文识别却吐出汉字（中文提问 + 术语），触发中文重识别
            calls.append(req.full_url)
            return SttResponse("介绍一下你的 SAP BAdI 经验")

        check("面试: recognize 拿到文本",
              iv.recognize(wav, speech_cn, "en-US", stt_opener)
              == "Tell me about your SAP project")
        calls.clear()
        iv.recognize(wav, custom, "zh-CN", stt_opener)
        check("面试: 请求确实打到自定义完整地址",
              calls[0].startswith(gw + "&language=zh-CN"), calls[0])
        check("面试: 中文夹英文术语仍判中文",
              iv.detect_lang("介绍一下你的 SAP BAdI 经验") == "zh"
              and iv.detect_lang("Tell me about your project") == "en")
        calls.clear()
        text, lang = iv.recognize_auto(wav, speech_cn, zh_opener)
        check("面试: 自动语种中文重识别",
              lang == "zh" and len(calls) == 2 and "zh-CN" in calls[1],
              "%s %s" % (lang, len(calls)))
        calls.clear()

        def multilingual_opener(req, timeout=None):
            calls.append(req)
            return FakeResponse({"choices": [{"message": {"content": "谈谈你的 SAP 项目"}}]})

        multilingual = {**bailian, "stt_json_template": bailian["stt_json_template"].replace(
            '"language": "{{lang_short}}", ', '')}
        text, lang = iv.recognize_auto(wav, multilingual, multilingual_opener)
        check("面试: 已自行判语种的 ASR 中文段只发一次请求",
              text == "谈谈你的 SAP 项目" and lang == "zh" and len(calls) == 1)

        check("面试: 复盘按记录原因标注未回答轮次",
              "本轮未生成回答" in json.dumps(
                  iv.review_messages({"turns": [{"text": "Q", "lang": "en"}]}),
                  ensure_ascii=False)
              and "开关都未打开" in json.dumps(
                  iv.review_messages({"turns": [{"text": "Q", "lang": "en",
                                                 "skip_reason": "开关都未打开"}]}),
                  ensure_ascii=False))

        sess = iv.Session(home)
        sess.start("practice")
        sess.add_turn({"lang": "en", "text": "Tell me about your project",
                       "translation_zh": "谈谈你的项目",
                       "answer_zh": "中文稿", "answer_en": "English draft"})
        check("面试: 会话累积与导出",
              sess.state()["count"] == 1
              and "中文稿" in sess.export_md() and "English draft" in sess.export_md())
        check("面试: 历史喂给下一轮", len(sess.history()) == 2)
        practice_end = sess.end()
        check("面试: 练习模式结束落盘",
              bool(practice_end["file"]) and Path(practice_end["file"]).exists())
        sess.start("live")
        sess.add_turn({"lang": "en", "text": "Why now?", "answer_zh": ""})
        live_end = sess.end()
        check("面试: 实战模式不落盘", live_end["file"] == "")

        check("persona: 英文轮输出译文字段",
              persona.parse_answer('{"translation_zh":"译文",'
                                   '"answer_zh":"中文","answer_en":"English"}',
                                   "en")["translation_zh"] == "译文")
        check("persona: 中文轮译文与英文稿置空",
              persona.parse_answer('{"translation_zh":"译文",'
                                   '"answer_zh":"中文","answer_en":"English"}',
                                   "zh")["translation_zh"] == ""
              and persona.parse_answer('{"answer_zh":"中文"}', "zh")["answer_en"] == "")
        with env(**FAKE_ENV):
            sysmsg = persona.build_messages("Q", source_lang="en")[0]["content"]
        check("persona: system prompt 含三字段与译文规则",
              "translation_zh" in sysmsg and "忠实原意" in sysmsg)

        check("persona: 开关组合决定字段，中文提问不翻译",
              persona.turn_fields("en", True, True) == ["translation_zh", "answer_en"]
              and persona.turn_fields("en", True, False) == ["translation_zh"]
              and persona.turn_fields("en", False, True) == ["answer_en"]
              and persona.turn_fields("zh", True, True) == ["answer_zh"]
              and persona.turn_fields("zh", True, False) == [])
        with env(**FAKE_ENV):
            en_both = persona.build_turn_messages(
                "Q", source_lang="en", use_kb=False,
                want_translation=True, want_answer=True)[0]["content"]
            en_only = persona.build_turn_messages(
                "Q", source_lang="en", use_kb=False,
                want_translation=False, want_answer=True)[0]["content"]
            zh_both = persona.build_turn_messages(
                "Q", source_lang="zh", use_kb=False,
                want_translation=True, want_answer=True)[0]["content"]
        check("persona: 英文轮只要英文稿，中文轮只要中文稿",
              '"answer_en": "..."' in en_both
              and '"answer_zh": "..."' not in en_both
              and '"translation_zh": "..."' in en_both
              and '"answer_zh": "..."' in zh_both
              and '"answer_en": "..."' not in zh_both
              and '"translation_zh"' not in zh_both
              and '"translation_zh"' not in en_only)
        check("persona: parse_turn 只保留本轮期望字段",
              persona.parse_turn(
                  '{"translation_zh":"译文","answer_zh":"中文","answer_en":"English"}',
                  "en", True, True)
              == {"translation_zh": "译文", "answer_zh": "",
                  "answer_en": "English", "degraded": False})
        truncated = persona.parse_turn(
            '{"translation_zh":"译文","answer_en":"I have ten years of ABAP',
            "en", True, True)
        check("persona: 半截 JSON 按字段归位，不漏报文结构",
              truncated["answer_en"] == "I have ten years of ABAP"
              and truncated["translation_zh"] == "译文"
              and truncated["degraded"] is True)

        http("POST", "/api/provider/use", {"profile": "azure-cn-qwen"}, base)
        orig_call, orig_rec = persona.call_llm, iv.recognize

        review_modes = []
        mock_calls = []

        def mock_call(messages, cfg, opener=None, json_mode=True):
            mock_calls.append(messages)
            if any("面试复盘教练" in str(m.get("content", "")) for m in messages):
                review_modes.append(json_mode)
            return '{"translation_zh": "谈谈你的 SAP 项目", "answer_zh": "中文稿", "answer_en": "English draft"}'

        persona.call_llm = mock_call
        iv.recognize = lambda raw, speech, language, opener=None: \
            "Tell me about your SAP project"
        try:
            with env(**FAKE_ENV):
                code, st3 = http("GET", "/api/state", base=base)
                check("api: 状态含面试就绪信息",
                      code == 200 and "interview" in st3
                      and st3["interview"]["speech"]["ready"] is True,
                      str(st3.get("interview"))[:160])
                check("api: 状态回传实际识别地址",
                      ".stt.speech." in st3["interview"]["speech"].get("url", ""),
                      st3["interview"]["speech"])
                check("api: 状态回传 ASR 模型名字段",
                      "model" in st3["interview"]["speech"])
                # ---- 模型管理 API：页面新增 / 编辑 / 删除，全部落在临时配置 ----
                code, saved = http("POST", "/api/provider/save", {
                    "kind": "llm", "id": "mm-test",
                    "data": {"type": "openai_compatible",
                             "base_url": "https://example.invalid/v1",
                             "model": "qwen-test", "supports_json_mode": True,
                             "temperature": 0.5, "max_tokens": 900,
                             "timeout_s": 25},
                    "secret": "sk-mm-test", "persist_secret": False}, base=base)
                check("api: 页面可新增模型且常用参数按数值落盘",
                      code == 200 and saved.get("provider", {}).get("temperature") == 0.5
                      and saved.get("provider", {}).get("max_tokens") == 900
                      and "mm-test" in saved.get("state", {}).get(
                          "provider", {}).get("llm_providers", {}),
                      saved.get("error") or saved.get("provider"))
                code, edited = http("POST", "/api/provider/save", {
                    "kind": "llm", "id": "mm-test",
                    "data": {"model": "qwen-edited", "temperature": 0.7},
                    "secret": "", "persist_secret": True}, base=base)
                check("api: 编辑模型只改提交字段、不丢已有参数",
                      code == 200
                      and edited["provider"]["model"] == "qwen-edited"
                      and edited["provider"]["temperature"] == 0.7
                      and edited["provider"]["max_tokens"] == 900
                      and edited["provider"]["api_key"]
                      == "${COPILOT_LLM_MM_TEST_KEY}",
                      edited.get("provider"))
                code, removed = http("POST", "/api/provider/delete", {
                    "kind": "llm", "id": "mm-test", "drop_refs": True}, base=base)
                check("api: 页面可删除自建模型",
                      code == 200 and "mm-test" not in
                      removed["state"]["provider"]["llm_providers"],
                      removed.get("error"))
                check("api: 状态含 ASR 协议清单与两侧独立生效项",
                      "generic_http_stt" in st3["provider"].get("asr_types", {})
                      and set(st3["provider"]["active_ids"]) == {"speech", "llm"}
                      and bool(st3["interview"]["speech"].get("label")))
                code, js = http("GET", "/interview.js", base=base)
                check("api: 面试脚本可访问", code == 200 and "InterviewView" in js)
                code, home_html = http("GET", "/", base=base)
                check("api: 首页含面试入口", 'id="viewInterview"' in home_html)

                code, started = http("POST", "/api/interview/start",
                                     {"mode": "practice"}, base)
                check("api: 开启练习会话",
                      code == 200 and started["session"]["active"] is True)
                b64wav = base64.b64encode(wav).decode("ascii")
                code, asr = http("POST", "/api/interview/asr",
                                 {"audio_b64": b64wav, "language": "en-US"}, base)
                check("api: 音频段识别", code == 200
                      and asr.get("text") == "Tell me about your SAP project"
                      and asr.get("duration_ms") == 500, str(asr)[:160])
                code, badwav = http("POST", "/api/interview/asr",
                                    {"audio_b64": base64.b64encode(b"bad").decode(),
                                     "language": "en-US"}, base)
                check("api: 非法音频被拦截", code == 400 and "error" in badwav)
                def turn(text, lang, translate, answers):
                    return http("POST", "/api/interview/turn",
                                {"text": text, "lang": lang,
                                 "translate": translate,
                                 "answers": answers}, base)

                # A. 两个开关都关：零 LLM 调用，只记原文，写明原因
                c0 = len(mock_calls)
                code, off = turn(asr["text"], asr["lang"], False, False)
                off_t = off.get("turn", {}) if code == 200 else {}
                check("api: 开关全关只记原文且零 LLM 调用",
                      code == 200 and off_t.get("translation_zh") == ""
                      and off_t.get("answer_zh") == ""
                      and off_t.get("answer_en") == ""
                      and "开关都未打开" in off_t.get("skip_reason", "")
                      and len(mock_calls) == c0, str(off)[:220])

                http("POST", "/api/interview/clear", {}, base)
                http("POST", "/api/interview/start", {"mode": "practice"}, base)

                # B. 只开翻译：给中文译文，不生成答案
                code, tr_only = turn(asr["text"], "en", True, False)
                t = tr_only.get("turn", {}) if code == 200 else {}
                check("api: 只开翻译不生成 AI 答案",
                      t.get("translation_zh") == "谈谈你的 SAP 项目"
                      and t.get("answer_zh") == "" and t.get("answer_en") == ""
                      and mock_calls[-1] == iv.translate_messages(asr["text"])
                      and isinstance(t.get("generation_ms"), int),
                      str(tr_only)[:220])

                # C. 只开回答：英文提问只给英文稿，不给译文与中文稿
                code, an_only = turn(asr["text"], "en", False, True)
                t = an_only.get("turn", {}) if code == 200 else {}
                check("api: 英文提问只给英文回答稿",
                      t.get("answer_en") == "English draft"
                      and t.get("answer_zh") == ""
                      and t.get("translation_zh") == "", str(an_only)[:220])

                # D. 中文提问：只给中文稿，翻译开关自动忽略
                code, zh_turn = turn("讲讲你的项目", "zh", True, True)
                t = zh_turn.get("turn", {}) if code == 200 else {}
                check("api: 中文提问只给中文稿且无需翻译",
                      t.get("answer_zh") == "中文稿"
                      and t.get("answer_en") == ""
                      and t.get("translation_zh") == "", str(zh_turn)[:220])

                # E. 两开关都开：一次 LLM 给译文 + 英文稿
                c0 = len(mock_calls)
                code, turned = turn(asr["text"], "en", True, True)
                t = turned.get("turn", {}) if code == 200 else {}
                check("api: 翻译+回答=一次请求给译文与英文稿",
                      t.get("translation_zh") == "谈谈你的 SAP 项目"
                      and t.get("answer_en") == "English draft"
                      and t.get("answer_zh") == ""
                      and len(mock_calls) == c0 + 1, str(turned)[:220])

                code, latest = http("GET", "/api/answer/latest", base=base)
                check("api: 最近回答同步给隐身窗",
                      code == 200
                      and latest["answer"]["answer_en"] == "English draft")
                code, ses = http("GET", "/api/interview/session", base=base)
                check("api: 会话可轮询", code == 200
                      and ses["session"]["count"] == 4)
                code, md = http("GET", "/api/interview/export?fmt=md", base=base)
                check("api: 导出 Markdown", code == 200
                      and "English draft" in md and "中文稿" in md)
                code, rep = http("POST", "/api/interview/review", {}, base)
                check("api: 生成复盘", code == 200 and rep.get("report"))
                check("api: 复盘不强制 JSON 输出", review_modes == [False],
                      review_modes)
                code, ended = http("POST", "/api/interview/end", {}, base)
                check("api: 结束练习会话并落盘",
                      code == 200 and ended["session"]["active"] is False
                      and bool(ended["file"]))

                # 模式只管落盘：实战模式打开开关照常生成，关掉照常不生成
                http("POST", "/api/interview/start", {"mode": "live"}, base)
                code, live_turn = turn("Why now?", "en", True, True)
                lt = live_turn.get("turn", {}) if code == 200 else {}
                check("api: 模式不再限制输出，实战开关打开仍生成答案",
                      lt.get("answer_en") == "English draft"
                      and lt.get("translation_zh") == "谈谈你的 SAP 项目",
                      str(live_turn)[:220])
                code, live_off = turn("Why not?", "en", False, False)
                lot = live_off.get("turn", {}) if code == 200 else {}
                check("api: 实战模式开关全关仍只记原文",
                      lot.get("answer_en") == ""
                      and lot.get("translation_zh") == ""
                      and "开关都未打开" in lot.get("skip_reason", ""),
                      str(live_off)[:220])
                code, live_end = http("POST", "/api/interview/end", {}, base)
                check("api: 实战模式不落盘",
                      code == 200 and live_end["file"] == "")
                code, cleared = http("POST", "/api/interview/clear", {}, base)
                check("api: 清空记录不误开会话",
                      code == 200 and cleared["session"]["count"] == 0
                      and cleared["session"]["active"] is False)

                _, started = http("POST", "/api/interview/start", {"mode": "practice"}, base)
                old_id = started["session"]["id"]
                check("api: 连续创建的会话使用不同 ID",
                      old_id != cleared["session"]["id"])
                entered, release = threading.Event(), threading.Event()

                def delayed_llm(*args, **kwargs):
                    entered.set()
                    release.wait(timeout=3)
                    raise TimeoutError()

                persona.call_llm = delayed_llm
                release.set()
                code, timed_out = turn("Keep this transcript", "en", True, False)
                check("api: LLM 超时仍保存识别原文并标注原因",
                      code == 200 and timed_out["turn"]["text"] == "Keep this transcript"
                      and "TimeoutError" in timed_out.get("generation_error", ""))
                entered.clear()
                release.clear()
                race_result = []
                race_worker = threading.Thread(target=lambda: race_result.append(
                    http("POST", "/api/interview/turn", {"text": "Old question",
                         "lang": "en", "answers": True, "session_id": old_id}, base)))
                race_worker.start()
                reached_llm = entered.wait(timeout=2)
                try:
                    _, fresh = http("POST", "/api/interview/start", {"mode": "practice"}, base)
                finally:
                    release.set()
                    race_worker.join(timeout=3)
                _, current = http("GET", "/api/interview/session", base=base)
                check("api: 在途旧会话结果不会写进新会话",
                      reached_llm and race_result and race_result[0][0] == 400
                      and current["session"]["id"] == fresh["session"]["id"]
                      and current["session"]["count"] == 0)
        finally:
            persona.call_llm, iv.recognize = orig_call, orig_rec

        # 流式 ASR 使用内存 WebSocket 替身，HTTP 路由与服务端事件线程走真实代码。
        rt = webapp.realtime_asr
        path = "/api/interview/asr/realtime/"
        sent = []
        dialed = []
        ready_preview = threading.Event()

        class MockRealtimeSocket:
            def __init__(self):
                self.messages = queue.Queue()

            def send(self, raw):
                data = json.loads(raw)
                sent.append(data)
                if data["type"] == "session.update":
                    self.messages.put({"type": "session.updated"})
                elif data["type"] == "input_audio_buffer.append":
                    self.messages.put({"type": rt.EVENT_PREFIX + "text", "item_id": "one",
                                       "text": "谈谈", "stash": "你的 SAP 项目", "language": "zh"})
                elif data["type"] == "session.finish":
                    self.messages.put({"type": "input_audio_buffer.speech_stopped", "item_id": "one"})
                    final = {"type": rt.EVENT_PREFIX + "completed", "item_id": "one",
                             "transcript": "谈谈你的 SAP 项目。", "language": "zh"}
                    self.messages.put(final)
                    self.messages.put(final)
                    self.messages.put({"type": "session.finished"})

            def recv(self, timeout=None):
                try:
                    return json.dumps(self.messages.get(timeout=timeout))
                except queue.Empty as exc:
                    raise TimeoutError() from exc

            def close(self):
                self.messages.put({"type": "session.finished"})

        def mock_connect(url, **kwargs):
            dialed.append((url, kwargs))
            return MockRealtimeSocket()

        with env(COPILOT_SPEECH="bailian-qwen-realtime", COPILOT_SPEECH_KEY="fake-rt-key"):
            ctx.cfg.set_provider("speech", "bailian-qwen-realtime", {
                "stt_url": "wss://workspace.example.invalid/api-ws/v1/realtime?model=stale",
            })
            sdk = ctx.cfg.speech_sdk_kwargs()
            check("流式 ASR: 可配置地址与模型被识别且一份密钥只留在服务端",
                  sdk["type"] == iv.REALTIME_STT and not any(
                      p.startswith("[speech:") for p in ctx.cfg.check())
                  and "model=qwen3-asr-flash-realtime" in iv.stt_effective_url(sdk))
            with patch.object(rt, "_connect", mock_connect):
                code, opened = http("POST", path + "open",
                                    {"mode": "practice", "language": "auto", "silence_ms": 400}, base)
                check("流式 ASR: 云端握手使用服务端密钥、PCM16 与可配置 VAD",
                      code == 200 and "fake-rt-key" not in str(opened)
                      and dialed[-1][0].endswith("model=qwen3-asr-flash-realtime")
                      and dialed[-1][1]["additional_headers"]["Authorization"] == "Bearer fake-rt-key"
                      and sent[-1]["session"]["sample_rate"] == 16000
                      and sent[-1]["session"]["turn_detection"]["silence_duration_ms"] == 400
                      and sent[-1]["session"]["input_audio_transcription"] == {})
                stream_id = opened["stream_id"]
                audio = base64.b64encode(b"\0\0" * 1600).decode("ascii")
                code, _ = http("POST", path + "audio", {"stream_id": "stale", "audio": audio}, base)
                check("流式 ASR: 不接受旧连接的音频", code == 400)
                stream = ctx.realtime
                publish = stream._publish

                def track_preview(event):
                    publish(event)
                    if event["type"] == "partial":
                        ready_preview.set()

                with patch.object(stream, "_publish", track_preview):
                    code, _ = http("POST", path + "audio", {"stream_id": stream_id, "audio": audio}, base)
                    reached_preview = ready_preview.wait(timeout=2)
                _, preview = http("GET", "/api/interview/asr/live", base=base)
                check("流式 ASR: 音频送达后可预览未断句的转写",
                      code == 200 and reached_preview
                      and preview["preview"] == {"type": "partial", "text": "谈谈你的 SAP 项目"}
                      and sent[-1]["audio"] == audio)
                code, finished = http("POST", path + "finish", {"stream_id": stream_id}, base)
                finals = [event for event in finished.get("events", []) if event["type"] == "final"]
                check("流式 ASR: 结束前等最后一句并合并重复事件",
                      code == 200 and finished.get("finalized") and ctx.realtime is None
                      and len(finals) == 1 and finals[0]["text"] == "谈谈你的 SAP 项目。"
                      and finals[0]["lang"] == "zh")
                code, new_stream = http("POST", path + "open", {"language": "en-US"}, base)
                check("流式 ASR: 下次开场可指定英文",
                      code == 200 and sent[-1]["session"]["input_audio_transcription"] == {"language": "en"})
                code, canceled = http("POST", path + "finish",
                                      {"stream_id": new_stream["stream_id"], "cancel": True}, base)
                check("流式 ASR: 取消立即断开且不提交尾段",
                      code == 200 and canceled["events"] == [] and ctx.realtime is None
                      and sent[-1]["type"] == "session.update")

        # 共享模式本机请求使用真实 HTTP，手机改用模拟局域网客户端地址。
        old_lan, old_token = ctx.lan, ctx.token
        ctx.share_safe, ctx.lan, ctx.token = True, True, "share-test"
        real_local_check = webapp.Handler._reject_local_ui
        try:
            check("共享模式: 本机有 token 也读不到页面和会话",
                  all(http("GET", path + "?token=share-test", base=base) == (403, "")
                      for path in ("/", "/api/interview/session", "/interview.js")))
            check("启动器: 端口冲突时识别正在运行的手机共享后台",
                  webapp.sharing_service_listening(srv.server_port))

            def phone_client(handler):
                handler.client_address = ("192.168.87.22", handler.client_address[1])
                return real_local_check(handler)

            class FakeAudio:
                paContinue, paInt16 = 0, 8

                class PyAudio:
                    def get_default_wasapi_loopback(self):
                        return {"name": "Test speakers", "defaultSampleRate": 48000,
                                "maxInputChannels": 2, "index": 1}

                    def open(self, **kwargs):
                        callback = kwargs["stream_callback"]

                        class Stream:
                            def start_stream(self):
                                from struct import pack
                                for sample in (1200,) * 5 + (0,) * 5:
                                    callback(pack("<hh", sample, sample) * 4800, 4800, {}, 0)

                            def close(self):
                                pass

                            def is_active(self):
                                return True

                        return Stream()

                    def terminate(self):
                        pass

            audio = webapp.headless_audio
            with (env(**FAKE_ENV), patch.object(webapp.Handler, "_reject_local_ui", phone_client),
                  patch.object(audio, "_driver", return_value=FakeAudio),
                  patch.object(iv, "recognize", return_value="An audio-only test.")):
                check("共享模式: 手机必须持有访问令牌",
                      http("GET", "/", base=base)[0] == 403
                      and http("GET", "/?token=share-test", base=base)[0] == 200)
                code, opened = http("POST", "/api/interview/capture/start?token=share-test",
                                    {"mode": "practice", "source": "tab"}, base)
                check("共享模式: 手机启动电脑音频采集",
                      code == 200 and opened["capture"]["source"] == "Test speakers")
                code, ended = http("POST", "/api/interview/end?token=share-test",
                                   {"session_id": opened["session"]["id"]}, base)
                check("共享模式: 48kHz 双声道转写后正常结束落盘",
                      code == 200 and ended["session"]["count"] == 1
                      and ended["session"]["turns"][0]["text"] == "An audio-only test."
                      and ended.get("file") and ctx.capture is None)
        finally:
            ctx.share_safe, ctx.lan, ctx.token = False, old_lan, old_token

    finally:
        srv.shutdown()
        srv.server_close()
        worker.join(timeout=2)

failed = [name for name, ok, _ in results if not ok]
for name, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + name
          + (("  -> " + detail) if detail and not ok else ""))
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
