import asyncio
import logging

import database as db

logger = logging.getLogger(__name__)


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
            duplicate = await asyncio.to_thread(
                db.get_bot_message_by_external_id,
                conversation["id"],
                event_id,
            )
            if duplicate:
                return
            try:
                reply = await self.chat_service.complete_reply(
                    conversation["id"], content, external_msg_id=event_id
                )
                await message.reply(content=reply, msg_type=0)
            except Exception as exc:
                logger.warning("QQ 消息回复失败: %s", exc, exc_info=True)
                await message.reply(content="我这边刚才没接上话，请稍后再试一次。", msg_type=0)
