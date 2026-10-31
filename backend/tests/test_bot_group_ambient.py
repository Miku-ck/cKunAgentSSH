"""群聊可参与期与临时上下文（spec 2026-10-30-qq-group-ambient）。

三条线各自单独测：
1. group_ambient 纯状态：窗口、节流、上下文 TTL；
2. QQ 适配层：@ 开窗、非 @ 消息只在窗口内接话、@ 了别人不抢答；
3. 入站管线 + 生成层：环境消息不写用户消息、不记关系、不召回也不落记忆。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import database as db
from bot import chat_service as cs_mod
from bot import group_ambient
from bot import inbound_pipeline as pipeline_mod
from bot.chat_service import BotChatService
from bot.qq_channel import QQInboundAdapter


@pytest.fixture(autouse=True)
def _clean_ambient():
    """窗口是进程内全局状态：用例之间必须互不影响。"""
    group_ambient.reset_for_tests()
    yield
    group_ambient.reset_for_tests()


NOW = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc)


# ── ① 窗口与节流 ──

def test_mention_opens_a_five_minute_window():
    assert not group_ambient.is_open("ch", "g1", now=NOW)
    group_ambient.note_mention("ch", "g1", now=NOW)
    assert group_ambient.is_open("ch", "g1", now=NOW + timedelta(minutes=4))
    assert not group_ambient.is_open("ch", "g1", now=NOW + timedelta(minutes=5))


def test_window_is_per_bot_and_per_group():
    group_ambient.note_mention("ch", "g1", now=NOW)
    assert not group_ambient.is_open("ch", "g2", now=NOW)
    assert not group_ambient.is_open("other", "g1", now=NOW)


def test_answer_requires_an_open_window():
    group_ambient.note_line("ch", "g1", "小明", "有人吗", now=NOW)
    assert not group_ambient.should_reply("ch", "g1", now=NOW), "没被叫过就不插话"


def test_answers_are_throttled_to_one_a_minute():
    group_ambient.note_mention("ch", "g1", now=NOW)
    assert group_ambient.should_reply("ch", "g1", now=NOW)
    group_ambient.mark_spoken("ch", "g1", now=NOW)
    assert not group_ambient.should_reply("ch", "g1", now=NOW + timedelta(seconds=30))
    assert group_ambient.should_reply("ch", "g1", now=NOW + timedelta(seconds=61))


def test_context_drops_lines_older_than_ten_minutes_and_keeps_order():
    group_ambient.note_line("ch", "g1", "小明", "早", now=NOW - timedelta(minutes=11))
    group_ambient.note_line("ch", "g1", "小红", "吃了吗", now=NOW - timedelta(minutes=2))
    group_ambient.note_line("ch", "g1", "小明", "刚吃完", now=NOW)
    assert group_ambient.context_lines("ch", "g1", now=NOW) == [
        ("小红", "吃了吗"), ("小明", "刚吃完"),
    ]


def test_context_is_capped_and_ignores_empty_lines():
    for index in range(30):
        group_ambient.note_line("ch", "g1", "小明", f"第{index}条", now=NOW)
    group_ambient.note_line("ch", "g1", "小明", "   ", now=NOW)
    lines = group_ambient.context_lines("ch", "g1", now=NOW)
    assert len(lines) == group_ambient.CONTEXT_MAX_LINES
    assert lines[-1][1] == "第29条"


def test_nameless_speaker_falls_back_to_group_member():
    group_ambient.note_line("ch", "g1", "", "在吗", now=NOW)
    assert group_ambient.context_lines("ch", "g1", now=NOW) == [("群友", "在吗")]


def test_seen_event_flags_a_re_delivered_event_only_once():
    """接话不落库，没有 answered_at 可用：事件去重得自己做。"""
    assert not group_ambient.seen_event("ch", "g1", "e1")
    assert group_ambient.seen_event("ch", "g1", "e1"), "重投同一条要拦住"
    assert not group_ambient.seen_event("ch", "g1", "e2")
    assert not group_ambient.seen_event("ch", "g2", "e1"), "去重按群分开"
    assert not group_ambient.seen_event("ch", "g1", ""), "没事件 id 就不去重"
    assert not group_ambient.seen_event("other", "g1", "e1"), "也按机器人分开"


def test_seen_event_history_is_bounded():
    for index in range(group_ambient.EVENT_HISTORY + 20):
        group_ambient.seen_event("ch", "g1", f"e{index}")
    assert not group_ambient.seen_event("ch", "g1", "e0"), "只看最近几条，不留长期台账"


# ── ① 'b 群参与档位：接话归档位管 ──

def test_only_the_two_upper_modes_allow_ambient_replies():
    assert not group_ambient.ambient_allowed("mention_only"), "只 @ 才答 = 不插话"
    assert group_ambient.ambient_allowed("ambient")
    assert group_ambient.ambient_allowed("proactive"), "最高档当然也能接话"
    assert not group_ambient.ambient_allowed(""), "脏值按最保守档读"
    assert not group_ambient.ambient_allowed(None)


def test_modes_are_ordered_from_quietest_to_loudest():
    """档位顺序即打扰程度顺序：UI 平铺与判定都按这一份走。"""
    assert group_ambient.GROUP_MODES == ("mention_only", "ambient", "proactive")


# ── ② QQ 适配层 ──


class _FakeMessage:
    def __init__(self, event_id, content, *, group_openid="g1", username="小明", bot=False):
        self.id = event_id
        self.event_id = event_id
        self.content = content
        self.group_openid = group_openid
        self.author = SimpleNamespace(username=username, bot=bot)
        self.replies = []

    async def reply(self, content=None, msg_type=0, msg_seq=None, media=None):
        self.replies.append(content)


def _adapter():
    received = []

    async def on_inbound(inbound):
        received.append(inbound)

    return QQInboundAdapter("ch-1", on_inbound), received


async def _settle():
    """接话走 create_task：让出两次控制权，等任务跑到 on_inbound。"""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_mention_opens_the_window_and_answers_normally():
    adapter, received = _adapter()
    await adapter.handle_group(_FakeMessage("e1", "<@!bot> 在吗"))

    assert group_ambient.is_open("ch-1", "g1")
    assert received[0].ambient is False, "@ 仍是正常回复：要幂等、要记账"


@pytest.mark.asyncio
async def test_non_at_message_is_only_a_context_line_without_a_window():
    adapter, received = _adapter()
    await adapter.handle_group_message(_FakeMessage("e1", "有人吗"))
    await _settle()

    assert group_ambient.context_lines("ch-1", "g1") == [("小明", "有人吗")]
    assert received == [], "没被 @ 过就不该开口"


@pytest.mark.asyncio
async def test_a_message_mentioning_someone_else_is_not_answered():
    adapter, received = _adapter()
    group_ambient.note_mention("ch-1", "g1")
    await adapter.handle_group_message(_FakeMessage("e1", "<@123456> 你来说"))
    await _settle()

    assert received == [], "@ 了别人说明这对话与它无关；@ 自己那条由 AT 事件答"


@pytest.mark.asyncio
async def test_open_window_emits_an_ambient_inbound_and_throttles_the_next_one():
    adapter, received = _adapter()
    group_ambient.note_mention("ch-1", "g1")

    await adapter.handle_group_message(_FakeMessage("e1", "那今晚吃啥"))
    await _settle()
    assert [item.ambient for item in received] == [True]
    assert received[0].conversation_key == "group:g1"

    await adapter.handle_group_message(_FakeMessage("e2", "随便"))
    await _settle()
    assert len(received) == 1, "60s 节流：不能每句都接"


@pytest.mark.asyncio
async def test_empty_group_message_is_dropped():
    adapter, received = _adapter()
    group_ambient.note_mention("ch-1", "g1")
    await adapter.handle_group_message(_FakeMessage("e1", "   "))
    await _settle()

    assert received == []
    assert group_ambient.context_lines("ch-1", "g1") == []


@pytest.mark.asyncio
async def test_the_bots_own_group_message_is_ignored():
    """自己的发言被全量事件回推：不进上下文也不接话，否则能自说自话好几句。"""
    adapter, received = _adapter()
    group_ambient.note_mention("ch-1", "g1")
    await adapter.handle_group_message(_FakeMessage("e1", "我在呢", bot=True))
    await _settle()

    assert received == []
    assert group_ambient.context_lines("ch-1", "g1") == []


@pytest.mark.asyncio
async def test_a_re_delivered_event_is_not_answered_again():
    """节流计时拨回之后，同一条事件重投仍不能再接一次：幂等不能只靠节流。"""
    adapter, received = _adapter()
    group_ambient.note_mention("ch-1", "g1")
    await adapter.handle_group_message(_FakeMessage("e1", "那今晚吃啥"))
    await _settle()
    assert len(received) == 1

    group_ambient.mark_spoken("ch-1", "g1", now=datetime.now(timezone.utc) - timedelta(seconds=120))
    await adapter.handle_group_message(_FakeMessage("e1", "那今晚吃啥"))
    await _settle()
    assert len(received) == 1, "重投的那条不能再答，也不能重复进上下文"
    assert group_ambient.context_lines("ch-1", "g1").count(("小明", "那今晚吃啥")) == 1

    await adapter.handle_group_message(_FakeMessage("e2", "就火锅"))
    await _settle()
    assert [item.text for item in received] == ["那今晚吃啥", "就火锅"], "新事件照常接"


@pytest.mark.asyncio
async def test_an_at_answer_also_starts_the_reply_gap():
    """@ 刚答完 3 秒的下一条不许立刻插话：那一句本身就是一次开口。"""
    adapter, received = _adapter()
    await adapter.handle_group(_FakeMessage("e1", "<@!bot> 在吗"))
    await adapter.handle_group_message(_FakeMessage("e2", "就是问你吃啥"))
    await _settle()

    assert [item.ambient for item in received] == [False], "只有 @ 那一条的回复，没有接话"
    assert group_ambient.is_open("ch-1", "g1"), "但窗口仍开着：只是节流了"


# ── ③ 入站管线：环境接话不写用户消息 ──


class _Provider:
    name = "qq"
    reply_window_s = 5.0

    def outbound_parts(self, text):
        return [text]

    def fallback(self, kind):  # noqa: ARG002 —— 接话不用兜底文案
        return "……"


class _Inbound:
    conversation_key = "group:g1"
    event_id = "e1"
    text = "随便"
    ambient = True

    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.parts = []

    async def send(self, part):
        self.parts.append(part)


async def _no_sleep(_seconds):
    return None


def _setup_group(group_mode="ambient"):
    """人格 + 已配置的机器人：bot_conversations.channel_id 有外键，不能瞎填。

    默认给「@ 后可接话」：这些用例要跑的是接话本身的行为，档位闸另开用例测。
    """
    persona = db.create_bot_persona(name="晚风")
    db.update_bot_persona(persona["id"], group_mode=group_mode)
    channel = db.create_bot_channel(
        provider="qq",
        display_name="主号",
        persona_id=persona["id"],
        app_id="app-1",
        app_secret="secret",
    )
    return persona, channel["id"]


class _Chat:
    def __init__(self, reply="火锅吧", *, fail=None, delay=0.0):
        self.reply = reply
        self.fail = fail
        self.delay = delay
        self.seen = {}

    async def ambient_reply(self, conversation_id, context, *, channel="qq"):
        self.seen["conversation_id"] = conversation_id
        self.seen["context"] = context
        self.seen["channel"] = channel
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return self.reply

    async def complete_reply(self, *args, **kwargs):  # pragma: no cover - 反例守卫
        raise AssertionError("环境消息不能走正常回复链路")


@pytest.mark.asyncio
async def test_ambient_inbound_answers_with_context_and_leaves_no_user_message(base_db):
    persona, channel_id = _setup_group()
    group_ambient.note_mention(channel_id, "g1")
    group_ambient.note_line(channel_id, "g1", "小明", "今晚吃啥")

    chat = _Chat()
    inbound = _Inbound(channel_id)
    pipeline = pipeline_mod.InboundPipeline(chat, sleep=_no_sleep)
    await pipeline.handle(inbound, _Provider(), persona["id"])

    assert chat.seen["context"] == [("小明", "今晚吃啥")]
    assert chat.seen["channel"] == "qq"
    assert inbound.parts == ["火锅吧"]

    conversation = db.get_or_create_bot_conversation(
        persona["id"], "qq", "group:g1", channel_id
    )
    rows = db.get_recent_bot_messages(conversation["id"], 10)
    assert rows == [], "环境消息不写用户消息（会话内没有落库的 user 行）"
    # 自己的发言进临时上下文：下一句才知道刚才说过什么
    assert group_ambient.context_lines(channel_id, "g1")[-1] == ("晚风", "火锅吧")


@pytest.mark.asyncio
async def test_ambient_inbound_dropped_when_the_persona_only_answers_at_mentions(base_db):
    """档位在管线这一层兜住：渠道适配层不知道人格配置，也不该知道。"""
    persona, channel_id = _setup_group(group_mode="mention_only")
    group_ambient.note_mention(channel_id, "g1")
    group_ambient.note_line(channel_id, "g1", "小明", "今晚吃啥")
    chat = _Chat()
    inbound = _Inbound(channel_id)
    pipeline = pipeline_mod.InboundPipeline(chat, sleep=_no_sleep)
    await pipeline.handle(inbound, _Provider(), persona["id"])

    assert chat.seen == {}, "不接话就不该碰模型"
    assert inbound.parts == []


@pytest.mark.asyncio
async def test_ambient_generation_failure_says_nothing(base_db):
    persona, channel_id = _setup_group()
    group_ambient.note_mention(channel_id, "g1")
    inbound = _Inbound(channel_id)
    pipeline = pipeline_mod.InboundPipeline(
        _Chat(fail=RuntimeError("模型炸了")), sleep=_no_sleep
    )
    await pipeline.handle(inbound, _Provider(), persona["id"])

    assert inbound.parts == [], "群里不需要报错：接不上就不说话"


@pytest.mark.asyncio
async def test_ambient_generation_timeout_drops_the_turn(base_db):
    persona, channel_id = _setup_group()
    group_ambient.note_mention(channel_id, "g1")
    inbound = _Inbound(channel_id)
    pipeline = pipeline_mod.InboundPipeline(
        _Chat(delay=0.05), sleep=_no_sleep
    )

    class SlowProvider(_Provider):
        reply_window_s = 0.001

    await pipeline.handle(inbound, SlowProvider(), persona["id"])

    assert inbound.parts == [], "超时也不拿兜底文案往群里刷"


@pytest.mark.asyncio
async def test_ambient_budget_is_capped_below_the_platform_window(base_db, monkeypatch):
    """接话预算不跟平台被动窗口走：一句可有可无的话不许在会话锁里占满 150s。"""
    persona, channel_id = _setup_group()
    group_ambient.note_mention(channel_id, "g1")
    monkeypatch.setattr(pipeline_mod, "AMBIENT_REPLY_BUDGET_S", 0.01)
    inbound = _Inbound(channel_id)
    pipeline = pipeline_mod.InboundPipeline(_Chat(delay=0.05), sleep=_no_sleep)

    class WideProvider(_Provider):
        reply_window_s = 150.0

    await pipeline.handle(inbound, WideProvider(), persona["id"])

    assert inbound.parts == []


@pytest.mark.asyncio
async def test_ambient_yields_while_the_conversation_is_answering(base_db):
    """正在答 @ 时接话直接作废，不许在锁上排队：排队会把 @ 的回复挤出被动窗口。"""
    persona, channel_id = _setup_group()
    group_ambient.note_mention(channel_id, "g1")
    chat = _Chat()
    pipeline = pipeline_mod.InboundPipeline(chat, sleep=_no_sleep)
    lock = pipeline._locks.setdefault((channel_id, "group:g1"), asyncio.Lock())
    await lock.acquire()
    try:
        await pipeline.handle(_Inbound(channel_id), _Provider(), persona["id"])
    finally:
        lock.release()

    assert chat.seen == {}, "锁被占着就一句都不生成"


# ── ④ 生成层：只落机器人自己那句 ──


class _Chunk:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning = reasoning


def _stub_ambient(monkeypatch, chunks, seen):
    async def fake_stream(**kw):
        seen["messages"] = kw["messages"]
        seen["params"] = kw
        for text in chunks:
            yield _Chunk(content=text)

    def fail(*args, **kwargs):  # pragma: no cover - 反例守卫
        raise AssertionError("环境消息不该产生记账副作用")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    monkeypatch.setattr(
        BotChatService, "_model_for_persona",
        lambda self, p: {"base_url": "u", "api_key": "k", "model_id": "m",
                         "max_tokens": 8192, "max_output_tokens": 4096,
                         "temperature": 0.7, "top_p": 0.9},
    )
    monkeypatch.setattr(
        cs_mod.db, "get_bot_conversation",
        lambda cid: {"external_id": "group:g1", "persona_id": "p1",
                     "persona": {"name": "晚风", "reply_style": "moderate",
                                 "enable_thinking": 0, "personality": "",
                                 "background": ""}},
    )
    # 只读分支也要跑到：没 persona_id 的话下面那堆「不写」从来没机会被验证
    monkeypatch.setattr(
        cs_mod.db, "get_bot_relation",
        lambda pid: seen.setdefault("reads", []).append("relation") or {},
    )
    monkeypatch.setattr(
        cs_mod.relation_mod, "snapshot",
        lambda pid, speed: seen.setdefault("reads", []).append("snapshot")
        or {"stage": "朋友", "score": 50},
    )
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", lambda cid, n: [])
    monkeypatch.setattr(cs_mod.db, "add_bot_message", lambda *a, **k: seen.setdefault(
        "persisted", []
    ).append((a[1], a[2])))
    monkeypatch.setattr(cs_mod.relation_mod, "record_turn", fail)
    monkeypatch.setattr(cs_mod.memory_mod, "remember_async", fail)
    monkeypatch.setattr(cs_mod.memory_mod, "recall", fail)
    monkeypatch.setattr(cs_mod.state_mod, "update_for_turn", fail)


@pytest.mark.asyncio
async def test_ambient_reply_persists_only_its_own_line(monkeypatch):
    seen: dict = {}
    _stub_ambient(monkeypatch, ["火锅吧"], seen)

    text = await BotChatService().ambient_reply(
        "c1", [("小明", "今晚吃啥"), ("小红", "随便")], channel="qq"
    )

    assert text == "火锅吧"
    assert seen["persisted"] == [("assistant", "火锅吧")], "只有机器人自己那句进记录"
    assert seen["params"]["disable_thinking"] is True
    assert seen["params"]["enable_thinking"] is False


@pytest.mark.asyncio
async def test_ambient_reply_feeds_the_scene_as_a_system_note(monkeypatch):
    seen: dict = {}
    _stub_ambient(monkeypatch, ["火锅吧"], seen)

    await BotChatService().ambient_reply("c1", [("小明", "今晚吃啥")], channel="qq")

    messages = seen["messages"]
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "今晚吃啥" in system
    assert "小明" in system
    # 现场是 system 而不是 user：不能以「某人说了这句话」的身份进历史
    assert all("今晚吃啥" not in str(m.get("content")) for m in messages if m["role"] == "user")
    assert messages[-1] == {"role": "user", "content": cs_mod._AMBIENT_ASK}


@pytest.mark.asyncio
async def test_ambient_reply_without_context_still_speaks(monkeypatch):
    seen: dict = {}
    _stub_ambient(monkeypatch, ["嗯？"], seen)

    text = await BotChatService().ambient_reply("c1", [], channel="qq")

    assert text == "嗯？"
    messages = seen["messages"]
    assert len([m for m in messages if m["role"] == "system"]) == 1, "没有现场就不加空 note"


@pytest.mark.asyncio
async def test_ambient_reply_raises_when_the_model_says_nothing(monkeypatch):
    seen: dict = {}
    _stub_ambient(monkeypatch, [], seen)

    with pytest.raises(RuntimeError):
        await BotChatService().ambient_reply("c1", [], channel="qq")
    assert "persisted" not in seen


@pytest.mark.asyncio
async def test_ambient_reply_reads_relationship_but_books_nothing(monkeypatch):
    """分寸要读（该用什么口气说话），账不记（这不是一轮对话）。"""
    seen: dict = {}
    _stub_ambient(monkeypatch, ["火锅吧"], seen)

    await BotChatService().ambient_reply("c1", [("小明", "今晚吃啥")], channel="qq")

    assert seen["reads"] == ["snapshot", "relation"], "只读分支必须真跑到"
    assert seen["persisted"] == [("assistant", "火锅吧")], "读了关系也不记账"
