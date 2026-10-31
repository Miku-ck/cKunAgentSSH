"""微信连接句柄：本地自检、回调处理、sendmsg/kefustate 与服务体验。

HTTP 用假客户端截获，报文用真实 WeChatCrypto 解密断言 —— 测的是
「发出去的到底是不是平台能解的那份 XML」，不是 httpx 本身。
"""

import asyncio
import base64
import json
import os
import xml.etree.ElementTree as ET

import pytest

import database as db
from bot.provider import ChannelConfig, ChannelConnectError
from bot.wechat_channel import parse_inbound
from bot.wechat_crypto import WeChatCrypto
from bot.wechat_manager import WeChatConnection, WeChatProvider


def _aes_key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def _inbound_xml(
    *,
    userid="o-user-1",
    appid="wx-app",
    msg="你好",
    channel=0,
    from_=0,
    event="",
    createtime="1700000000",
) -> str:
    return (
        "<xml>"
        f"<userid><![CDATA[{userid}]]></userid>"
        f"<appid><![CDATA[{appid}]]></appid>"
        f"<createtime>{createtime}</createtime>"
        f"<content><msg><![CDATA[{msg}]]></msg></content>"
        f"<from>{from_}</from>"
        f"<event>{event}</event>"
        f"<kfstate>0</kfstate>"
        f"<channel>{channel}</channel>"
        "</xml>"
    )


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeClient:
    def __init__(self, data=None, serving=None):
        self.data = data if data is not None else {"errcode": 0, "errmsg": "ok"}
        # 回读状态默认「已接管」；给需要断言未生效场景的用例单独指定。
        self.serving = serving if serving is not None else {
            "errcode": 0,
            "kefustate": "on",
            "status": "personserving",
        }
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, json))
        if "kefustate/get" in url:
            return FakeResponse(dict(self.serving))
        return FakeResponse(self.data)

    async def aclose(self):
        self.closed = True


def _cfg(key, *, app_id="wx-app", token="token-123", channel_id="channel-1"):
    return ChannelConfig(
        channel_id=channel_id,
        app_id=app_id,
        app_secret=token,
        persona_id="persona-1",
        extra_secret=key,
    )


async def _noop(_message) -> None:
    return None


async def _drain(connection: WeChatConnection) -> None:
    for _ in range(10):
        if not connection._tasks:
            return
        await asyncio.gather(*list(connection._tasks), return_exceptions=True)


def _make(key):
    client = FakeClient()
    received = []

    async def on_inbound(message):
        received.append(message)

    connection = WeChatConnection(_cfg(key), on_inbound, client=client)
    return connection, client, received


def _encrypted(key, xml_text, app_id="wx-app") -> str:
    return WeChatCrypto(app_id, key).encrypt(xml_text)


def _outbound_xml(key, encrypted, app_id="wx-app"):
    # 出站 XML 没有 userid，不能走 parse_inbound（那只认入站字段）。
    return ET.fromstring(WeChatCrypto(app_id, key).decrypt(encrypted))


@pytest.mark.asyncio
async def test_start_self_checks_credentials_without_network():
    key = _aes_key()
    connection, client, _ = _make(key)
    await connection.start()

    assert connection.status == {"state": "online", "detail": "已就绪，等待微信回调"}
    assert client.calls == []
    await connection.stop()


@pytest.mark.asyncio
async def test_bad_credentials_fail_fatally():
    with pytest.raises(ChannelConnectError) as info:
        await WeChatProvider().start(_cfg("short-key"), lambda _m: None)
    assert info.value.fatal is True

    with pytest.raises(ChannelConnectError) as info:
        await WeChatProvider().start(_cfg(_aes_key(), token=""), lambda _m: None)
    assert info.value.fatal is True


@pytest.mark.asyncio
async def test_accept_decrypts_acks_and_hands_off_to_the_pipeline():
    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()

    ack = await connection.accept(
        {"encrypted": _encrypted(key, _inbound_xml(msg="今天好累"))}
    )
    assert ack == {"errcode": 0, "errmsg": "ok"}
    await _drain(connection)

    assert len(received) == 1
    message = received[0]
    assert message.channel_id == "channel-1"
    assert message.conversation_key == "wx:o-user-1"
    assert message.text == "今天好累"
    assert message.event_id
    # 一条入站同时触发接管（kefustate=personserving），并回读确认已生效
    url, payload = client.calls[0]
    assert url.endswith("/openapi/kefustate/change/token-123")
    state = _outbound_xml(key, payload["encrypt"])
    assert state.findtext("openid") == "o-user-1"
    assert state.findtext("kefustate") == "personserving"
    assert any("kefustate/get" in u for u, _ in client.calls)
    await connection.stop()


