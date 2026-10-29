"""QQ 平台侧片段：事件转换与出站文本。

消息流程（去重、首回合、超时、分段投递）在 test_channel_runtime.py 里用
FakeProvider 验证；这里只测 QQ 自己的那两件事。
"""

import asyncio
from types import SimpleNamespace

import pytest

from bot.qq_channel import (
    QQInboundAdapter,
    split_qq_text,
    strip_markdown_for_qq,
)


class FakeMessage:
    def __init__(self, event_id, content, group_openid=None, user_openid=None):
        self.id = event_id
        self.event_id = event_id
        self.content = content
        self.group_openid = group_openid
        self.author = SimpleNamespace(member_openid="member-1", user_openid=user_openid)
        self.replies = []
        self.seqs = []

    async def reply(self, content, msg_type=0, msg_seq=None):
        self.replies.append(content)
        self.seqs.append(msg_seq)


def collect_adapter(channel_id="channel-1"):
    received = []

    async def on_inbound(inbound):
        received.append(inbound)

    return QQInboundAdapter(channel_id, on_inbound), received


@pytest.mark.asyncio
async def test_group_event_becomes_an_inbound_with_a_platform_only_key():
    adapter, received = collect_adapter()
    message = FakeMessage("event-1", "  你好  ", group_openid="group-1")

    await adapter.handle_group(message)

    inbound = received[0]
    assert inbound.channel_id == "channel-1"
    # 机器人归属由 channel_id 列承担，key 里不再塞 context_id
    assert inbound.conversation_key == "group:group-1"
    assert inbound.text == "你好"
    assert inbound.event_id == "event-1"

    await inbound.send("在的")
    assert message.replies == ["在的"]


@pytest.mark.asyncio
async def test_parts_of_the_same_event_carry_increasing_msg_seq():
    """QQ 按 (msg_id, msg_seq) 去重：同一个事件连发多段时序号必须递增。

    不递增的代价是静默丢消息 —— 平台回 40054005「消息被去重，请检查请求msgseq」，
    于是本地记录三条、QQ 上只到一条。
    """
    adapter, received = collect_adapter()
    message = FakeMessage("event-1", "在吗", user_openid="user-9")
    await adapter.handle_c2c(message)
    inbound = received[0]

    for part in ("第一句", "第二句", "第三句"):
        await inbound.send(part)

    assert message.replies == ["第一句", "第二句", "第三句"]
    assert message.seqs == [1, 2, 3]


@pytest.mark.asyncio
async def test_private_event_uses_the_user_openid():
    adapter, received = collect_adapter()
    await adapter.handle_c2c(FakeMessage("event-2", "私下说", user_openid="user-9"))

    assert received[0].conversation_key == "c2c:user-9"


@pytest.mark.asyncio
async def test_event_id_falls_back_to_message_id():
    adapter, received = collect_adapter()
    message = FakeMessage("msg-7", "你好", user_openid="user-9")
    message.event_id = None
    await adapter.handle_c2c(message)

    assert received[0].event_id == "msg-7"


@pytest.mark.asyncio
async def test_empty_content_or_missing_id_is_dropped():
    adapter, received = collect_adapter()
    await adapter.handle_c2c(FakeMessage("event-1", "   ", user_openid="user-9"))
    await adapter.handle_c2c(FakeMessage(None, "有内容", user_openid="user-9"))

    assert received == []


def test_markdown_is_stripped_before_sending():
    assert strip_markdown_for_qq("# 标题\n**加粗** 与 `代码`") == "标题\n加粗 与 代码"


def test_long_reply_splits_on_sentence_boundaries():
    text = "第一句话。" * 300
    parts = split_qq_text(text)

    assert 1 < len(parts) <= 4
    assert all(len(part) <= 800 for part in parts)
    assert "".join(parts).replace("\n", "").startswith("第一句话。")


def test_overlong_reply_is_truncated_at_the_last_part():
    parts = split_qq_text("啊" * 4000)

    assert len(parts) == 4
    assert parts[-1].endswith("……")


def test_short_reply_stays_one_part():
    assert split_qq_text("就在") == ["就在"]


def test_outbound_parts_sends_one_message_per_blank_line():
    """模型用空行分开说的，QQ 就一条一条连发 —— 真人在群里就是这样。"""
    from bot.qq_manager import QQProvider

    assert QQProvider().outbound_parts("在的\n\n晚上八点开一把，来不来") == [
        "在的",
        "晚上八点开一把，来不来",
    ]


def test_outbound_parts_respects_platform_quota_without_dropping_words():
    """被动回复只有 4 条额度：超出的分条并回末条，宁可长也不吞字。

    每段都写够一句话，否则先被「三截薄气泡并回一条」那条规则收走，测不到额度。
    """
    from bot.qq_manager import QQProvider

    five = [
        "今天这局我先出牌，你把防守的那一手留到最后一轮再跟。",
        "上次你说明天再打，结果拖到周末才上线，我一直惦记着这事。",
        "这周要是真有事就直说，别到点才冒一句不打了，我看着难受。",
        "下回提前半小时说一声，我好把饭吃了再来，不用饿着肚子等你。",
        "要是你只是想自己安静打两把，也直接讲一句给我，我不会多想。",
    ]

    parts = QQProvider().outbound_parts("\n\n".join(five))

    assert len(parts) == 4
    assert "".join(parts).replace("\n", "") == "".join(five)
    assert parts[-1] == "\n\n".join(five[3:])


@pytest.mark.asyncio
async def test_provider_outbound_parts_cleans_then_splits():
    from bot.qq_manager import QQProvider

    provider = QQProvider()
    parts = provider.outbound_parts("# 标题\n" + "**第一段**" + "啊" * 900 + "\n" + "嗯" * 900)

    assert 2 <= len(parts) <= 4
    assert all(len(part) <= 800 for part in parts)
    joined = "".join(parts)
    assert "**" not in joined and "`" not in joined and "#" not in joined
    assert "标题" in joined and "第一段" in joined


def test_provider_fallback_text_is_per_kind():
    from bot.qq_manager import QQProvider

    provider = QQProvider()
    assert provider.fallback("timeout") == "这条我想得有点久，你稍后再跟我说一次好吗？"
    assert provider.fallback("failure") == "我这边刚才没接上话，请稍后再试一次。"
    assert provider.name == "qq"
    assert provider.reply_window_s == 150


def test_provider_start_creates_an_independent_connection():
    """provider 每次 start 交出一个新句柄：这是「多个机器人」的最低要求。"""
    from bot.qq_manager import QQProvider

    clients = []

    def factory(processor, on_ready):
        class C:
            def __init__(self):
                self.on_ready = on_ready
                self.closed = False

            async def start(self, appid, secret, ret_coro=False):
                await self.on_ready()

                async def listen():
                    await asyncio.Future()

                return listen()

            async def close(self):
                self.closed = True

        client = C()
        clients.append(client)
        return client

    async def scenario():
        provider = QQProvider(client_factory=factory)
        config = SimpleNamespace(
            channel_id="ch-1", app_id="app-id", app_secret="secret", persona_id="p-1"
        )
        first = await provider.start(config, lambda _i: asyncio.sleep(0))
        second = await provider.start(config, lambda _i: asyncio.sleep(0))
        assert first is not second
        await first.stop()
        await second.stop()
        return first.status, second.status

    assert asyncio.run(scenario()) == (
        {"state": "offline", "detail": ""},
        {"state": "offline", "detail": ""},
    )
