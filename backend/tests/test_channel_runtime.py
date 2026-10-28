"""多渠道注册表与 provider 契约的验证。

FakeProvider 只实现 bot/provider.py 里那 5 个成员，回复走闭包而不是消息对象
（钉钉的 sessionWebhook 就是这个形状）。这组测试跑通即说明渠道缝切对了：
被测代码里没有任何 QQ 依赖。
"""

import asyncio

import pytest

import database as db
from bot.channel_runtime import ChannelRuntime
from bot.provider import (
    ChannelAlreadyConnectedError,
    ChannelConnectError,
    InboundMessage,
    UnsupportedProviderError,
)


class FakeConnection:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.status = {"state": "online", "detail": ""}
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1
        self.status = {"state": "offline", "detail": ""}


class FakeProvider:
    name = "fake"
    reply_window_s = 5.0

    def __init__(self):
        self.connections = {}
        self.handlers = {}
        self.sent: list[tuple[str, str]] = []
        self.fail_with = ""

    async def start(self, cfg, on_inbound, *, wait_ready=True, ready_timeout=20.0):
        if self.fail_with:
            detail, self.fail_with = self.fail_with, ""
            raise ChannelConnectError(detail, fatal=True)
        connection = FakeConnection(cfg.channel_id)
        self.connections[cfg.channel_id] = connection
        self.handlers[cfg.channel_id] = on_inbound
        return connection

    def outbound_parts(self, text):
        return [part.strip() for part in text.split("||") if part.strip()]

    def fallback(self, kind):
        return "超时兜底" if kind == "timeout" else "失败兜底"

    async def push(self, channel_id, key, event_id, text, *, send_fails=False):
        """模拟平台推来一条消息：平台原始对象不外泄，只交 InboundMessage。"""
        sent = []

        async def send(part):
            if send_fails:
                raise RuntimeError("发送通道故障")
            sent.append(part)
            self.sent.append((channel_id, part))

        await self.handlers[channel_id](
            InboundMessage(
                channel_id=channel_id,
                conversation_key=key,
                event_id=event_id,
                text=text,
                send=send,
            )
        )
        return sent


class FakeChatService:
    def __init__(self):
        self.calls = []

    async def complete_reply(
        self, conversation_id, content, external_msg_id=None, *, channel="local"
    ):
        self.calls.append((conversation_id, content, external_msg_id, channel))
        db.add_bot_message(conversation_id, "user", content, external_msg_id)
        reply = f"回复：{content}"
        db.add_bot_message(conversation_id, "assistant", reply)
        return reply


@pytest.fixture
def rig(base_db):
    provider = FakeProvider()
    service = FakeChatService()
    runtime = ChannelRuntime(providers={provider.name: provider}, chat_service=service)
    return runtime, provider, service


def _channel(persona_name, display_name, app_id):
    persona = db.create_bot_persona(name=persona_name)
    channel = db.create_bot_channel(
        provider="fake",
        display_name=display_name,
        persona_id=persona["id"],
        app_id=app_id,
        app_secret="s",
    )
    return channel, persona


@pytest.mark.asyncio
async def test_two_channels_connect_side_by_side(rig):
    runtime, provider, _ = rig
    first, warm = _channel("小暖", "机器人A", "100001")
    second, cool = _channel("阿冷", "机器人B", "100002")

    await runtime.start(first)
    await runtime.start(second)

    assert provider.connections[first["id"]].status["state"] == "online"
    assert provider.connections[second["id"]].status["state"] == "online"
    assert runtime.statuses()[first["id"]]["state"] == "online"
    # 每个连接拿到的是自己的凭据与人格，不是别人的
    assert provider.handlers[first["id"]] is not provider.handlers[second["id"]]
    assert warm["id"] != cool["id"]