@pytest.mark.asyncio
async def test_status_switches_to_connected_after_first_real_message():
    key = _aes_key()
    connection, _client, _ = _make(key)
    await connection.start()
    assert connection.status["detail"] == "已就绪，等待微信回调"

    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(msg="在吗"))})
    await _drain(connection)

    assert connection.status["state"] == "online"
    detail = connection.status["detail"]
    assert detail.startswith("已接通")
    assert "最近收到消息" in detail
    assert "等待微信回调" not in detail
    await connection.stop()


@pytest.mark.asyncio
async def test_robot_agent_and_event_callbacks_are_ignored():
    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()

    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(from_=1))})
    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(event="enter"))})
    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(msg=""))})
    await _drain(connection)

    assert received == []
    assert client.calls == []
    # 机器人/事件回调不算「收到消息」，状态不该从等待变成已接通
    assert connection.status["detail"] == "已就绪，等待微信回调"
    await connection.stop()


@pytest.mark.asyncio
async def test_inbound_appid_is_the_bound_account_not_the_platform_appid():
    # 入站 XML 的 appid 是公众号 appid（与开放平台 APPID 不同）：
    # 密文里的平台 appid 已由 AES 校验，这里不能拿它再比对一次把真消息拒掉。
    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()

    ack = await connection.accept(
        {"encrypted": _encrypted(key, _inbound_xml(appid="wx-official", msg="在吗"))}
    )
    await _drain(connection)

    assert ack == {"errcode": 0, "errmsg": "ok"}
    assert len(received) == 1
    assert client.calls  # 同时完成接管
    await connection.stop()


@pytest.mark.asyncio
async def test_sendmsg_encrypts_one_bubble_per_call_and_binds_inbound_identity():
    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()
    await connection.accept(
        {"encrypted": _encrypted(key, _inbound_xml(userid="o-9", channel=0))}
    )
    await _drain(connection)
    client.calls.clear()

    await received[0].send("第一段 ]]> 带符号")

    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert url.endswith("/openapi/sendmsg/token-123")
    sent = _outbound_xml(key, payload["encrypt"])
    assert sent.findtext("appid") == "wx-app"
    assert sent.findtext("openid") == "o-9"
    assert sent.findtext("msg") == "第一段 ]]> 带符号"
    assert sent.findtext("channel") == "0"
    await connection.stop()


class FirstSendFailsClient(FakeClient):
    """图片消息被平台拒绝：只让第一次 sendmsg 失败，用来验证回落颜文字。"""

    def __init__(self):
        super().__init__()
        self.failed = False

    async def post(self, url, json=None, headers=None):
        if "sendmsg" in url and not self.failed:
            self.failed = True
            self.calls.append((url, json))
            raise RuntimeError("image url rejected")
        return await super().post(url, json, headers)


@pytest.mark.asyncio
async def test_sticker_sends_rich_image_url_built_from_public_base(base_db):
    db.set_app_setting("bot.webhook_base_url", "https://bot.example.com/")
    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()
    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(userid="o-9"))})
    await _drain(connection)
    client.calls.clear()

    await received[0].send("[表情:开心]")

    assert len(client.calls) == 1
    sent = _outbound_xml(key, client.calls[0][1]["encrypt"])
    assert json.loads(sent.findtext("msg")) == {
        "image": {"url": "https://bot.example.com/assets/stickers/%E5%BC%80%E5%BF%83"}
    }
    await connection.stop()


@pytest.mark.asyncio
async def test_sticker_without_public_base_falls_back_to_kaomoji(base_db):
    from bot import stickers

    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()
    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(userid="o-9"))})
    await _drain(connection)
    client.calls.clear()

    await received[0].send("[表情:困了]")

    sent = _outbound_xml(key, client.calls[0][1]["encrypt"])
    assert sent.findtext("msg") == stickers.kaomoji_for("困了")
    await connection.stop()


