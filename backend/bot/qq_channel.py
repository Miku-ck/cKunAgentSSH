"""QQ 渠道的平台侧片段：入站事件转换、出站文本清洗与分段。

连接生命周期在 `qq_manager.py`，消息流程在 `inbound_pipeline.py`。
"""

import logging
import re
from functools import partial

from bot.provider import InboundMessage
from bot.voice import split_reply_bubbles

logger = logging.getLogger(__name__)

# QQ 被动回复窗口是 5 分钟：留一半给分段发送与平台延迟
QQ_REPLY_TIMEOUT = 150
QQ_TEXT_LIMIT = 800
QQ_MAX_PARTS = 4
QQ_TIMEOUT_REPLY = "这条我想得有点久，你稍后再跟我说一次好吗？"
QQ_FAILURE_REPLY = "我这边刚才没接上话，请稍后再试一次。"

_MARKDOWN_PATTERNS = (
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"\1"),
    (re.compile(r"__([^_\n]+)__"), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"(?m)^#{1,6}\s*"), ""),
)


def strip_markdown_for_qq(text: str) -> str:
    """QQ 不渲染 Markdown：提示词是第一道闸，这里是发送前的兜底。"""
    cleaned = text or ""
    for pattern, replacement in _MARKDOWN_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _split_point(text: str, limit: int) -> int:
    floor = limit // 2
    for separator in ("\n", "。", "！", "？", "，", ". "):
        cut = text.rfind(separator, 0, limit)
        if cut >= floor:
            return cut + len(separator)
    return limit


def split_qq_text(
    text: str, limit: int = QQ_TEXT_LIMIT, max_parts: int = QQ_MAX_PARTS
) -> list[str]:
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
        cut = _split_point(remaining, limit)
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts[-1] = parts[-1][: limit - 2].rstrip() + "……"
    return [part for part in parts if part]


def split_qq_parts(
    text: str, limit: int = QQ_TEXT_LIMIT, max_parts: int = QQ_MAX_PARTS
) -> list[str]:
    """先按空行分「条」，每条再按平台单条上限切。

    提示词里的空行就是「另发一条消息」，所以模型分开说的话在 QQ 上也该是一条一条
    连发 —— 真人就是这样。被动回复只有 max_parts 条额度：分条用完后余下的并回末条，
    宁可某条长一点也不吞字。
    """
    parts: list[str] = []
    for bubble in split_reply_bubbles(text, max_bubbles=max_parts):
        room = max_parts - len(parts)
        if room <= 0:
            break
        parts.extend(split_qq_text(bubble, limit=limit, max_parts=room))
    return parts


class QQInboundAdapter:
    """把 botpy 的消息对象转成平台中立的 InboundMessage。

    `conversation_key` 只装平台身份（群/单聊 + openid）；机器人归属由
    `bot_conversations.channel_id` 列承担，不再像旧版那样往 key 尾巴塞 context_id。
    """

    def __init__(self, channel_id: str, on_inbound):
        self.channel_id = channel_id
        self.on_inbound = on_inbound

    async def handle_group(self, message) -> None:
        await self._emit(message, f"group:{message.group_openid}")

    async def handle_c2c(self, message) -> None:
        await self._emit(message, f"c2c:{message.author.user_openid}")

    async def _emit(self, message, conversation_key: str) -> None:
        content = (message.content or "").strip()
        event_id = message.event_id or message.id
        if not content or not event_id:
            return
        await self.on_inbound(
            InboundMessage(
                channel_id=self.channel_id,
                conversation_key=conversation_key,
                event_id=event_id,
                text=content,
                # 回复能力闭包住这条消息：管线拿不到 message 对象本身。
                send=partial(self._reply, message),
            )
        )

    @staticmethod
    async def _reply(message, part: str) -> None:
        await message.reply(content=part, msg_type=0)
