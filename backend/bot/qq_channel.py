"""QQ 渠道的平台侧片段：入站事件转换、出站文本清洗与分段。

连接生命周期在 `qq_manager.py`，消息流程在 `inbound_pipeline.py`。
"""

import asyncio
import base64
import logging
from functools import partial
from itertools import count
from types import SimpleNamespace

from bot import group_ambient
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

# 主动消息单次发送预算：含排队、鉴权刷新与平台响应，超时按 network 失败处理。
PROACTIVE_SEND_TIMEOUT_S = 15.0


def parse_conversation_key(conversation_key: str) -> "tuple[str, str] | None":
    """`group:{openid}` / `c2c:{openid}` → (分区, openid)；解析不了返回 None。

    主动发送没有入站消息可抱，只能靠会话键里的平台身份定位收件人。
    """
    partition, _, openid = (conversation_key or "").partition(":")
    if partition not in ("group", "c2c") or not openid:
        return None
    return partition, openid


# botpy 1.2.1 只按 HTTP 状态抛异常，业务码（400xxxxx）只出现在异常文案里，
# 所以主动消息的失败分类只能「异常类型 + 文案」双轨判定。
_PERMISSION_MARKERS = ("无权限", "无好友", "非群成员", "不是群成员", "拒收")
_QUOTA_MARKERS = ("频控", "频率", "频繁", "配额", "达上限", "超过")
_CONTENT_MARKERS = ("违规", "内容无效")


def classify_proactive_error(exc: BaseException) -> "tuple[str, str]":
    """主动消息失败的处置口径。

    permission 降级寄存（平台不让主动发），其余一律跳过等下一轮：频控会自己恢复，
    内容违规换句话才有效，网络错误当没发过比当「憋着的话」更干净。
    """
    text = str(exc or "").strip()
    if "召回" in text and "上限" in text:
        return "wakeup_limit", text
    if type(exc).__name__ == "ForbiddenError" or any(m in text for m in _PERMISSION_MARKERS):
        return "permission", text
    if any(m in text for m in _QUOTA_MARKERS):
        return "quota", text
    if any(m in text for m in _CONTENT_MARKERS):
        return "content", text
    return "network", text


def strip_markdown_for_qq(text: str) -> str:
    """QQ 不渲染 Markdown：提示词是第一道闸，这里是发送前的兜底。"""
    return strip_markdown(text)


def split_qq_parts(
    text: str, limit: int = QQ_TEXT_LIMIT, max_parts: int = QQ_MAX_PARTS
) -> list[str]:
    """先按空行分「条」，每条再按平台单条上限切（规则见 text_format）。"""
    return split_bubbles_to_parts(text, limit=limit, max_parts=max_parts)


class AmbientGroupMessage:
    """`GROUP_MESSAGE_CREATE`（全量模式）的轻量包装。

    botpy 1.2.1 的 GroupMessage 只透出 member_openid，而群聊临时上下文需要昵称
    （还要 author.bot 判自己发的回声）；其余字段与 `GROUP_AT_MESSAGE_CREATE`
    完全一致，所以只补需要的那几个。被动回复的凭据沿用 botpy 约定：拿事件信封
    的 id 当 msg_id（不是 `d.id`），平台按 (msg_id, msg_seq) 去重。
    """

    def __init__(self, api, payload: dict):
        data = payload.get("d") or {}
        author = data.get("author") or {}
        self._api = api
        self.id = payload.get("id") or data.get("id")
        self.event_id = self.id
        self.content = data.get("content") or ""
        self.group_openid = data.get("group_openid")
        self.author = SimpleNamespace(
            member_openid=author.get("member_openid"),
            username=author.get("username") or "",
            bot=bool(author.get("bot", False)),
        )

    async def reply(self, content=None, msg_type=0, msg_seq=None, media=None):
        return await self._api.post_group_message(
            group_openid=self.group_openid,
            msg_id=self.id,
            content=content,
            msg_type=msg_type,
            msg_seq=msg_seq,
            media=media,
        )


