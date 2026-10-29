"""QQ 渠道的平台侧片段：入站事件转换、出站文本清洗与分段。

连接生命周期在 `qq_manager.py`，消息流程在 `inbound_pipeline.py`。
"""

import logging
from functools import partial
from itertools import count

from bot.provider import InboundMessage
from bot.replies import FAILURE_REPLY, TIMEOUT_REPLY
from bot.text_format import (
    split_bubbles_to_parts,
    strip_markdown,
)
from bot.voice import MAX_REPLY_BUBBLES

logger = logging.getLogger(__name__)

# QQ 被动回复窗口是 5 分钟：留一半给分段发送与平台延迟
QQ_REPLY_TIMEOUT = 150
QQ_TEXT_LIMIT = 800
# 不是额度，是失控兜底：与本地展示用同一个数，否则同一段话本地三条、QQ 四条。
QQ_MAX_PARTS = MAX_REPLY_BUBBLES
QQ_TIMEOUT_REPLY = TIMEOUT_REPLY
QQ_FAILURE_REPLY = FAILURE_REPLY


def strip_markdown_for_qq(text: str) -> str:
    """QQ 不渲染 Markdown：提示词是第一道闸，这里是发送前的兜底。"""
    return strip_markdown(text)


def split_qq_parts(
    text: str, limit: int = QQ_TEXT_LIMIT, max_parts: int = QQ_MAX_PARTS
) -> list[str]:
    """先按空行分「条」，每条再按平台单条上限切（规则见 text_format）。"""
    return split_bubbles_to_parts(text, limit=limit, max_parts=max_parts)


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
                # 序号按事件独立：QQ 用 (msg_id, msg_seq) 去重，同一条消息的多次回复
                # 必须各占一个序号，否则第 2 段起被 40054005「消息被去重」静默丢弃。
                send=partial(self._reply, message, count(1)),
            )
        )

    @staticmethod
    async def _reply(message, seq, part: str) -> None:
        await message.reply(content=part, msg_type=0, msg_seq=next(seq))
