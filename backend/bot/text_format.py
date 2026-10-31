"""平台无关的出站文本处理：Markdown 兜底、按句读切长文、按空行分气泡。

QQ 与微信都不渲染 Markdown，也有各自的单条长度上限；差异只在参数（limit），
切分规则本身共用一份，免得两个渠道的断句行为慢慢跑偏。
"""

import re

from bot.voice import MAX_REPLY_BUBBLES, split_reply_bubbles

_MARKDOWN_PATTERNS = (
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"\1"),
    (re.compile(r"__([^_\n]+)__"), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"(?m)^#{1,6}\s*"), ""),
)


def strip_markdown(text: str) -> str:
    """提示词是第一道闸，这里是发送前的兜底。"""
    cleaned = text or ""
    for pattern, replacement in _MARKDOWN_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


# QQ 入站原文里的 @ 标记长这样：`<@!1234>`（带昵称）/ `<at id="..."></at>`（纯文本）。
# 模型把它们当正文照抄进回复时，QQ 的纯文本消息会把尖括号原样显示。
_AT_MARKUP = re.compile(r"<@!?[^>]*>|<at\b[^>]*>(?:[ \t]*</at>)?")


def strip_at_markup(text: str) -> str:
    """剔掉照抄进来的 @ 标记：出站没有「在纯文本里 @ 某人」这个语法。"""
    cleaned = _AT_MARKUP.sub("", text or "")
    # 标记夹在句中时会留下连续空格，挤一下再去首尾空白，不把缝隙留在正文里。
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def split_point(text: str, limit: int) -> int:
    floor = limit // 2
    for separator in ("\n", "。", "！", "？", "，", ". "):
        cut = text.rfind(separator, 0, limit)
        if cut >= floor:
            return cut + len(separator)
    return limit


def split_text(text: str, *, limit: int, max_parts: int) -> list[str]:
    """按段落/句读切成多段，每段不超过平台单条上限；超出段数时截断。"""
    remaining = (text or "").strip()
    if not remaining:
        return []
    parts: list[str] = []
    while remaining and len(parts) < max_parts:
        if len(remaining) <= limit:
            parts.append(remaining)
            remaining = ""
            break
        cut = split_point(remaining, limit)
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts[-1] = parts[-1][: limit - 2].rstrip() + "……"
    return [part for part in parts if part]


def split_bubbles_to_parts(
    text: str,
    *,
    limit: int,
    max_parts: int = MAX_REPLY_BUBBLES,
) -> list[str]:
    """先按空行分「条」，每条再按平台单条上限切。

    提示词里的空行就是「另发一条消息」，所以模型分开说的话在平台上也该一条一条
    连发 —— 真人就是这样。被动回复只有 max_parts 条额度：分条用完后余下的并回末条，
    宁可某条长一点也不吞字。
    """
    parts: list[str] = []
    for bubble in split_reply_bubbles(text, max_bubbles=max_parts):
        room = max_parts - len(parts)
        if room <= 0:
            break
        parts.extend(split_text(bubble, limit=limit, max_parts=room))
    return parts
