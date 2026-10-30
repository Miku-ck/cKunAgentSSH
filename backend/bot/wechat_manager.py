"""微信机器人的回调式连接（spec 2026-10-29-wechat-channel）。

微信对话开放平台是推送模型：没有长连接，消息由 `/bot/wechat/{channel_id}`
转交给 `accept()`。`start()` 只校验凭据并登记一个「已就绪」句柄，真正的
连接状态其实是「回调地址能不能打进来」。
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from urllib.parse import quote

import database as db
import netconfig

from bot import stickers as sticker_mod
from bot import wechat_channel as wx
from bot.provider import (
    ChannelConfig,
    ChannelConnectError,
    InboundMessage,
)
from bot.wechat_crypto import WeChatCrypto, WeChatCryptoError

logger = logging.getLogger(__name__)

SENDMSG_URL = "https://chatbot.weixin.qq.com/openapi/sendmsg/{token}"
KEFUSTATE_CHANGE_URL = "https://chatbot.weixin.qq.com/openapi/kefustate/change/{token}"
KEFUSTATE_GET_URL = "https://chatbot.weixin.qq.com/openapi/kefustate/get/{token}"
# 接管后平台机器人不再回答该用户；每半小时补一次，防止后台被人工改成 complete。
SERVING_TTL_S = 1800
HTTP_TIMEOUT_S = 10
# 断开/删除渠道时把最近会话里的用户还给平台机器人。
MAX_RELEASE_USERS = 20
RELEASE_BUDGET_S = 5


def _mask(openid: str) -> str:
    return f"{openid[:6]}…" if len(openid) > 6 else openid


def _received_at_text() -> str:
    """状态行的「最近收到消息」时间：本地时区，精确到分钟就够。"""
    return datetime.now().astimezone().strftime("%m-%d %H:%M")


class WeChatConnection:
    def __init__(self, cfg: ChannelConfig, on_inbound, *, client=None):
        self.cfg = cfg
        self.on_inbound = on_inbound
        self.crypto: WeChatCrypto | None = None
        self._client = client
        # 注入的客户端归测试/调用方管，stop 不去关它。
        self._owns_client = client is None
        self._status = {"state": "connecting", "detail": ""}
        self._tasks: set[asyncio.Task] = set()
        self._serving: dict[str, float] = {}
        self._serving_inflight: set[str] = set()
        # 每个用户所属的公众号/小程序 appid：sendmsg 与 kefustate 都只认它，
        # 开放平台 APPID（cfg.app_id）传进去会被判 appid is not valid。
        self._appids: dict[str, str] = {}

    @property
    def status(self) -> dict:
        return dict(self._status)

    def _set_status(self, state: str, detail: str = "") -> None:
        self._status = {"state": state, "detail": detail}

    async def start(self) -> None:
        """本地自检：密钥可解、往返一致、Token 非空。不做网络握手。"""
        if not (self.cfg.app_secret or "").strip():
            raise ChannelConnectError("Token 不能为空，请到开放API 页重新复制", fatal=True)
        try:
            self.crypto = WeChatCrypto(self.cfg.app_id, self.cfg.extra_secret)
            probe = self.crypto.encrypt("ping")
            if self.crypto.decrypt(probe) != "ping":
                raise WeChatCryptoError("加解密自检不一致")
        except WeChatCryptoError as exc:
            raise ChannelConnectError(str(exc), fatal=True) from exc
        if self._client is None:
            self._client = netconfig.new_client(default_timeout=HTTP_TIMEOUT_S)
        self._set_status("online", "已就绪，等待微信回调")

    async def accept(self, body: dict) -> dict:
        """处理一次平台回调。解密失败/内容不对抛 WeChatPayloadError（路由转 400）。"""
        if self.crypto is None:
            raise wx.WeChatPayloadError("渠道尚未就绪")
        encrypted = body.get("encrypted") if isinstance(body, dict) else None
        if not isinstance(encrypted, str) or not encrypted:
            raise wx.WeChatPayloadError("缺少 encrypted 字段")
        try:
            xml_text = self.crypto.decrypt(encrypted)
        except WeChatCryptoError as exc:
            raise wx.WeChatPayloadError(str(exc)) from exc
        inbound = wx.parse_inbound(xml_text)

        # 平台推来的机器人/人工客服发言、进入离开事件，都不该触发新一轮生成。
        if inbound.from_ != 0 or inbound.event:
            return dict(wx.ACK)
        text = wx.extract_text(inbound.msg)
        if not text:
            return dict(wx.ACK)
        # 入站 XML 的 appid 是公众号/小程序 appid，不是开放平台 APPID：
        # 渠道归属性已由 AES 密钥 + 明文尾部 appid 校验，这里不再比对 cfg.app_id。
        self._appids[inbound.userid] = inbound.appid

        message = InboundMessage(
            channel_id=self.cfg.channel_id,
            conversation_key=f"wx:{inbound.userid}",
            event_id=wx.inbound_event_id(inbound.userid, inbound.createtime, inbound.msg),
            text=text,
            send=self._sender(inbound),
        )
        # 真收到用户消息才从「等待回调」翻成「已接通」：机器人/事件回调不算，
        # 否则状态会在还没打通时就谎报接通。
        self._set_status("online", f"已接通 · 最近收到消息 {_received_at_text()}")
        # 先接管再生成：接管是 HTTP，生成更慢；两者并行，用户无需等待接管结果。
        self._spawn(self._take_over(inbound.userid, inbound.appid))
        self._spawn(self.on_inbound(message))
        return dict(wx.ACK)

    def _sender(self, inbound: wx.WeChatInbound):
        async def send(part: str) -> None:
            key = sticker_mod.bubble_key(part)
            if key:
                await self._send_sticker(inbound, key)
                return
            await self._sendmsg(inbound, part)

        return send

    async def _sendmsg(self, inbound: wx.WeChatInbound, text: str) -> None:
        # 渠道 7（H5）不传 appid；其余渠道传入站里的公众号/小程序 appid。
        # 绝不能回落 cfg.app_id —— 那是开放平台 APPID，sendmsg 直接回 1011 appid is not valid。
        appid = "" if inbound.channel == 7 else (inbound.appid or "")
        xml_text = wx.build_sendmsg_xml(
            appid=appid,
            openid=inbound.userid,
            text=text,
            channel=inbound.channel,
        )
        await self._post(self._api_url(SENDMSG_URL), self.crypto.encrypt(xml_text))

    async def _send_sticker(self, inbound: wx.WeChatInbound, key: str) -> None:
        """图片消息走 `<msg>` 里的 JSON；平台要能拉到图，所以只在配了公网地址时发。

        小程序渠道只认 media_id、本机又没有公众号素材上传凭据，发送失败就回落
        颜文字 —— 宁可少一张图，也不能让这一条回复直接消失。
        """
        base = (await asyncio.to_thread(
            db.get_app_setting, wx.WEBHOOK_BASE_SETTING, ""
        ) or "").strip().rstrip("/")
        if base:
            url = f"{base}/assets/stickers/{quote(key, safe='')}"
            rich = json.dumps({"image": {"url": url}}, ensure_ascii=False)
            try:
                await self._sendmsg(inbound, rich)
                return
            except Exception as exc:  # noqa: BLE001 —— 平台不认 url 是正常分支
                logger.info(
                    "微信渠道 %s 图片表情包发送失败，回落颜文字: %s",
                    self.cfg.channel_id[:8],
                    exc,
                )
        await self._sendmsg(inbound, sticker_mod.kaomoji_for(key) or "……")

    async def _change_kefustate(self, openid: str, state: str, appid: str) -> None:
        # 平台文档：这里的 appid 是公众号或小程序 appid。传开放平台 APPID 会返回
        # errcode 0 但状态不动，用户侧表现为「消息发得出、微信里看不到」。
        xml_text = wx.build_kefustate_xml(appid=appid, openid=openid, state=state)
        await self._post(self._api_url(KEFUSTATE_CHANGE_URL), self.crypto.encrypt(xml_text))

    async def _get_kefustate(self, openid: str, appid: str) -> dict:
        xml_text = wx.build_kefustate_get_xml(appid=appid, openid=openid)
        return await self._post(self._api_url(KEFUSTATE_GET_URL), self.crypto.encrypt(xml_text))

    def _api_url(self, template: str) -> str:
        return template.format(token=quote(self.cfg.app_secret.strip(), safe=""))

    async def _post(self, url: str, encrypted: str) -> dict:
        response = await self._client.post(
            url,
            json={"encrypt": encrypted},
            headers={"content-type": "application/json"},
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("微信接口返回了非 JSON 响应") from exc
        errcode = int(data.get("errcode", -1))
        if errcode != 0:
            detail = netconfig.redact_urls_in_text(str(data.get("errmsg") or ""))
            raise RuntimeError(f"微信接口失败 errcode={errcode} {detail}".strip())
        return data

    async def _take_over(self, openid: str, appid: str) -> None:
        if self.crypto is None or not appid:
            return
        now = time.monotonic()
        if now - self._serving.get(openid, 0.0) < SERVING_TTL_S:
            return
        if openid in self._serving_inflight:
            return
        self._serving_inflight.add(openid)
        try:
            await self._change_kefustate(openid, "personserving", appid)
            self._serving[openid] = time.monotonic()
            logger.info("微信渠道 %s 接管用户 %s", self.cfg.channel_id[:8], _mask(openid))
            # change 对 appid 不做校验（假的也回 errcode 0、状态原地不动），
            # 只信回读：读到 off 说明平台机器人还在应答，这条日志就是现场。
            state = await self._get_kefustate(openid, appid)
            if str(state.get("kefustate") or "").lower() != "on":
                logger.warning(
                    "微信渠道 %s 接管用户 %s 未生效（kefustate=%s status=%s）："
                    "平台机器人仍在应答，请确认公众号已认证并授权给开放平台",
                    self.cfg.channel_id[:8],
                    _mask(openid),
                    state.get("kefustate"),
                    state.get("status"),
                )
        except Exception as exc:  # noqa: BLE001 —— 接管失败也要继续回复
            logger.warning(
                "微信渠道 %s 接管用户 %s 失败: %s",
                self.cfg.channel_id[:8],
                _mask(openid),
                exc,
            )
        finally:
            self._serving_inflight.discard(openid)

    async def release(self) -> None:
        """显式断开/删除渠道时，把最近会话里的用户还给平台机器人。"""
        if self.crypto is None or self._client is None:
            return
        try:
            rows = await asyncio.to_thread(
                db.list_bot_conversations,
                channel_id=self.cfg.channel_id,
                limit=MAX_RELEASE_USERS,
            )
        except Exception as exc:  # noqa: BLE001 —— 释放是尽力而为
            logger.warning("微信渠道 %s 读取最近会话失败: %s", self.cfg.channel_id[:8], exc)
            return

        openids: list[str] = []
        for row in rows:
            key = str(row.get("external_id") or "")
            if key.startswith("wx:"):
                openid = key[3:]
                if openid and openid not in openids:
                    openids.append(openid)
        if not openids:
            return

        async def release_one(openid: str) -> None:
            appid = self._appids.get(openid, "")
            if not appid:
                return
            try:
                await self._change_kefustate(openid, "complete", appid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "微信渠道 %s 释放用户 %s 失败: %s",
                    self.cfg.channel_id[:8],
                    _mask(openid),
                    exc,
                )

        try:
            await asyncio.wait_for(
                asyncio.gather(*(release_one(openid) for openid in openids)),
                RELEASE_BUDGET_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "微信渠道 %s 释放用户超时（已尝试 %d 个）",
                self.cfg.channel_id[:8],
                len(openids),
            )

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning(
                "微信渠道 %s 后台任务失败: %s", self.cfg.channel_id[:8], exc, exc_info=exc
            )

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None if self._owns_client else self._client
        self.crypto = None
        self._set_status("offline")


class WeChatProvider:
    """微信渠道的 provider 实现（契约见 bot/provider.py）。"""

    name = "wechat"
    reply_window_s = wx.WECHAT_REPLY_TIMEOUT
    credential_labels = {"app_secret": "Token", "extra_secret": "EncodingAESKey"}

    def __init__(self, client_factory=None):
        # client_factory 只给测试注入 httpx 替身；生产为 None，连接自己建客户端。
        self.client_factory = client_factory

    async def start(
        self,
        cfg: ChannelConfig,
        on_inbound,
        *,
        wait_ready: bool = True,
        ready_timeout: float = 20.0,
    ) -> WeChatConnection:
        connection = WeChatConnection(
            cfg,
            on_inbound,
            client=self.client_factory() if self.client_factory else None,
        )
        await connection.start()
        return connection

    def outbound_parts(self, text: str) -> list[str]:
        return wx.split_wechat_parts(text)

    def fallback(self, kind: str) -> str:
        return wx.WECHAT_TIMEOUT_REPLY if kind == "timeout" else wx.WECHAT_FAILURE_REPLY
