"""微信渠道的平台侧片段：入站 XML 解析、出站 XML 组装与分段。

连接与 HTTP 调用在 `wechat_manager.py`，消息流程在 `inbound_pipeline.py`。
解密后的报文不是标准微信 XML 包法（见开放平台「接收客服消息」），字段：
userid / appid / content/msg / from / kfstate / channel / createtime。
"""

import hashlib
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from bot.replies import FAILURE_REPLY, TIMEOUT_REPLY
from bot.text_format import split_bubbles_to_parts, strip_markdown
from bot.voice import MAX_REPLY_BUBBLES

logger = logging.getLogger(__name__)

# 客服消息文本按 UTF-8 计约 2048 字节；600 个汉字留足余量。
WECHAT_TEXT_LIMIT = 600
# 不是额度，是失控兜底：与本地展示用同一个数，否则同一段话本地三条、微信四条。
WECHAT_MAX_PARTS = MAX_REPLY_BUBBLES
# 客服消息走 API，不存在 QQ 的 5 分钟被动回复窗口；生成预算与 QQ 对齐即可。
WECHAT_REPLY_TIMEOUT = 150
# 公网回调地址（隧道），图片表情包靠它给出平台能拉取的 url；空串 = 只发文字。
WEBHOOK_BASE_SETTING = "bot.webhook_base_url"
WECHAT_TIMEOUT_REPLY = TIMEOUT_REPLY
WECHAT_FAILURE_REPLY = FAILURE_REPLY

# 平台回调的通用确认报文：manager 主动发、callback 路由被动回，共用一份。
ACK = {"errcode": 0, "errmsg": "ok"}

# 解密后的报文由平台生成，但别把 DTD 交给解析器：实体膨胀没有任何业务理由。
_FORBIDDEN_DTD = ("<!doctype", "<!entity")


class WeChatPayloadError(Exception):
    """回调内容无法解析或与渠道不匹配。"""


@dataclass(frozen=True)
class WeChatInbound:
    userid: str
    appid: str
    msg: str
    channel: int
    kfstate: int
    createtime: str
    event: str
    from_: int


def _int_or(value: str, fallback: int) -> int:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return fallback


def parse_inbound(xml_text: str) -> WeChatInbound:
    lowered = (xml_text or "").lower()
    if any(marker in lowered for marker in _FORBIDDEN_DTD):
        raise WeChatPayloadError("回调报文包含 DTD，已拒绝")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise WeChatPayloadError("回调 XML 解析失败") from exc

    content = root.find("content")
    msg = (content.findtext("msg") if content is not None else "") or ""
    userid = (root.findtext("userid") or "").strip()
    if not userid:
        raise WeChatPayloadError("回调缺少 userid")
    return WeChatInbound(
        userid=userid,
        appid=(root.findtext("appid") or "").strip(),
        msg=msg,
        channel=_int_or(root.findtext("channel"), 0),
        kfstate=_int_or(root.findtext("kfstate"), 0),
        createtime=(root.findtext("createtime") or "").strip(),
        event=(root.findtext("event") or "").strip(),
        from_=_int_or(root.findtext("from"), 0),
    )


def extract_text(msg: str) -> str:
    """纯文本原样返回；富内容 JSON 里能取出文字就取，取不到（图片/语音）返回空。"""
    text = (msg or "").strip()
    if not text:
        return ""
    if text[0] not in "{[":
        return text
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(data, dict):
        for key in ("text", "content", "question", "msg"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _cdata(value: str) -> str:
    # "]]>" 会提前闭合 CDATA；拆成两段再续上，内容本身不变。
    return "<![CDATA[" + str(value).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_sendmsg_xml(*, appid: str, openid: str, text: str, channel: int) -> str:
    parts = ["<xml>"]
    if appid:
        parts.append(f"<appid>{_cdata(appid)}</appid>")
    parts.append(f"<openid>{_cdata(openid)}</openid>")
    parts.append(f"<msg>{_cdata(text)}</msg>")
    parts.append(f"<channel>{int(channel)}</channel>")
    parts.append("</xml>")
    return "".join(parts)


def build_kefustate_xml(*, appid: str, openid: str, state: str) -> str:
    return (
        "<xml>"
        f"<appid>{_cdata(appid)}</appid>"
        f"<openid>{_cdata(openid)}</openid>"
        f"<kefustate>{_cdata(state)}</kefustate>"
        "</xml>"
    )


def build_kefustate_get_xml(*, appid: str, openid: str) -> str:
    return (
        "<xml>"
        f"<appid>{_cdata(appid)}</appid>"
        f"<openid>{_cdata(openid)}</openid>"
        "</xml>"
    )


def inbound_event_id(userid: str, createtime: str, msg: str) -> str:
    """平台重投同一条消息时幂等；同一个人同一秒发两条相同内容会合并为一次回复。"""
    return hashlib.sha1(f"{userid}|{createtime}|{msg}".encode()).hexdigest()


def split_wechat_parts(
    text: str,
    limit: int = WECHAT_TEXT_LIMIT,
    max_parts: int = WECHAT_MAX_PARTS,
) -> list[str]:
    cleaned = strip_markdown(text).strip() or text
    return split_bubbles_to_parts(cleaned, limit=limit, max_parts=max_parts)