@pytest.mark.asyncio
async def test_sticker_image_rejection_falls_back_to_kaomoji(base_db):
    from bot import stickers

    db.set_app_setting("bot.webhook_base_url", "https://bot.example.com")
    key = _aes_key()
    client = FirstSendFailsClient()
    connection = WeChatConnection(_cfg(key), lambda _m: None, client=client)
    await connection.start()
    inbound = parse_inbound(_inbound_xml(userid="o-9"))

    await connection._sender(inbound)("[表情:生气]")

    assert len(client.calls) == 2
    sent = _outbound_xml(key, client.calls[1][1]["encrypt"])
    assert sent.findtext("msg") == stickers.kaomoji_for("生气")
    await connection.stop()


@pytest.mark.asyncio
async def test_h5_channel_omits_appid():
    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()
    await connection.accept(
        {"encrypted": _encrypted(key, _inbound_xml(channel=7))}
    )
    await _drain(connection)
    client.calls.clear()

    await received[0].send("在吗")

    sent = _outbound_xml(key, client.calls[0][1]["encrypt"])
    assert sent.find("appid") is None
    assert sent.findtext("channel") == "7"
    await connection.stop()


@pytest.mark.asyncio
async def test_send_failure_raises_and_keeps_the_token_out_of_the_message():
    key = _aes_key()
    client = FakeClient({"errcode": 1001, "errmsg": "TOKEN is not valid"})
    connection = WeChatConnection(_cfg(key), lambda _m: None, client=client)
    await connection.start()

    with pytest.raises(RuntimeError) as info:
        await connection._sendmsg(
            parse_inbound(_inbound_xml()), "你好"
        )
    assert "errcode=1001" in str(info.value)
    assert "token-123" not in str(info.value)
    await connection.stop()


@pytest.mark.asyncio
async def test_kefustate_uses_the_bound_account_appid_not_the_platform_appid():
    # 平台文档：kefustate 的 appid 是「公众号或小程序的 appid」，不是开放平台 APPID。
    # 传错时代码返回 errcode 0 但状态不生效，用户永远拿不到客服消息。
    key = _aes_key()
    client = FakeClient()
    connection = WeChatConnection(
        _cfg(key, app_id="platform-app"), _noop, client=client
    )
    await connection.start()

    await connection.accept(
        {"encrypted": _encrypted(key, _inbound_xml(appid="wx-official", msg="在吗"), app_id="platform-app")}
    )
    await _drain(connection)

    url, payload = client.calls[0]
    assert "kefustate/change" in url
    state = _outbound_xml(key, payload["encrypt"], app_id="platform-app")
    assert state.findtext("appid") == "wx-official"
    assert state.findtext("kefustate") == "personserving"
    await connection.stop()


@pytest.mark.asyncio
async def test_takeover_reads_back_state_and_warns_when_it_did_not_stick(caplog):
    # change 对 appid 不做校验（假的也回 errcode 0），只有回读能看出平台机器人还在应答。
    key = _aes_key()
    client = FakeClient(serving={"errcode": 0, "kefustate": "off", "status": "asking"})
    connection = WeChatConnection(_cfg(key), _noop, client=client)
    await connection.start()

    with caplog.at_level("WARNING", logger="bot.wechat_manager"):
        await connection.accept(
            {"encrypted": _encrypted(key, _inbound_xml(appid="wx-official", msg="在吗"))}
        )
        await _drain(connection)

    get_calls = [(u, p) for u, p in client.calls if "kefustate/get" in u]
    assert len(get_calls) == 1
    asked = _outbound_xml(key, get_calls[0][1]["encrypt"])
    assert asked.findtext("appid") == "wx-official"
    assert asked.findtext("openid") == "o-user-1"
    assert "未生效" in caplog.text
    assert "kefustate=off" in caplog.text
    await connection.stop()


@pytest.mark.asyncio
async def test_sendmsg_never_falls_back_to_the_platform_appid():
    # 平台对 sendmsg 的 appid 做校验：开放平台 APPID 会回 1011 appid is not valid。
    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()
    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(appid="", channel=0))})
    await _drain(connection)
    client.calls.clear()

    await received[0].send("在吗")

    sent = _outbound_xml(key, client.calls[0][1]["encrypt"])
    assert sent.find("appid") is None
    assert sent.findtext("channel") == "0"
    await connection.stop()


