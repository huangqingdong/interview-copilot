"""零依赖本地知识库：把用户提供的资料切块、建索引，按问题检索事实片段。

资料放 kb/ 目录（.md / .txt / .json / .csv / .docx，已被 .gitignore 排除）。
中文按 bigram、英文按词，避免引入分词器、embedding 和向量库。
文件新增或修改后自动重建索引，不用重启。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
KB_DIR = ROOT / "kb"
SUPPORTED = {".md", ".markdown", ".txt", ".json", ".csv", ".docx"}
MAX_CHARS = 420
K1, B = 1.5, 0.75

WORD = re.compile(r"[a-z0-9][a-z0-9+#._-]*")
HAN = re.compile(r"[\u4e00-\u9fff]+")
SPLIT_SENTENCE = re.compile(r"(?<=[。！？.!?；;])\s*")


def tokenize(text: str) -> list[str]:
    lowered = (text or "").lower()
    tokens = WORD.findall(lowered)
    for seg in HAN.findall(lowered):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


def _flatten_json(obj, out: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and not value.startswith("${"):
                out.append(f"{key}: {value}")
            else:
                _flatten_json(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _flatten_json(item, out)
    elif isinstance(obj, str):
        out.append(obj)


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(path: Path) -> str:
    """标准库解 .docx：取 word/document.xml 的段落文本，不依赖 python-docx。"""
    with zipfile.ZipFile(path) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    lines = []
    for para in root.iter(W_NS + "p"):
        pieces = []
        for node in para.iter():
            if node.tag == W_NS + "t":
                pieces.append(node.text or "")
            elif node.tag == W_NS + "tab":
                pieces.append("\t")
            elif node.tag in (W_NS + "br", W_NS + "cr"):
                pieces.append("\n")
        lines.append("".join(pieces))
    return "\n".join(lines)


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        try:
            return read_docx(path)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError):
            return ""
    if suffix == ".json":
        try:
            lines: list[str] = []
            _flatten_json(json.loads(path.read_text(encoding="utf-8")), lines)
            return "\n".join(lines)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    pieces, current = [], ""
    for sentence in SPLIT_SENTENCE.split(text):
        if len(current) + len(sentence) > MAX_CHARS and current:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)
    out = []
    for piece in pieces:
        if len(piece) <= MAX_CHARS:
            out.append(piece)
        else:
            out.extend(piece[i:i + MAX_CHARS]
                       for i in range(0, len(piece), MAX_CHARS))
    return out


def split_chunks(text: str, source: str) -> list[dict]:
    chunks: list[dict] = []
    heading, buffer = "", []

    def flush():
        content = " ".join(x.strip() for x in buffer if x.strip())
        buffer.clear()
        if not content:
            return
        for part in _split_long(content):
            chunks.append({"text": part, "source": source, "heading": heading})

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            heading = stripped.lstrip("#").strip()
        elif not stripped:
            flush()
        else:
            buffer.append(stripped)
    flush()
    return chunks


class KnowledgeBase:
    def __init__(self, root=KB_DIR):
        self.root = Path(root)
        self.chunks: list[dict] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.lengths: list[int] = []
        self.avg_len = 0.0
        self._stamp: tuple | None = None
        self.refresh(force=True)

    def _scan_stamp(self):
        if not self.root.exists():
            return ()
        files = sorted(p for p in self.root.rglob("*")
                       if p.is_file() and p.suffix.lower() in SUPPORTED)
        return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                     for p in files)

    def refresh(self, force=False):
        stamp = self._scan_stamp()
        if not force and stamp == self._stamp:
            return False
        self._stamp = stamp
        self._build(stamp)
        return True

    def _build(self, stamp):
        self.chunks = []
        for entry in stamp:
            path = Path(entry[0])
            source = path.name
            for chunk in split_chunks(read_document(path), source):
                self.chunks.append(chunk)
        self.postings = defaultdict(list)
        self.lengths = []
        for idx, chunk in enumerate(self.chunks):
            tokens = tokenize(chunk["text"])
            self.lengths.append(len(tokens) or 1)
            for token, tf in _term_freq(tokens).items():
                self.postings[token].append((idx, tf))
        self.postings = dict(self.postings)
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0

    def stats(self):
        self.refresh()
        return {"files": len(self._stamp or ()), "chunks": len(self.chunks),
                "root": str(self.root)}

    def search(self, query: str, k: int = 4, min_score: float = 0.6):
        """BM25-lite；低于 min_score 的片段不返回，避免噪声误导回答。"""
        self.refresh()
        query_tokens = set(tokenize(query))
        total = len(self.chunks)
        if not total or not query_tokens:
            return []
        scores: dict[int, float] = defaultdict(float)
        for token in query_tokens:
            postings = self.postings.get(token)
            if not postings:
                continue
            df = len(postings)
            # 资料只有一两份时 df≈total，idf 会趋近 0，导致刚拖入第一份简历
            # 时永远检索不到。给下限，弱相关仍由 min_score 过滤。
            idf = max(math.log(1 + (total - df + 0.5) / (df + 0.5)), 0.4)
            for idx, tf in postings:
                norm = 1 - B + B * self.lengths[idx] / self.avg_len
                scores[idx] += idf * (tf * (K1 + 1)) / (tf + K1 * norm)
        ranked = sorted(scores.items(), key=lambda item: -item[1])
        return [dict(self.chunks[idx], score=round(score, 3))
                for idx, score in ranked[:k] if score >= min_score]


def _term_freq(tokens):
    counts = defaultdict(int)
    for token in tokens:
        counts[token] += 1
    return counts


def render_context(hits: list[dict]) -> str:
    """把命中片段渲染进 system prompt；空结果返回空串。"""
    if not hits:
        return ""
    lines = ["【资料依据（本次检索命中，唯一可引用的事实来源）】"]
    for i, hit in enumerate(hits, 1):
        title = hit["source"] + (f" / {hit['heading']}" if hit.get("heading") else "")
        lines.append(f"{i}. [{title}]\n{hit['text']}")
    lines.append("资料与画像冲突时以资料为准；不要提及文件名、编号或「根据资料」这类措辞。")
    return "\n".join(lines) + "\n\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地资料库检索自检")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help="文件数与切块数")
    p_search = sub.add_parser("search", help="按问题检索命中片段")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=4)
    args = parser.parse_args(argv)
    kb = KnowledgeBase()
    if args.cmd == "stats":
        print(json.dumps(kb.stats(), ensure_ascii=False, indent=2))
    else:
        hits = kb.search(args.query, k=args.k)
        print(f"hits: {len(hits)}")
        for hit in hits:
            print(f"\n[{hit['source']} / {hit.get('heading', '')}] score={hit['score']}")
            print(hit["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