class QQInboundAdapter:
    """把 botpy 的消息对象转成平台中立的 InboundMessage。

    `conversation_key` 只装平台身份（群/单聊 + openid）；机器人归属由
    `bot_conversations.channel_id` 列承担，不再像旧版那样往 key 尾巴塞 context_id。
    """

    def __init__(self, channel_id: str, on_inbound):
        self.channel_id = channel_id
        self.on_inbound = on_inbound
        # 接话任务的强引用：事件循环只持弱引用，一个正在等模型的接话 task
        # 没人引用的话可能被 GC 掉，现场不报错、也不发回复（事后查不到）。
        self._ambient_tasks: "set[asyncio.Task]" = set()

    async def handle_group(self, message) -> None:
        # 被 @ 就是可参与期的起点：之后 5 分钟里群里不 @ 也能接话
        # （spec 2026-10-30-qq-group-ambient §1）。这一句回复本身就是一次开口，
        # 同时起节流：否则 @ 刚答完 3 秒的下一条消息又能立刻插一句，观感是抢话。
        group_ambient.note_mention(self.channel_id, message.group_openid)
        group_ambient.mark_spoken(self.channel_id, message.group_openid)
        await self._emit(message, f"group:{message.group_openid}")

    async def handle_group_message(self, message) -> None:
        """全量模式下的群消息：只记上下文，满足条件时接一句。

        生成会等模型几十秒，不能阻塞收包：整段丢给 task，异常只记日志。
        """
        content = (message.content or "").strip()
        if not content:
            return
        # 机器人自己的发言（主动消息/回复被回推）不进上下文也不触发接话：
        # 否则一个窗口内可能自说自话连好几句（自己发的文本不含 <@，守卫挡不住）。
        if getattr(getattr(message, "author", None), "bot", False):
            return
        if group_ambient.seen_event(self.channel_id, message.group_openid, message.id):
            return
        group_ambient.note_line(
            self.channel_id,
            message.group_openid,
            getattr(getattr(message, "author", None), "username", "") or "",
            content,
        )
        # @ 了任何人（含机器人）：机器人被 @ 那条由 AT 事件处理，这里再接就是双份；
        # @ 的是别人则说明这对话与它无关。
        if "<@" in content:
            return
        if not group_ambient.should_reply(self.channel_id, message.group_openid):
            return
        group_ambient.mark_spoken(self.channel_id, message.group_openid)
        key = f"group:{message.group_openid}"
        task = asyncio.create_task(self._emit_ambient(message, key))
        self._ambient_tasks.add(task)
        task.add_done_callback(self._ambient_tasks.discard)

    async def _emit_ambient(self, message, conversation_key: str) -> None:
        try:
            await self._emit(message, conversation_key, ambient=True)
        except Exception as exc:  # noqa: BLE001 —— 接话失败不能拖垮收包循环
            logger.warning("群聊接话失败: %s", exc, exc_info=True)

    async def handle_c2c(self, message) -> None:
        await self._emit(message, f"c2c:{message.author.user_openid}")

    async def _emit(self, message, conversation_key: str, *, ambient: bool = False) -> None:
        content = (message.content or "").strip()
        event_id = message.event_id or message.id
        if not content or not event_id:
            return
        # 「不 @」通道只在群会话挂：单聊本来就不 @ 人，挂上它等于白白
        # 把回复换到主动消息那条限频、要权限的通道上。
        plain_seq = count(1) if conversation_key.startswith("group:") else None
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
                send_plain=(
                    partial(self._reply_plain, message, plain_seq)
                    if plain_seq is not None
                    else None
                ),
                ambient=ambient,
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
    async def _upload_sticker(message, key: str) -> dict:
        """表情包上传拿 file_info：两条群发送通道共用这一段。

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
        return await message._api._http.request(route, json=payload)

    @staticmethod
    async def _reply_sticker(message, key: str, seq) -> None:
        """被动回复的富媒体：先上传拿 file_info，再用 msg_type=7 发出去。"""
        media = await QQInboundAdapter._upload_sticker(message, key)
        await message.reply(msg_type=7, media=media, msg_seq=next(seq))

    @classmethod
    async def _reply_plain(cls, message, seq, part: str) -> None:
        """群里不 @ 人那句：走不带 msg_id 的群消息接口。

        msg_id 一进请求体就是「回复某人」，那个 @ 是平台加的样式（接口层没有
        「不要 @」这个参数）：想不 @ 就只能换通道，把整句改走主动消息。
        代价是这句要群主开「主动推送消息能力」，被拒时按失败性质分流：
        权限/参数错 = 什么都没发出去，回落被动回复（宁可带 @ 也不让这句消失）；
        网络不确定 = 可能已经发过，再回落就是群里同样的两句，所以不回落。
        """
        key = sticker_mod.bubble_key(part)
        try:
            media = await cls._upload_sticker(message, key) if key else None
        except Exception as exc:  # noqa: BLE001 —— 上传没成功 = 什么都没发，回落不重复
            logger.warning("QQ 不 @ 通道素材上传失败，回落被动回复: %s", exc)
            await cls._reply(message, seq, part)
            return
        payload = (
            {"msg_type": 7, "media": media}
            if media is not None
            else {"msg_type": 0, "content": part}
        )
        try:
            await cls._post_group_plain(message, payload)
            return
        except Exception as exc:  # noqa: BLE001 —— 定性交给分类器，只有可确定的失败才回落
            kind, detail = classify_proactive_error(exc)
            if kind not in ("permission", "invalid"):
                raise
            logger.info("QQ 不 @ 通道被平台拒（%s）：%s，回落被动回复", kind, detail)
        await cls._reply(message, seq, part)

    @staticmethod
    async def _post_group_plain(message, payload: dict) -> None:
        """最小 payload 直调群消息接口：payload 里永远没有 msg_id。

        不用 SDK 的 post_group_message：1.2.1 的 `payload = locals()` 会把 msg_id
        等可选参数以 null 一并塞进 JSON，而「带 msg_id 的被动回复」正是这里要避免的。
        """
        from botpy.api import Route

        route = Route(
            "POST",
            "/v2/groups/{group_openid}/messages",
            group_openid=message.group_openid,
        )
        await message._api._http.request(route, json=payload)