@pytest.mark.asyncio
async def test_release_reuses_the_appid_seen_on_inbound(base_db):
    key = _aes_key()
    persona = db.create_bot_persona(name="小暖")
    channel = db.create_bot_channel(
        provider="wechat",
        display_name="微信A",
        persona_id=persona["id"],
        app_id="platform-app",
        app_secret=db.protect_secret("token-123"),
        extra_secret=db.protect_secret(key),
    )
    conversation = db.create_bot_conversation(
        persona["id"], "wechat", "wx:o-user-1", channel_id=channel["id"]
    )
    db.add_bot_message(conversation["id"], "user", "在吗")

    client = FakeClient()
    connection = WeChatConnection(
        _cfg(key, app_id="platform-app", channel_id=channel["id"]),
        _noop,
        client=client,
    )
    await connection.start()
    await connection.accept(
        {"encrypted": _encrypted(key, _inbound_xml(appid="wx-official", msg="在吗"), app_id="platform-app")}
    )
    await _drain(connection)
    client.calls.clear()

    await connection.release()

    url, payload = client.calls[0]
    assert "kefustate/change" in url
    released = _outbound_xml(key, payload["encrypt"], app_id="platform-app")
    assert released.findtext("appid") == "wx-official"
    assert released.findtext("kefustate") == "complete"
    await connection.stop()


@pytest.mark.asyncio
async def test_takeover_is_cached_per_user_and_deduped_in_flight():
    key = _aes_key()
    connection, client, received = _make(key)
    await connection.start()

    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(createtime="1"))})
    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(createtime="2"))})
    await _drain(connection)

    takeovers = [url for url, _ in client.calls if "kefustate/change" in url]
    assert len(takeovers) == 1
    assert len(received) == 2
    await connection.stop()


@pytest.mark.asyncio
async def test_release_returns_recent_users_to_the_platform_bot(base_db):
    persona = db.create_bot_persona(name="小暖")
    channel = db.create_bot_channel(
        provider="wechat",
        display_name="微信A",
        persona_id=persona["id"],
        app_id="wx-app",
        app_secret=db.protect_secret("token-123"),
        extra_secret=db.protect_secret(_aes_key()),
    )
    conversation = db.create_bot_conversation(
        persona["id"], "wechat", "wx:o-gone", channel_id=channel["id"]
    )
    db.add_bot_message(conversation["id"], "user", "在吗")

    key = db.get_bot_channel_extra_secret(channel["id"])
    client = FakeClient()
    connection = WeChatConnection(
        _cfg(key, channel_id=channel["id"]), _noop, client=client
    )
    await connection.start()
    # 释放只认得「本进程见过 appid」的用户：先来一条该用户的消息。
    await connection.accept({"encrypted": _encrypted(key, _inbound_xml(userid="o-gone"))})
    await _drain(connection)
    client.calls.clear()

    await connection.release()

    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert url.endswith("/openapi/kefustate/change/token-123")
    released = _outbound_xml(key, payload["encrypt"])
    assert released.findtext("openid") == "o-gone"
    assert released.findtext("kefustate") == "complete"
    await connection.stop()


@pytest.mark.asyncio
async def test_stop_cancels_background_work_and_marks_offline():
    key = _aes_key()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_message):
        started.set()
        await release.wait()

    connection = WeChatConnection(_cfg(key), slow, client=FakeClient())
    await connection.start()
    await connection.accept({"encrypted": _encrypted(key, _inbound_xml())})
    await started.wait()

    await connection.stop()

    assert connection.status == {"state": "offline", "detail": ""}
    assert connection._tasks == set()


def test_event_id_is_stable_across_redelivery():
    xml_text = _inbound_xml(userid="o-1", createtime="123", msg="同一条")
    from bot.wechat_channel import inbound_event_id

    first = inbound_event_id("o-1", "123", "同一条")
    assert first == inbound_event_id("o-1", "123", "同一条")
    assert first != inbound_event_id("o-1", "124", "同一条")
    assert ET.fromstring(xml_text).findtext("userid") == "o-1"
