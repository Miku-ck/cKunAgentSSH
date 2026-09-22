"""记忆 v2 文本切词：中文 2-gram + 英文词，供 FTS5 索引与查询复用。

不用 jieba 等外部分词依赖（零依赖原则）；2-gram 对 BM25 召回足够，
与旧 memory.py 的 Jaccard 切词同源。
"""
import re

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
_CJK_RE = re.compile(r"[一-鿿]")


def tokenize(text: str) -> list[str]:
    """切词并去重（保持首现顺序）。中文按 2-gram，英文按小写词。"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for w in _WORD_RE.findall(text.lower()):
        if w not in seen:
            seen.add(w)
            out.append(w)
    zh = _CJK_RE.findall(text)
    if len(zh) == 1:
        out.append(zh[0] * 2)
    for i in range(len(zh) - 1):
        gram = zh[i] + zh[i + 1]
        if gram not in seen:
            seen.add(gram)
            out.append(gram)
    return out


def match_query(query: str, max_tokens: int = 24) -> str:
    """构造 FTS5 MATCH 串（token OR 联合，token 仅含字母数字/CJK，无需转义）。"""
    toks = tokenize(query)[:max_tokens]
    return " OR ".join(toks)
