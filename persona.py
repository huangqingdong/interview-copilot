"""人设驱动的面试回答生成。

职责：加载 / 切换人设 → 构造 LLM messages → 组装请求 → 解析回答。
面试主链路走 generate_turn：按「翻译 / 回答」开关输出，答案语言跟随提问语种；
generate_answer 是保留的兼容入口，仍返回中英双版。
与 provider_config.py 解耦：那边只管 key 与连接，这里只管内容与 prompt。
零第三方依赖。

    from provider_config import ProviderConfig
    import persona
    store = persona.PersonaStore()
    answer = persona.generate_answer(
        "Tell me about a complex delivery you led",
        cfg=ProviderConfig(), store=store, source_lang="en")
    print(answer["answer_zh"], answer["answer_en"])
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import knowledge_base

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
EXAMPLE_PATH = CONFIG_DIR / "persona.example.json"
PERSONA_PATH = CONFIG_DIR / "persona.json"

ENV_PERSONA = "COPILOT_PERSONA"
ANSWER_FIELDS = ("answer_zh", "answer_en")


class PersonaError(Exception):
    pass


SYSTEM_TEMPLATE = """你是面试回答助手。基于下面给定的候选人画像与资料依据，为候选人口头回答面试官的问题生成参考稿。

【候选人画像】
__PERSONA__

__CUSTOM_PROMPT____KB_CONTEXT__【硬性规则】
1. 只能使用__FACT_SOURCES__里出现的事实（项目、岗位、年限、技术栈、数字）。里面没有的不得编造；可改用可迁移的经验作答，或明确说明「这块我没有直接做过」并给出思路。
2. 直接回答问题，不复述问题，不写「作为一位……」这类开场套话，不加标题和 Markdown。
3. 用「结论 → 依据 → 结果」或 STAR 组织，控制在 60 秒口述长度（中文约 150–220 字）。
4. 遵守画像里的「边界与约束」与「表达风格」。
5. 只输出一个合法的 json 对象（JSON object），字段固定为 __FIELDS__，取值均为字符串；不要解释、不要前后缀。
6. __LANG_RULE__
7. answer_en 不是 answer_zh 的逐句直译，而是同一内容用自然、可直接念出口的英文重新表达。
__KB_RULE__
__TRANSLATION_RULE__
__EXAMPLES__"""

LANG_RULE = {
    "en": "本轮问题为英文：answer_zh 写中文对照稿，answer_en 写可直接念出口的英文回答稿，两版都要给。",
    "zh": "本轮问题为中文：answer_zh 写可直接念出口的中文回答稿，answer_en 固定为空字符串。",
}

TRANSLATION_RULE = {
    "en": "9. translation_zh 写【本轮问题（原文）】的中文译文：忠实原意、不补充不解释、技术术语保留原文。",
    "zh": "9. 本轮为中文提问，translation_zh 固定为空字符串。",
}
ANSWER_SCHEMA = '{"translation_zh": "...", "answer_zh": "...", "answer_en": "..."}'

KB_RULE = "8. 回答里的具体事实必须能在【资料依据】或画像中找到；资料里没有的细节一律不补，也不要在回答里提及文件名、编号或「根据我的资料」这类措辞。"

CUSTOM_SECTION = """【候选人自定义要求】
{prompt}