@pytest.mark.asyncio
async def test_inbound_is_attributed_to_its_own_channel_and_persona(rig):
    runtime, provider, service = rig
    channel, persona = _channel("小暖", "机器人A", "100001")
    await runtime.start(channel)

    await provider.push(channel["id"], "group:openid-1", "event-1", "你好")

    conversation_id = service.calls[0][0]
    conversation = db.get_bot_conversation(conversation_id)
    assert conversation["channel"] == "fake"
    assert conversation["channel_id"] == channel["id"]
    assert conversation["persona_id"] == persona["id"]
    # 纯平台身份，不再往 key 尾巴塞 context_id
    assert conversation["external_id"] == "group:openid-1"
    # 渠道名同时是 prompt 的格式分支依据
    assert service.calls[0][3] == "fake"


@pytest.mark.asyncio
async def test_same_group_on_two_channels_is_two_conversations(rig):
    runtime, provider, service = rig
    first, _ = _channel("小暖", "机器人A", "100001")
    second, _ = _channel("阿冷", "机器人B", "100002")
    await runtime.start(first)
    await runtime.start(second)

    await provider.push(first["id"], "group:openid-1", "event-a", "群里好")
    await provider.push(second["id"], "group:openid-1", "event-b", "群里好")

    assert service.calls[0][0] != service.calls[1][0]
    rows = db.list_bot_conversations(channel="fake")
    assert {row["channel_id"] for row in rows} == {first["id"], second["id"]}
    assert provider.sent == [(first["id"], "回复：群里好"), (second["id"], "回复：群里好")]


@pytest.mark.asyncio
async def test_reply_goes_out_through_the_provided_closure(rig):
    """分段与「首段成功即标已答」由管线负责，怎么切段是 provider 的事。"""

    class PartsService(FakeChatService):
        async def complete_reply(self, conversation_id, content, external_msg_id=None, **_kw):
            db.add_bot_message(conversation_id, "user", content, external_msg_id)
            return "第一段 || 第二段 || 第三段"

    provider = FakeProvider()
    runtime = ChannelRuntime(providers={provider.name: provider}, chat_service=PartsService())
    channel, _ = _channel("小暖", "机器人A", "100001")
    await runtime.start(channel)

    sent = await provider.push(channel["id"], "c2c:user-1", "event-1", "讲讲")

    assert sent == ["第一段", "第二段", "第三段"]
    conversation = db.list_bot_conversations(channel_id=channel["id"])[0]
    stored = db.get_bot_message_by_external_id(conversation["id"], "event-1")
    assert stored["answered_at"]


@pytest.mark.asyncio
async def test_send_failure_keeps_the_event_retryable(rig):
    runtime, provider, service = rig
    channel, _ = _channel("小暖", "机器人A", "100001")
    await runtime.start(channel)

    await provider.push(channel["id"], "group:g-1", "event-1", "你好", send_fails=True)

    conversation = db.list_bot_conversations(channel_id=channel["id"])[0]
    stored = db.get_bot_message_by_external_id(conversation["id"], "event-1")
    assert stored["answered_at"] is None

    # 平台重投同一条事件：还要能再答一次
    sent = await provider.push(channel["id"], "group:g-1", "event-1", "你好")
    assert sent == ["回复：你好"]
    assert len(service.calls) == 2
    assert db.get_bot_message_by_external_id(conversation["id"], "event-1")["answered_at"]


@pytest.mark.asyncio
async def test_generation_timeout_uses_the_providers_fallback(rig):
    class SlowService(FakeChatService):
        async def complete_reply(self, conversation_id, content, external_msg_id=None, **_kw):
            db.add_bot_message(conversation_id, "user", content, external_msg_id)
            await asyncio.sleep(1)

    provider = FakeProvider()
    provider.reply_window_s = 0.05
    runtime = ChannelRuntime(providers={provider.name: provider}, chat_service=SlowService())
    channel, _ = _channel("小暖", "机器人A", "100001")
    await runtime.start(channel)

    sent = await provider.push(channel["id"], "c2c:user-1", "event-1", "在吗")

    assert sent == ["超时兜底"]
    conversation = db.list_bot_conversations(channel_id=channel["id"])[0]
    assert db.get_bot_message_by_external_id(conversation["id"], "event-1")["answered_at"]


