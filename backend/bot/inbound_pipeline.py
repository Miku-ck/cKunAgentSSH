"""平台中立的入站管线：会话归属、幂等、生成预算、分段投递。

逻辑从 `qq_channel.QQEventProcessor._handle` 原样搬出，只把 QQ 常量换成
provider 提供的三样东西（reply_window_s / outbound_parts / fallback）。
"""

import asyncio
import logging

import database as db
from bot import group_ambient
from bot.pace import outbound_gaps

logger = logging.getLogger(__name__)

# 接话自己的生成预算，不用 provider.reply_window_s（QQ 是 150s）：接话可有可无，
# @ 必须答 —— 一句接话在会话锁里等满 150s，会把随后那条 @ 的回复挤出 5 分钟被动窗口。
AMBIENT_REPLY_BUDGET_S = 45.0


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
        # 群聊环境消息走单独的窄路：不幂等、不写用户消息、不记关系
        # （spec 2026-10-30-qq-group-ambient §2）。
        if getattr(inbound, "ambient", False):
            if lock.locked():
                # 这条会话正在答真消息：不插队也不排队，这句接话直接作废。
                return
            if not await self._may_ambient(persona_id):
                return
            async with lock:
                await self._handle_ambient(inbound, provider, persona_id)
            return
        async with lock:
            await self._handle_locked(inbound, provider, persona_id)

    async def _may_ambient(self, persona_id: str) -> bool:
        """群参与档没开到「可接话」就不说话。

        闸放在管线而不是渠道适配层：接不接话是人格的属性，而适配层只认识机器人。
        人格读不到（已删/脏数据）按最保守档处理：宁可少说一句。
        """
        persona = await asyncio.to_thread(db.get_bot_persona, persona_id)
        return group_ambient.ambient_allowed((persona or {}).get("group_mode"))

    async def _handle_ambient(self, inbound, provider, persona_id: str) -> None:
        """只接一句话：拿到近期群消息当临时上下文，回一句就结束。"""
        conversation = await asyncio.to_thread(
            db.get_or_create_bot_conversation,
            persona_id,
            provider.name,
            inbound.conversation_key,
            inbound.channel_id,
        )
        group_openid = (inbound.conversation_key or "").removeprefix("group:")
        context = group_ambient.context_lines(inbound.channel_id, group_openid)
        budget = min(provider.reply_window_s, AMBIENT_REPLY_BUDGET_S)
        try:
            reply = await asyncio.wait_for(
                self.chat_service.ambient_reply(
                    conversation["id"], context, channel=provider.name
                ),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            # 环境接话本就可有可无：超时直接放弃，不拿兜底文案往群里刷。
            logger.warning("%s 群聊接话生成超时（%ss），本轮放弃", provider.name, budget)
            return
        except Exception as exc:  # noqa: BLE001 —— 接不上就不说话，群里不需要报错
            logger.warning("%s 群聊接话失败: %s", provider.name, exc, exc_info=True)
            return
        spoken = await self._deliver(
            inbound, provider, conversation, reply, mark_answered=False
        )
        # 自己的发言也进上下文：下一句接话才知道刚才说过什么，避免复读。
        if spoken:
            persona = (conversation.get("persona") or {}).get("name") or "我"
            group_ambient.note_line(inbound.channel_id, group_openid, persona, reply)

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
            # 意愿寄存：过了意愿闸但这条发不出去的「心里话」（无主动推送权限
            # /离线/已不在群里），见到对方来消息时以「对了，」捎上 ——
            # 协议仍是被动回复，行为上等价于「她憋了一句话，见到你终于说了」。
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
        await self._deliver(inbound, provider, conversation, reply)

    @staticmethod
    def _send_lane(inbound, conversation: dict):
        """这句回复走哪条发送通道：人格关掉「@ 提问者」就走不带 msg_id 的主动消息。

        读会话快照而不是再查一次人格：入站热路径不该多一次查询，而快照在人格
        编辑时就会被回填（revise_persona_snapshots）。旧快照没这个字段就按
        默认「照旧 @」处理：宁可多一个 @，也不冒群主没开主动推送权限的风险。
        没挂 send_plain 的渠道（本地/微信）也不换：那边本来就没有 @ 这回事。
        """
        plain = getattr(inbound, "send_plain", None)
        if plain is None:
            return inbound.send
        raw = (conversation.get("persona") or {}).get("mention_sender")
        mention = 1 if raw is None else int(raw or 0)
        return inbound.send if mention else plain

    async def _deliver(
        self,
        inbound,
        provider,
        conversation: dict,
        reply: str,
        *,
        mark_answered: bool = True,
    ) -> bool:
        """逐段投递，返回是否全部发出。

        mark_answered=False 用于群聊接话：那条事件本来就没有落库记录，
        也没有人要靠「已答」做幂等。
        """
        conversation_id = conversation["id"]
        send = self._send_lane(inbound, conversation)
        parts = provider.outbound_parts(reply)
        gaps = outbound_gaps(parts)
        for index, part in enumerate(parts):
            # 首段零延迟，之后按「像在打字」的间隔发：同一秒弹出三条是广播，
            # 不是人在说话。段数不变，所以本地记录与平台收到的条数始终一致。
            if gaps[index]:
                await self._sleep(gaps[index])
            try:
                await send(part)
            except Exception as exc:
                # 发送失败就不标已答：平台重投同一条事件时还要能再答一次。
                logger.warning(
                    "%s 回复发送失败（第 %d 段）: %s", provider.name, index + 1, exc, exc_info=True
                )
                return False
            if index == 0 and mark_answered:
                await asyncio.to_thread(
                    db.mark_bot_message_answered, conversation_id, inbound.event_id
                )
        return True
