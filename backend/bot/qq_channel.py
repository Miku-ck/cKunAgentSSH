"""QQ 渠道的平台侧片段：入站事件转换、出站文本清洗与分段。

连接生命周期在 `qq_manager.py`，消息流程在 `inbound_pipeline.py`。
"""

import base64
import logging
from functools import partial
from itertools import count

from bot import stickers as sticker_mod
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
        key = sticker_mod.bubble_key(part)
        if key:
            try:
                await QQInboundAdapter._reply_sticker(message, key, seq)
                return
            except Exception as exc:  # noqa: BLE001 —— 发图失败不能让这一句消失
                logger.warning("QQ 表情包发送失败，回落颜文字: %s", exc)
                await message.reply(
                    content=sticker_mod.kaomoji_for(key) or "……",
                    msg_type=0,
                    msg_seq=next(seq),
                )
                return
        await message.reply(content=part, msg_type=0, msg_seq=next(seq))

    @staticmethod
    async def _reply_sticker(message, key: str, seq) -> None:
        """QQ 富媒体：先上传拿 file_info，再用 msg_type=7 发出去。

        botpy 1.2.1 的 post_group_file 只透传 url，本机没有公网图床，
        所以直接走 SDK 的 HTTP 通道带 file_data（base64），鉴权头由 SDK 补。
        """
        path = sticker_mod.path_for(key)
        if not path:
            raise FileNotFoundError(f"表情包素材缺失：{key}")
        from botpy.api import Route

        payload = {
            "file_type": 1,
            "file_data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "srv_send_msg": False,
        }
        if getattr(message, "group_openid", None):
            route = Route(
                "POST",
                "/v2/groups/{group_openid}/files",
                group_openid=message.group_openid,
            )
        else:
            route = Route(
                "POST",
                "/v2/users/{openid}/files",
                openid=message.author.user_openid,
            )
        media = await message._api._http.request(route, json=payload)
        await message.reply(msg_type=7, media=media, msg_seq=next(seq))