@pytest.mark.asyncio
async def test_model_error_sends_the_providers_failure_fallback(rig):
    class BrokenService:
        async def complete_reply(self, *_args, **_kwargs):
            raise RuntimeError("上游细节不外泄")

    provider = FakeProvider()
    runtime = ChannelRuntime(providers={provider.name: provider}, chat_service=BrokenService())
    channel, _ = _channel("小暖", "机器人A", "100001")
    await runtime.start(channel)

    sent = await provider.push(channel["id"], "c2c:user-1", "event-1", "你好")
    assert sent == ["失败兜底"]


@pytest.mark.asyncio
async def test_first_turn_prefixes_the_persona_greeting(rig):
    provider = FakeProvider()
    runtime = ChannelRuntime(providers={provider.name: provider}, chat_service=FakeChatService())
    channel, persona = _channel("小暖", "机器人A", "100001")
    db.update_bot_persona(persona["id"], greeting="今天过得怎么样？")
    await runtime.start(channel)

    sent = await provider.push(channel["id"], "c2c:user-1", "event-1", "在吗")
    assert sent == ["今天过得怎么样？\n\n回复：在吗"]

    # 第二回合不再前置
    await provider.push(channel["id"], "c2c:user-1", "event-2", "再来")
    assert provider.sent[-1][1] == "回复：再来"


@pytest.mark.asyncio
async def test_starting_the_same_channel_twice_is_rejected(rig):
    runtime, provider, _ = rig
    channel, _ = _channel("小暖", "机器人A", "100001")
    await runtime.start(channel)

    with pytest.raises(ChannelAlreadyConnectedError):
        await runtime.start(channel)

    assert len(provider.connections) == 1


@pytest.mark.asyncio
async def test_an_unsupported_provider_is_reported(rig):
    runtime, _, _ = rig
    channel, _ = _channel("小暖", "机器人A", "100001")
    channel = {**channel, "provider": "telegram"}

    with pytest.raises(UnsupportedProviderError) as info:
        await runtime.start(channel)
    assert "telegram" in str(info.value)


@pytest.mark.asyncio
async def test_a_failing_channel_does_not_disturb_the_others(rig):
    runtime, provider, _ = rig
    first, _ = _channel("小暖", "机器人A", "100001")
    second, _ = _channel("阿冷", "机器人B", "100002")
    await runtime.start(first)

    provider.fail_with = "AppID 或 AppSecret 不正确"
    with pytest.raises(ChannelConnectError):
        await runtime.start(second)

    # 失败的那条不留残留句柄：状态回到 offline，修好凭据后可以重来
    assert runtime.status(second["id"]) == {"state": "offline", "detail": ""}
    assert first["id"] in provider.connections
    await runtime.start(second)
    assert provider.connections[second["id"]].status["state"] == "online"


@pytest.mark.asyncio
async def test_stop_only_stops_that_channel(rig):
    runtime, provider, _ = rig
    first, _ = _channel("小暖", "机器人A", "100001")
    second, _ = _channel("阿冷", "机器人B", "100002")
    await runtime.start(first)
    await runtime.start(second)

    await runtime.stop(first["id"])

    assert provider.connections[first["id"]].stop_calls == 1
    assert provider.connections[second["id"]].stop_calls == 0
    assert runtime.status(first["id"]) == {"state": "offline", "detail": ""}
    assert runtime.status(second["id"])["state"] == "online"


@pytest.mark.asyncio
async def test_restore_brings_back_enabled_channels_and_skips_broken_one(rig):
    runtime, provider, _ = rig
    first, _ = _channel("小暖", "机器人A", "100001")
    broken, _ = _channel("阿冷", "机器人B", "100002")
    other, _ = _channel("阿冷", "机器人C", "100003")
    for channel in (first, broken, other):
        db.set_bot_channel_enabled(channel["id"], True)
    db.update_bot_channel(broken["id"], provider="telegram")

    await runtime.restore()

    # 凭据/渠道类型坏掉的那个不能拖累其余两个开机接回
    assert first["id"] in provider.connections
    assert other["id"] in provider.connections
    assert broken["id"] not in provider.connections
