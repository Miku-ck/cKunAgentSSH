"""平台中立的入站管线：会话归属、幂等、生成预算、分段投递。

逻辑从 `qq_channel.QQEventProcessor._handle` 原样搬出，只把 QQ 常量换成
provider 提供的三样东西（reply_window_s / outbound_parts / fallback）。
"""

import asyncio
import logging

import database as db
from bot.pace import outbound_gaps

logger = logging.getLogger(__name__)


class InboundPipeline:
    def __init__(self, chat_service, *, sleep=asyncio.sleep):
        self.chat_service = chat_service
        # 段间等待走注入：测试里换成记录器，生产里就是 asyncio.sleep。
        self._sleep = sleep
        # 锁按 (机器人, 平台内会话) 分：多个机器人同时收到同一个 openid 的消息
        # 不应互相排队，也不该并发写同一条会话。
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def handle(self, inbound, provider, persona_id: str) -> None:
        key = (inbound.channel_id, inbound.conversation_key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._handle_locked(inbound, provider, persona_id)

    async def _handle_locked(self, inbound, provider, persona_id: str) -> None:
        conversation = await asyncio.to_thread(
            db.get_or_create_bot_conversation,
            persona_id,
            provider.name,
            inbound.conversation_key,
            inbound.channel_id,
        )
        stored = await asyncio.to_thread(
            db.get_bot_message_by_external_id, conversation["id"], inbound.event_id
        )
        # 幂等键是「已送达」而不是「已收到」：上次生成或发送失败的事件，
        # 平台重投时必须还能再答一次，否则用户永远等不到回复。
        if stored and stored.get("answered_at"):
            return
        # 首回合判定必须在生成之前：complete_reply 会写入本轮用户/助手消息，
        # 之后再查历史就永远非空了。被动回复不可编辑、不可流式，
        # 只能把开场白前置拼进第一条回复。
        first_turn = not await asyncio.to_thread(
            db.get_recent_bot_messages, conversation["id"], 1
        )
        fallback = False
        try:
            reply = await asyncio.wait_for(
                self.chat_service.complete_reply(
                    conversation["id"],
                    inbound.text,
                    external_msg_id=inbound.event_id,
                    channel=provider.name,
                ),
                timeout=provider.reply_window_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "%s 消息生成超时（%ss），改发兜底回复", provider.name, provider.reply_window_s
            )
            reply = provider.fallback("timeout")
            fallback = True
        except Exception as exc:
            logger.warning("%s 消息回复失败: %s", provider.name, exc, exc_info=True)
            reply = provider.fallback("failure")
            fallback = True
        if fallback:
            await asyncio.to_thread(db.add_bot_message, conversation["id"], "assistant", reply)
        else:
            greeting = (conversation.get("persona") or {}).get("greeting") or ""
            if first_turn and greeting:
                reply = f"{greeting}\n\n{reply}"
            # P3 意愿寄存：过闸但协议上发不出的「心里话」（QQ 150s 被动窗口），
            # 见到对方来消息时以「对了，」捎上 —— 协议仍是被动回复，行为上
            # 等价于「她憋了一句话，见到你终于说了」。
            try:
                from bot.proactive import take_pending_for_reply

                pending_text = await asyncio.to_thread(
                    take_pending_for_reply, conversation["id"]
                )
            except Exception as exc:  # noqa: BLE001 —— 寄存失败不影响正常回复
                logger.warning("主动性寄存读取失败: %s", exc)
                pending_text = None
            if pending_text:
                reply = f"对了，{pending_text}\n\n{reply}"
        await self._deliver(inbound, provider, conversation["id"], reply)

    async def _deliver(self, inbound, provider, conversation_id: str, reply: str) -> None:
        parts = provider.outbound_parts(reply)
        gaps = outbound_gaps(parts)
        for index, part in enumerate(parts):
            # 首段零延迟，之后按「像在打字」的间隔发：同一秒弹出三条是广播，
            # 不是人在说话。段数不变，所以本地记录与平台收到的条数始终一致。
            if gaps[index]:
                await self._sleep(gaps[index])
            try:
                await inbound.send(part)
            except Exception as exc:
                # 发送失败就不标已答：平台重投同一条事件时还要能再答一次。
                logger.warning(
                    "%s 回复发送失败（第 %d 段）: %s", provider.name, index + 1, exc, exc_info=True
                )
                return
            if index == 0:
                await asyncio.to_thread(
                    db.mark_bot_message_answered, conversation_id, inbound.event_id
                )
