"""QQ 平台侧片段：事件转换与出站文本。

消息流程（去重、首回合、超时、分段投递）在 test_channel_runtime.py 里用
FakeProvider 验证；这里只测 QQ 自己的那两件事。
"""

import asyncio
from types import SimpleNamespace

import pytest

from bot.qq_channel import (
    QQ_MAX_PARTS,
    QQInboundAdapter,
    classify_proactive_error,
    parse_conversation_key,
    strip_markdown_for_qq,
)
from bot.text_format import split_text


class FakeHttp:
    def __init__(self, files=None):
        self.files = files if files is not None else {"file_info": "media-1"}
        self.calls = []

    async def request(self, route, json=None):
        self.calls.append((route, json))
        if isinstance(self.files, Exception):
            raise self.files
        return self.files


class FakeMessage:
    def __init__(self, event_id, content, group_openid=None, user_openid=None):
        self.id = event_id
        self.event_id = event_id
        self.content = content
        self.group_openid = group_openid
        self.author = SimpleNamespace(member_openid="member-1", user_openid=user_openid)
        self.replies = []
        self.seqs = []
        self.media = []
        self.msg_types = []
        self.http = FakeHttp()
        self._api = SimpleNamespace(_http=self.http)

    async def reply(self, content=None, msg_type=0, msg_seq=None, media=None):
        self.replies.append(content)
        self.seqs.append(msg_seq)
        self.media.append(media)
        self.msg_types.append(msg_type)


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
    parts = split_text(text, limit=800, max_parts=QQ_MAX_PARTS)

    assert 1 < len(parts) <= QQ_MAX_PARTS
    assert all(len(part) <= 800 for part in parts)
    assert "".join(parts).replace("\n", "").startswith("第一句话。")


def test_overlong_reply_is_truncated_at_the_last_part():
    parts = split_text("啊" * 4000, limit=800, max_parts=QQ_MAX_PARTS)

    assert len(parts) == QQ_MAX_PARTS
    assert parts[-1].endswith("……")


def test_short_reply_stays_one_part():
    assert split_text("就在", limit=800, max_parts=QQ_MAX_PARTS) == ["就在"]


def test_outbound_parts_sends_one_message_per_blank_line():
    """模型用空行分开说的，QQ 就一条一条连发 —— 真人在群里就是这样。"""
    from bot.qq_manager import QQProvider

    assert QQProvider().outbound_parts("在的\n\n晚上八点开一把，来不来") == [
        "在的",
        "晚上八点开一把，来不来",
    ]


def test_outbound_parts_caps_parts_without_dropping_words():
    """条数上限只是失控兜底：超出并回末条，宁可长也不吞字。

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

    assert len(parts) == QQ_MAX_PARTS
    assert "".join(parts).replace("\n", "") == "".join(five)
    assert parts[-1] == "\n\n".join(five[QQ_MAX_PARTS - 1:])


@pytest.mark.asyncio
async def test_sticker_bubble_uploads_media_then_sends_msg_type_7():
    adapter, received = collect_adapter()
    message = FakeMessage("event-1", "在吗", group_openid="group-1")
    await adapter.handle_group(message)
    inbound = received[0]

    await inbound.send("[表情:开心]")

    route, payload = message.http.calls[0]
    assert route.method == "POST"
    assert route.path == "/v2/groups/{group_openid}/files"
    assert route.parameters == {"group_openid": "group-1"}
    assert payload["srv_send_msg"] is False
    assert payload["file_data"]
    assert message.msg_types == [7]
    assert message.media == [{"file_info": "media-1"}]
    assert message.replies == [None]
    assert message.seqs == [1]


@pytest.mark.asyncio
async def test_sticker_upload_failure_falls_back_to_kaomoji():
    from bot import stickers

    adapter, received = collect_adapter()
    message = FakeMessage("event-1", "在吗", group_openid="group-1")
    message.http.files = RuntimeError("upload rejected")
    await adapter.handle_group(message)
    inbound = received[0]

    await inbound.send("[表情:害羞]")

    assert message.msg_types == [0]
    assert message.replies == [stickers.kaomoji_for("害羞")]
    assert message.seqs == [1]


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


# ── 主动消息：会话键解析、失败分类、最小 payload ──


class ForbiddenError(RuntimeError):
    """与 botpy.errors.ForbiddenError 同名：分类器按类名识别 HTTP 403。"""


def test_conversation_key_parses_both_partitions():
    assert parse_conversation_key("c2c:user-1") == ("c2c", "user-1")
    assert parse_conversation_key("group:group-1") == ("group", "group-1")


def test_dirty_conversation_keys_are_rejected():
    for key in ("", "user-1", "c2c:", "channel-1:c2c:user-1", "dm:user-1"):
        assert parse_conversation_key(key) is None, key


def test_proactive_error_classification():
    cases = [
        (ForbiddenError("403"), "permission"),
        (RuntimeError("40034105 主动消息无权限"), "permission"),
        (RuntimeError("40054013 用户拒收消息"), "permission"),
        (RuntimeError("40054004 无好友关系"), "permission"),
        (RuntimeError("40034122 互动召回已达区间上限"), "wakeup_limit"),
        (RuntimeError("40034100 主动消息超频控"), "quota"),
        (RuntimeError("40034006 内容违规"), "content"),
        (asyncio.TimeoutError(), "network"),
    ]
    for exc, expected in cases:
        kind, detail = classify_proactive_error(exc)
        assert kind == expected, (exc, kind)
        assert detail == str(exc).strip()


def test_wakeup_limit_wins_over_the_quota_markers():
    """召回达上限的文案里带「上限」，必须归到 wakeup_limit（退回普通主动），不能当频控跳过。"""
    assert classify_proactive_error(RuntimeError("召回已达上限"))[0] == "wakeup_limit"


def _post_target(calls):
    class Http:
        async def request(self, route, json=None):
            calls.append((route, json))
            return {"id": "msg-1"}

    return SimpleNamespace(api=SimpleNamespace(_http=Http()))


@pytest.mark.asyncio
async def test_post_proactive_private_message_has_a_minimal_payload():
    from bot.qq_manager import _post_proactive

    calls = []
    await _post_proactive(_post_target(calls), ("c2c", "user-1"), "在忙吗", False)

    route, payload = calls[0]
    assert route.method == "POST"
    assert route.path == "/v2/users/{openid}/messages"
    assert route.parameters == {"openid": "user-1"}
    # SDK 的 post_c2c_message 会把 embed/ark/media/msg_id 以 null 一并塞进 JSON
    assert payload == {"msg_type": 0, "content": "在忙吗"}


@pytest.mark.asyncio
async def test_post_proactive_group_message_goes_to_the_group_route():
    from bot.qq_manager import _post_proactive

    calls = []
    await _post_proactive(_post_target(calls), ("group", "group-1"), "在吗", False)

    route, payload = calls[0]
    assert route.path == "/v2/groups/{group_openid}/messages"
    assert route.parameters == {"group_openid": "group-1"}
    assert payload == {"msg_type": 0, "content": "在吗"}


@pytest.mark.asyncio
async def test_post_proactive_wakeup_adds_the_is_wakeup_flag():
    """互动召回只能走单聊路由：群聊没有 is_wakeup 能力。"""
    from bot.qq_manager import _post_proactive

    calls = []
    await _post_proactive(_post_target(calls), ("group", "group-1"), "在吗", True)

    route, payload = calls[0]
    assert route.path == "/v2/users/{openid}/messages"
    assert payload == {"msg_type": 0, "content": "在吗", "is_wakeup": True}


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