这段要求只决定回答的侧重、口吻与取舍；不得据此引入画像和资料里没有的事实，也不得与下方硬性规则冲突（冲突时以硬性规则为准）。
"""


class PersonaStore:
    """多套人设 + 当前选中项。persona.json 不存在时自动回退到 example 模板。"""

    def __init__(self, path=PERSONA_PATH, example=EXAMPLE_PATH):
        self.path = Path(path)
        self.example = Path(example)
        self._stamp = None
        self._raw = {}
        self._source = self.example
        self._override = None
        self._reload(force=True)

    def _reload(self, force=False):
        source = self.path if self.path.exists() else self.example
        if not source.exists():
            raise PersonaError(f"找不到人设文件：{self.path} 与 {self.example} 均不存在")
        stat = source.stat()
        stamp = (str(source), stat.st_mtime_ns, stat.st_size)
        if not force and stamp == self._stamp:
            return
        try:
            self._raw = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PersonaError(f"人设文件 JSON 有误: {source} -> {exc}") from exc
        if "active" not in self._raw or "personas" not in self._raw:
            raise PersonaError(f"人设文件缺少 active / personas 字段: {source}")
        self._source = source
        self._stamp = stamp

    @property
    def raw(self):
        self._reload()
        return self._raw

    @property
    def using_example(self):
        self._reload()
        return self._source == self.example

    @property
    def active(self):
        return (os.environ.get(ENV_PERSONA) or self._override
                or self.raw["active"])

    def set_override(self, name):
        self._override = name
        return self

    def list(self):
        return self.raw["personas"]

    def current(self):
        name = self.active
        pool = self.list()
        if name not in pool:
            raise PersonaError(f"未知人设「{name}」，可用: {', '.join(pool)}")
        item = copy.deepcopy(pool[name])
        item["id"] = name
        return item

    def use(self, name):
        self._reload()
        if name not in self._raw["personas"]:
            raise PersonaError(
                f"未知人设「{name}」，可用: {', '.join(self._raw['personas'])}")
        self._raw["active"] = name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        self._stamp = None
        self._reload(force=True)
        return self

    def save(self, name: str, data: dict):
        """写入或更新一套人设，并设为当前。"""
        self._reload()
        item = copy.deepcopy(data)
        item.pop("id", None)
        self._raw["personas"][name] = item
        self._raw["active"] = name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        self._stamp = None
        self._reload(force=True)
        return self

    def delete(self, name: str):
        """删除一套人设；不允许删掉最后一套。"""
        self._reload()
        pool = self._raw["personas"]
        if name not in pool:
            raise PersonaError(f"未知人设「{name}」")
        if len(pool) <= 1:
            raise PersonaError("至少保留一套人设")
        del pool[name]
        if self._raw["active"] == name:
            self._raw["active"] = next(iter(pool))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        self._stamp = None
        self._reload(force=True)
        return self

    def init(self):
        """从模板生成本地 persona.json；已存在则不覆盖。"""
        if self.path.exists():
            return False
        self.path.write_text(
            self.example.read_text(encoding="utf-8"), encoding="utf-8")
        self._stamp = None
        self._reload(force=True)
        return True


def render_persona(persona: dict) -> str:
    lines = []
    single = (("label", "定位"), ("target_role", "目标岗位"),
              ("years", "从业年限"), ("domain", "领域"))
    for key, label in single:
        if persona.get(key):
            lines.append(f"- {label}：{persona[key]}")
    if persona.get("stack"):
        lines.append("- 技术栈：" + "、".join(str(x) for x in persona["stack"]))
    for key, label in (("highlights", "关键经历"),
                       ("constraints", "边界与约束")):
        values = persona.get(key) or []
        if values:
            lines.append(f"- {label}：")
            lines.extend(f"  - {v}" for v in values)
    if persona.get("style"):
        lines.append(f"- 表达风格：{persona['style']}")
    return "\n".join(lines)


def render_custom_prompt(persona: dict) -> str:
    """手写提示词作为独立段落注入；不改画像字段，也不放松事实约束。"""
    text = str(persona.get("custom_prompt") or "").strip()
    return CUSTOM_SECTION.format(prompt=text) if text else ""


def render_examples(persona: dict) -> str:
    examples = (persona.get("examples") or [])[:3]
    if not examples:
        return ""
    parts = ["【参考范例】"]
    for item in examples:
        parts.append(f"Q: {item.get('q', '')}\nA: {item.get('a', '')}")
    return "\n\n".join(parts)


def _format_history(history) -> str:
    rows = []
    for item in (history or [])[-6:]:
        if isinstance(item, dict):
            rows.append(f"{item.get('role', '?')}: {item.get('content', '')}")
        else:
            rows.append(f"{item[0]}: {item[1]}")
    return "\n".join(rows)


_KB_CACHE: knowledge_base.KnowledgeBase | None = None


def _default_kb():
    global _KB_CACHE
    if _KB_CACHE is None:
        _KB_CACHE = knowledge_base.KnowledgeBase()
    else:
        _KB_CACHE.refresh()
    return _KB_CACHE


def build_messages(question: str, history=None, source_lang: str = "en",
                   store: PersonaStore | None = None, kb=None,
                   kb_k: int = 4, use_kb: bool = True) -> list[dict]:
    """构造 system + user 消息；人设与资料依据只进旁路，不影响流式字幕链路。"""
    store = store or PersonaStore()
    persona = store.current()
    lang = "zh" if str(source_lang).lower().startswith("zh") else "en"
    hits = []
    if use_kb:
        source_kb = kb if kb is not None else _default_kb()
        source_kb.refresh()
        if source_kb.chunks:
            hits = source_kb.search(question, k=kb_k)
    system = (SYSTEM_TEMPLATE
              .replace("__PERSONA__", render_persona(persona))
              .replace("__CUSTOM_PROMPT__", render_custom_prompt(persona))
              .replace("__KB_CONTEXT__", knowledge_base.render_context(hits))
              .replace("__FACT_SOURCES__",
                       "【候选人画像】和【资料依据】" if hits else "【候选人画像】")
              .replace("__KB_RULE__", KB_RULE if hits else "")
              .replace("__FIELDS__", ANSWER_SCHEMA)
              .replace("__LANG_RULE__", LANG_RULE[lang])
              .replace("__TRANSLATION_RULE__", TRANSLATION_RULE[lang])
              .replace("__EXAMPLES__", render_examples(persona))).strip()
    parts = []
    hist = _format_history(history)
    if hist:
        parts.append("【最近对话】\n" + hist)
    parts.append(f"【输入语种】{lang}")
    parts.append(f"【本轮问题（原文）】\n{(question or '').strip()}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)}]


def build_request(messages: list[dict], cfg, json_mode: bool = True) -> tuple[str, dict, dict]:
    """返回 (url, headers, body)，可直接交给任意 HTTP 客户端。

    json_mode=False 用于复盘这类要 Markdown / 自由文本的输出；国内部分
    模型强制 JSON 时无法返回正常文本，调用方需按输出形态显式关闭。
    """
    llm = cfg.llm
    body = {"model": llm["model"], "messages": messages,
            "temperature": llm.get("temperature", 0.3),
            "max_tokens": llm.get("max_tokens", 600)}
    if (json_mode and llm.get("response_format") == "json_object"
            and llm.get("supports_json_mode", True)):
        body["response_format"] = {"type": "json_object"}
    if llm.get("type") == "azure_openai":
        base = llm["base_url"].rstrip("/")
        version = llm.get("api_version", "2024-10-21")
        url = (f"{base}/openai/deployments/{llm['model']}"
               f"/chat/completions?api-version={version}")
        headers = {"Content-Type": "application/json",
                   "api-key": llm["api_key"]}
    else:
        url = llm["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {llm['api_key']}"}
    return url, headers, body


def call_llm(messages: list[dict], cfg, opener=urlopen,
             json_mode: bool = True) -> str:
    url, headers, body = build_request(messages, cfg, json_mode)
    req = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                  headers=headers, method="POST")
    with opener(req, timeout=cfg.llm.get("timeout_s", 20)) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


_FIELD_TAG_RE = re.compile(
    r'["\']?(translation_zh|answer_zh|answer_en)["\']?\s*:\s*')
_ENVELOPE_RE = re.compile(r'^\s*[\[{]\s*|[\]}]\s*$')


def _salvage_fields(text: str) -> dict:
    """从被截断 / 畸形的 JSON 里按字段名切段，把值各归其位。

    推理档模型在 max_tokens 不足时，token 先被 reasoning_content 吃光，正文
    只剩半截 JSON：{"translation_zh":"...","answer_zh":"我在零售与
    json.loads 必然失败，旧逻辑会把整段报文塞进 answer_zh，界面上就露出
    translation_zh: 这类字段名。按字段名切段后，哪怕末尾没闭合引号，也能
    把译文与回答分开，最后一个字段保留已经输出的部分。
    """
    out = {}
    marks = list(_FIELD_TAG_RE.finditer(text))
    for i, mark in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        seg = text[mark.end():stop].strip()
        seg = re.sub(r'^["\']+', "", seg)
        seg = re.sub(r'["\']?\s*[,}\]]*\s*$', "", seg).strip()
        seg = seg.rstrip('"\' ,}]').strip()
        seg = seg.replace(r'\"', '"').replace(r'\n', "\n")
        if seg and mark.group(1) not in out:
            out[mark.group(1)] = seg
    return out


def _strip_envelope(text: str) -> str:
    """剥掉报文字段名、括号和字段间分隔符，保证回答里不出现 JSON 结构。"""
    t = _FIELD_TAG_RE.sub("", _ENVELOPE_RE.sub("", str(text or "").strip()))
    t = re.sub(r'["\']\s*,\s*["\']?', " ", t)
    return t.strip().strip('"\', ').strip()


def parse_answer(raw: str, source_lang: str = "en") -> dict:
    """容错解析模型输出；半截 JSON 先按字段抢救，不把报文结构显示给用户。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            data = None
    complete = (isinstance(data, dict)
                and any(k in data for k in ANSWER_FIELDS))
    degraded = not complete
    if not complete:
        salvaged = _salvage_fields(text)
        data = salvaged or {"answer_zh": _strip_envelope(text),
                            "answer_en": "", "translation_zh": ""}

    def clean(value) -> str:
        v = str(value or "").strip()
        return _strip_envelope(v) if _FIELD_TAG_RE.search(v) else v

    answer_zh = clean(data.get("answer_zh"))
    answer_en = clean(data.get("answer_en"))
    translation_zh = clean(data.get("translation_zh"))
    if str(source_lang).lower().startswith("zh"):
        answer_en = ""
        translation_zh = ""
    return {"translation_zh": translation_zh,
            "answer_zh": answer_zh, "answer_en": answer_en,
            "degraded": degraded}


def generate_answer(question: str, cfg, store: PersonaStore | None = None,
                    history=None, source_lang: str = "en", opener=urlopen,
                    kb=None, kb_k: int = 4, use_kb: bool = True) -> dict:
    """完整旁路：问题 + 人设 + 资料依据 → 中英双版回答。

    保留为兼容入口（配置页的旧调用与自检）；面试主链路改用 generate_turn，
    由「翻译 / 回答」开关决定输出字段，答案语言跟随提问语言。
    """
    messages = build_messages(question, history, source_lang, store,
                              kb=kb, kb_k=kb_k, use_kb=use_kb)
    return parse_answer(call_llm(messages, cfg, opener), source_lang)


# ---------------------------------------------------------------- 面试轮次
TURN_SYSTEM_TEMPLATE = """你是面试辅助助手。基于下面给定的候选人画像与资料依据，处理面试官本轮的问题。

【候选人画像】
__PERSONA__

__CUSTOM_PROMPT____KB_CONTEXT__【硬性规则】
1. 只能使用__FACT_SOURCES__里出现的事实（项目、岗位、年限、技术栈、数字）。里面没有的不得编造；可改用可迁移的经验作答，或明确说明「这块我没有直接做过」并给出思路。
2. 不复述问题，不写「作为一位……」这类开场套话，不加标题和 Markdown。
3. 遵守画像里的「边界与约束」与「表达风格」。
4. 只输出一个合法的 json 对象（JSON object），字段固定为 __FIELDS__，取值均为字符串；不要解释、不要前后缀。
__TASK_RULES__
__KB_RULE__
__EXAMPLES__"""

_TRANSLATION_TASK = ("translation_zh 写【本轮问题（原文）】的中文译文："
                     "忠实原意、不补充不解释、技术术语保留原文。")
_ANSWER_TASK = {
    "en": ("answer_en 写可直接念出口的英文回答稿：用「结论 → 依据 → 结果」"
           "或 STAR 组织，控制在 60 秒口述长度；不要输出中文回答。"),
    "zh": ("answer_zh 写可直接念出口的中文回答稿：用「结论 → 依据 → 结果」"
           "或 STAR 组织，控制在 60 秒口述长度（约 150–220 字）；不要输出英文回答。"),
}


def turn_fields(lang: str, want_translation: bool,
                want_answer: bool) -> list[str]:
    """本轮要向模型索要的字段；中文提问不需要翻译。"""
    out = []
    if want_translation and lang == "en":
        out.append("translation_zh")
    if want_answer:
        out.append("answer_en" if lang == "en" else "answer_zh")
    return out


def build_turn_messages(question: str, history=None, source_lang: str = "en",
                        store: PersonaStore | None = None, kb=None,
                        kb_k: int = 4, use_kb: bool = True,
                        want_translation: bool = False,
                        want_answer: bool = False) -> list[dict]:
    """按开关定制本轮的字段 schema 与任务规则，只发一次 LLM 请求。"""
    lang = "zh" if str(source_lang).lower().startswith("zh") else "en"
    fields = turn_fields(lang, want_translation, want_answer)
    store = store or PersonaStore()
    persona = store.current()
    hits = []
    if use_kb:
        source_kb = kb if kb is not None else _default_kb()
        source_kb.refresh()
        if source_kb.chunks:
            hits = source_kb.search(question, k=kb_k)
    rules = []
    if "translation_zh" in fields:
        rules.append(_TRANSLATION_TASK)
    if want_answer:
        rules.append(_ANSWER_TASK[lang])
    numbered = ["%d. %s" % (i + 5, r) for i, r in enumerate(rules)]
    schema = "{" + ", ".join('"%s": "..."' % f for f in fields) + "}"
    kb_rule = KB_RULE.replace("8.", "%d." % (len(rules) + 5)) if hits else ""
    system = (TURN_SYSTEM_TEMPLATE
              .replace("__PERSONA__", render_persona(persona))
              .replace("__CUSTOM_PROMPT__", render_custom_prompt(persona))
              .replace("__KB_CONTEXT__", knowledge_base.render_context(hits))
              .replace("__FACT_SOURCES__",
                       "【候选人画像】和【资料依据】" if hits else "【候选人画像】")
              .replace("__KB_RULE__", kb_rule)
              .replace("__FIELDS__", schema)
              .replace("__TASK_RULES__", "\n".join(numbered))
              .replace("__EXAMPLES__", render_examples(persona))).strip()
    parts = []
    hist = _format_history(history)
    if hist:
        parts.append("【最近对话】\n" + hist)
    parts.append(f"【输入语种】{lang}")
    parts.append(f"【本轮问题（原文）】\n{(question or '').strip()}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)}]


def parse_turn(raw: str, source_lang: str = "en",
               want_translation: bool = False,
               want_answer: bool = True) -> dict:
    """按本轮期望字段解析；半截 JSON 先按字段抢救，不把报文结构显示给用户。"""
    lang = "zh" if str(source_lang).lower().startswith("zh") else "en"
    fields = turn_fields(lang, want_translation, want_answer)
    if not fields:
        return {"translation_zh": "", "answer_zh": "", "answer_en": "",
                "degraded": False}
    primary = (("answer_en" if lang == "en" else "answer_zh")
               if want_answer else "translation_zh")
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            data = None
    complete = isinstance(data, dict) and any(k in data for k in fields)
    degraded = not complete
    if not complete:
        salvaged = {k: v for k, v in _salvage_fields(text).items()
                    if k in fields}
        data = salvaged or {primary: _strip_envelope(text)}

    def clean(v) -> str:
        v = str(v or "").strip()
        return _strip_envelope(v) if _FIELD_TAG_RE.search(v) else v

    picked = {k: clean(data.get(k)) for k in fields}
    return {"translation_zh": picked.get("translation_zh", ""),
            "answer_zh": picked.get("answer_zh", ""),
            "answer_en": picked.get("answer_en", ""),
            "degraded": degraded}


def generate_turn(question: str, cfg, store: PersonaStore | None = None,
                  history=None, source_lang: str = "en", opener=urlopen,
                  kb=None, kb_k: int = 4, use_kb: bool = True,
                  want_translation: bool = False,
                  want_answer: bool = False) -> dict:
    """按「翻译 / 回答」开关生成一轮；两个开关都关时完全不调用 LLM。"""
    lang = "zh" if str(source_lang).lower().startswith("zh") else "en"
    if not turn_fields(lang, want_translation, want_answer):
        return {"translation_zh": "", "answer_zh": "", "answer_en": "",
                "degraded": False}
    messages = build_turn_messages(
        question, history, lang, store, kb=kb, kb_k=kb_k, use_kb=use_kb,
        want_translation=want_translation, want_answer=want_answer)
    return parse_turn(call_llm(messages, cfg, opener), lang,
                      want_translation, want_answer)


BUILD_SYSTEM = """你是简历解析助手。从下面给出的候选人资料中，提取用于面试人设的结构化字段。

只输出一个合法的 json 对象（JSON object），字段固定为：
{"label": "...", "target_role": "...", "years": "...", "domain": "...", "stack": ["..."], "highlights": ["..."]}

规则：
1. 每个字段只能使用资料里明确出现的信息，不得推测、补全或编造公司名、数字、年限。
2. 资料里没有的字段置为空字符串或空数组。
3. highlights 每条一句完整事实（做了什么、结果如何），最多 6 条，按重要性排序。
4. 不要把客户名称、合同金额、内部编号写进任何字段，统一用「某客户」代替。
5. 【用户对人设的要求】只影响字段的取舍、侧重与措辞；不得据此补充资料里没有的事实，资料中无对应信息时留空。
6. 不要输出解释、Markdown 或前后缀。"""


def build_persona_messages(kb, max_chars: int = 9000,
                           user_prompt: str = "") -> list[dict]:
    parts, used = [], 0
    prompt = str(user_prompt or "").strip()
    if prompt:
        parts.append("【用户对人设的要求】\n" + prompt)
        used = len(prompt)
    for chunk in kb.chunks:
        text = f"[{chunk['source']} / {chunk.get('heading', '')}]\n{chunk['text']}"
        if used + len(text) > max_chars:
            break
        parts.append(text)
        used += len(text)
    return [{"role": "system", "content": BUILD_SYSTEM},
            {"role": "user", "content": "\n\n".join(parts)}]


def _parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise PersonaError("模型未返回 JSON，无法生成人设")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise PersonaError(f"模型返回的 JSON 无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise PersonaError("模型返回的不是 JSON 对象")
    return data


def build_persona_from_kb(cfg, store: PersonaStore | None = None, kb=None,
                          name: str = "kb-persona", opener=urlopen,
                          user_prompt: str | None = None) -> dict:
    """从资料库生成人设的事实字段；手写提示词、constraints、style、examples 保留。"""
    store = store or PersonaStore()
    kb = kb or knowledge_base.KnowledgeBase()
    kb.refresh()
    if not kb.chunks:
        raise PersonaError(
            "kb/ 目录没有可索引的资料，请先放入 .md / .txt / .json / .csv / .docx 文件")
    prompt = (str(store.current().get("custom_prompt") or "")
              if user_prompt is None else str(user_prompt))
    generated = _parse_json_object(
        call_llm(build_persona_messages(kb, user_prompt=prompt), cfg, opener))
    merged = dict(store.current())
    for field in ("label", "target_role", "years", "domain", "stack", "highlights"):
        if generated.get(field):
            merged[field] = generated[field]
    store.save(name, merged)
    return store.current()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="人设管理与面试回答 prompt 预览（密钥由 provider_config 管理）",
        epilog=f"临时覆盖人设：{ENV_PERSONA}=<name>")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="从模板生成本地 persona.json")
    sub.add_parser("list", help="列出所有人设")
    sub.add_parser("show", help="显示当前人设")
    p_use = sub.add_parser("use", help="切换当前人设")
    p_use.add_argument("name", nargs="?", help="人设名；省略则显示当前")
    p_prev = sub.add_parser("preview", help="预览将要发给 LLM 的 messages")
    p_prev.add_argument("question", help="面试官的问题原文")
    p_prev.add_argument("--lang", choices=["en", "zh"], default="en")
    p_prev.add_argument("--kb-k", type=int, default=4, help="注入的资料片段数")
    p_prev.add_argument("--no-kb", action="store_true", help="本次不检索资料库")
    sub.add_parser("kb", help="显示本地资料库状态")
    p_build = sub.add_parser("build", help="从 kb/ 资料生成人设（需先配好 LLM key）")
    p_build.add_argument("--name", default="kb-persona", help="生成的人设名")
    p_build.add_argument("--prompt", help="本次生成的人设要求；省略则用当前人设的手写提示词")
    args = parser.parse_args(argv)

    try:
        store = PersonaStore()
        if args.cmd == "init":
            print("created: " + str(PERSONA_PATH) if store.init()
                  else "already exists: " + str(PERSONA_PATH))
        elif args.cmd == "list":
            print(f"active: {store.active}" + ("  (使用示例模板)" if store.using_example else ""))
            for name, item in store.list().items():
                mark = "*" if name == store.active else " "
                print(f"  {mark} {name:<18} {item.get('label', '')}")
        elif args.cmd == "show":
            print(json.dumps(store.current(), ensure_ascii=False, indent=2))
        elif args.cmd == "use":
            if args.name:
                store.use(args.name)
                print(f"switched to: {args.name}")
            else:
                print(f"current persona: {store.active}")
        elif args.cmd == "preview":
            print(json.dumps(build_messages(
                args.question, source_lang=args.lang, store=store,
                kb_k=args.kb_k, use_kb=not args.no_kb),
                ensure_ascii=False, indent=2))
        elif args.cmd == "kb":
            print(json.dumps(knowledge_base.KnowledgeBase().stats(),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "build":
            from provider_config import ConfigError, ProviderConfig
            try:
                cfg = ProviderConfig()
                result = build_persona_from_kb(
                    cfg, store=store, name=args.name, user_prompt=args.prompt)
            except ConfigError as exc:
                print(f"LLM 配置错误: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (PersonaError, OSError) as exc:
        print(f"人设错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
