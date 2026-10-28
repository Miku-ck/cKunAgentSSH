import asyncio
import logging
import re

import database as db

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


class QQEventProcessor:
    def __init__(self, chat_service, persona_id: str, context_id: str):
        self.chat_service = chat_service
        self.persona_id = persona_id
        self.context_id = context_id
        self._locks: dict[str, asyncio.Lock] = {}

    async def handle_group(self, message) -> None:
        await self._handle(message, f"group:{message.group_openid}:{self.context_id}")

    async def handle_c2c(self, message) -> None:
        await self._handle(message, f"c2c:{message.author.user_openid}:{self.context_id}")

    async def _handle(self, message, external_id: str) -> None:
        content = (message.content or "").strip()
        event_id = message.event_id or message.id
        if not content or not event_id:
            return
        lock = self._locks.setdefault(external_id, asyncio.Lock())
        async with lock:
            conversation = await asyncio.to_thread(
                db.get_or_create_bot_conversation,
                self.persona_id,
                "qq",
                external_id,
            )
            stored = await asyncio.to_thread(
                db.get_bot_message_by_external_id,
                conversation["id"],
                event_id,
            )
            # 幂等键是「已送达」而不是「已收到」：上次生成或发送失败的事件，
            # 平台重投时必须还能再答一次，否则用户永远等不到回复。
            if stored and stored.get("answered_at"):
                return
            # 首回合判定必须在 complete_reply 之前：complete_reply 会写入本轮用户/助手
            # 消息，之后再查历史就永远非空了。QQ 被动回复不可编辑、不可流式，
            # 只能把开场白前置拼进第一条回复。
            first_turn = not await asyncio.to_thread(
                db.get_recent_bot_messages, conversation["id"], 1
            )
            fallback = False
            try:
                reply = await asyncio.wait_for(
                    self.chat_service.complete_reply(
                        conversation["id"], content, external_msg_id=event_id, channel="qq"
                    ),
                    timeout=QQ_REPLY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("QQ 消息生成超时（%ss），改发兜底回复", QQ_REPLY_TIMEOUT)
                reply = QQ_TIMEOUT_REPLY
                fallback = True
            except Exception as exc:
                logger.warning("QQ 消息回复失败: %s", exc, exc_info=True)
                reply = QQ_FAILURE_REPLY
                fallback = True
            if fallback:
                await asyncio.to_thread(
                    db.add_bot_message, conversation["id"], "assistant", reply
                )
            else:
                greeting = (conversation.get("persona") or {}).get("greeting") or ""
                if first_turn and greeting:
                    reply = f"{greeting}\n\n{reply}"
            await self._send_reply(message, conversation["id"], event_id, reply)

    async def _send_reply(
        self, message, conversation_id: str, event_id: str, reply: str
    ) -> None:
        text = strip_markdown_for_qq(reply).strip() or reply
        for index, part in enumerate(split_qq_text(text)):
            try:
                await message.reply(content=part, msg_type=0)
            except Exception as exc:
                logger.warning(
                    "QQ 回复发送失败（第 %d 段）: %s", index + 1, exc, exc_info=True
                )
                return
            if index == 0:
                await asyncio.to_thread(
                    db.mark_bot_message_answered, conversation_id, event_id
                )
