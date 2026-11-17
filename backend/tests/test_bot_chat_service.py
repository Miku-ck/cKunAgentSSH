import pytest

import bot.chat_service as cs_mod
from bot.chat_service import BotChatService


class _Chunk:
    def __init__(self, content=None, reasoning=None, finish_reason=""):
        self.content, self.reasoning = content, reasoning
        self.finish_reason = finish_reason


@pytest.mark.asyncio
async def test_stream_reply_yields_reasoning_and_content_and_persists(monkeypatch):
    async def fake_stream(**kw):
        assert kw["enable_thinking"] is True  # persona.enable_thinking 驱动，不看 supports_thinking
        for c in [_Chunk(reasoning="想"), _Chunk(content="好")]:
            yield c

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    persisted = {}

    def fake_add(conv, role, content, external_msg_id=None, reasoning=None, images=None):
        if role == "assistant":
            persisted["content"], persisted["reasoning"] = content, reasoning

    monkeypatch.setattr(
        cs_mod.db, "get_bot_conversation",
        lambda cid: {"persona": {"name": "n", "reply_style": "moderate",
                                 "enable_thinking": 1, "personality": "", "background": ""}},
    )
    monkeypatch.setattr(
        BotChatService, "_model_for_persona",
        lambda self, p: {"base_url": "u", "api_key": "k", "model_id": "m",
                         "max_tokens": 8192, "max_output_tokens": 4096,
                         "reasoning_tokens": 1024, "temperature": 0.7, "top_p": 0.9},
    )
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", lambda cid, n: [])
    monkeypatch.setattr(cs_mod.db, "add_bot_message", fake_add)
    out = [x async for x in BotChatService().stream_reply("c1", "在吗", channel="local")]
    assert ("reasoning", "想") in out and ("content", "好") in out
    assert persisted == {"content": "好", "reasoning": "想"}


@pytest.mark.asyncio
async def test_complete_reply_only_content(monkeypatch):
    async def fake_stream(**kw):
        yield _Chunk(reasoning="忽略我")
        yield _Chunk(content="正文")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    svc = BotChatService()
    monkeypatch.setattr(
        svc, "_model_for_persona",
        lambda p: {"base_url": "u", "api_key": "k", "model_id": "m",
                   "max_tokens": 8192, "max_output_tokens": 4096,
                   "reasoning_tokens": 1024, "temperature": 0.7, "top_p": 0.9},
    )
    monkeypatch.setattr(
        cs_mod.db, "get_bot_conversation",
        lambda cid: {"persona": {"name": "n", "reply_style": "moderate",
                                 "enable_thinking": 1, "personality": "", "background": ""}},
    )
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", lambda cid, n: [])
    monkeypatch.setattr(cs_mod.db, "add_bot_message", lambda *a, **k: {})
    text = await svc.complete_reply("c1", "在吗", channel="qq")
    assert text == "正文"


@pytest.mark.asyncio
async def test_memory_recall_is_fenced_for_group_conversations(base_db, monkeypatch):
    """群会话的记忆召回必须带 group=True。

    `audience` 是这条链上唯一的「此刻有没有旁人在听」判定，也是最容易接错的一根线：
    接错之后召回本身照样返回得像没事发生。这里只钉接线，筛得对不对由
    test_bot_memory 里的召回用例覆盖。
    """
    import database as db

    persona = db.create_bot_persona(name="晚风")
    group = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g-1"
    )
    private = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="c2c:user-1"
    )
    local = db.create_bot_conversation(persona_id=persona["id"], channel="local")

    seen: list = []

    async def spy_recall(persona_id, query="", **kw):
        seen.append(bool(kw.get("group")))
        return []

    async def fake_stream(**kw):
        yield _Chunk(content="好")

    monkeypatch.setattr(cs_mod.memory_mod, "recall", spy_recall)
    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    monkeypatch.setattr(
        BotChatService, "_model_for_persona",
        lambda self, p: {"base_url": "u", "api_key": "k", "model_id": "m",
                         "max_tokens": 8192, "max_output_tokens": 4096,
                         "reasoning_tokens": 1024, "temperature": 0.7, "top_p": 0.9},
    )

    svc = BotChatService()
    await svc.complete_reply(group["id"], "在吗", channel="qq")
    await svc.complete_reply(private["id"], "在吗", channel="qq")
    await svc.complete_reply(local["id"], "在吗", channel="local")

    assert seen == [True, False, False], "只有群会话进隔离档：私聊与本地照旧是「她记得你」"


@pytest.mark.asyncio
async def test_thinking_off_drops_reasoning_and_asks_upstream_to_stop(monkeypatch):
    """人格没勾思考：思考链不显示、不落库，且要明确让上游别思考。

    Qwen3 系 chat template 的 enable_thinking 默认是 True —— 只做到「不显示」等于让
    模型白烧思考 token（首字变慢），所以这里同时钉住 disable_thinking=True 的下发。
    """
    async def fake_stream(**kw):
        assert kw["enable_thinking"] is False
        assert kw["disable_thinking"] is True
        for c in [_Chunk(reasoning="我在想什么"), _Chunk(content="正文")]:
            yield c

    persisted = {}

    def fake_add(conv, role, content, external_msg_id=None, reasoning=None, images=None):
        if role == "assistant":
            persisted["content"], persisted["reasoning"] = content, reasoning

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    monkeypatch.setattr(
        BotChatService, "_model_for_persona",
        lambda self, p: {"base_url": "u", "api_key": "k", "model_id": "m",
                         "max_tokens": 8192, "max_output_tokens": 4096,
                         "reasoning_tokens": 1024, "temperature": 0.7, "top_p": 0.9},
    )
    monkeypatch.setattr(
        cs_mod.db, "get_bot_conversation",
        lambda cid: {"persona": {"name": "n", "reply_style": "moderate",
                                 "enable_thinking": 0, "personality": "", "background": ""}},
    )
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", lambda cid, n: [])
    monkeypatch.setattr(cs_mod.db, "add_bot_message", fake_add)
    out = [x async for x in BotChatService().stream_reply("c1", "在吗", channel="local")]
    assert out == [("content", "正文")]
    assert persisted == {"content": "正文", "reasoning": None}


def _stub_delivery(monkeypatch, external_id: str, chunks: list[str], seen: dict) -> None:
    """把一轮生成接到外部会话上：记下提示词、落库文本，回读按落库结果返回。"""

    async def fake_stream(**kw):
        seen["prompt"] = "\n".join(
            str(m.get("content")) for m in kw["messages"] if m.get("role") == "system"
        )
        for text in chunks:
            yield _Chunk(content=text)

    def fake_add(conv, role, content, external_msg_id=None, reasoning=None, images=None):
        if role == "assistant":
            seen["content"] = content

    def fake_recent(cid, limit):
        if limit == 1 and "content" in seen:
            return [{"role": "assistant", "content": seen["content"]}]
        return []

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    monkeypatch.setattr(
        BotChatService, "_model_for_persona",
        lambda self, p: {"base_url": "u", "api_key": "k", "model_id": "m",
                         "max_tokens": 8192, "max_output_tokens": 4096,
                         "reasoning_tokens": 1024, "temperature": 0.7, "top_p": 0.9},
    )
    monkeypatch.setattr(
        cs_mod.db, "get_bot_conversation",
        lambda cid: {"external_id": external_id,
                     "persona": {"name": "n", "reply_style": "moderate",
                                 "enable_thinking": 0, "personality": "", "background": ""}},
    )
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", fake_recent)
    monkeypatch.setattr(cs_mod.db, "add_bot_message", fake_add)


@pytest.mark.asyncio
async def test_group_reply_is_shaped_before_persist_and_that_is_what_gets_sent(monkeypatch):
    """群聊里「上半句的下半句」并回第一条；发出去的就是落库的那份文本。"""
    seen: dict = {}
    _stub_delivery(monkeypatch, "group:g1", [
        "今晚八点开一把，来不来都得先说一声。",
        "\n\n别又到点了才冒一句不打了，我饭都做好了。",
    ], seen)

    text = await BotChatService().complete_reply("c1", "在吗", channel="qq")

    assert "\n\n" not in seen["content"]
    assert text == seen["content"]
    assert "群里" in seen["prompt"]


@pytest.mark.asyncio
async def test_private_chat_keeps_two_beats(monkeypatch):
    """私聊不设受众门槛：两拍本来就是活的节奏。"""
    seen: dict = {}
    _stub_delivery(monkeypatch, "c2c:u1", [
        "今晚八点开一把，来不来都得先说一声。",
        "\n\n别又到点了才冒一句不打了，我饭都做好了。",
    ], seen)

    text = await BotChatService().complete_reply("c1", "在吗", channel="qq")

    assert text.count("\n\n") == 1
    assert "群里" not in seen["prompt"]


# ── 第一批优化（借鉴 dsh-pet）的钉子测试 ──

def test_get_recent_bot_messages_carries_created_at(base_db):
    """P0-1：历史查询必须带出 created_at。

    曾漏掉这一列，chat_service 读到的 user_created_at 全是 None，
    状态层的 burst 判定（60s 内 ≥3 条）在真实链路上永远不触发。
    单测之前只注入时间戳给 turn_events，所以没暴露这个断链。
    """
    import database as db

    persona = db.create_bot_persona(name="晚风")
    conv = db.create_bot_conversation(persona_id=persona["id"], channel="local")
    for i in range(3):
        db.add_bot_message(conv["id"], "user", f"msg-{i}")
    rows = db.get_recent_bot_messages(conv["id"], 10)
    assert len(rows) == 3
    for row in rows:
        assert row.get("created_at"), f"历史条目缺 created_at: {row}"
        # 格式必须能被 bot.timeutil.parse_ts 解析（state 层同款口径）
        from bot.timeutil import parse_ts
        assert parse_ts(row["created_at"]) is not None


def test_history_time_anchor_only_on_user_after_gap():
    """P1-1：时间前缀只标 user、且间隔 ≥30min 才标；assistant 永不标。"""
    from datetime import datetime, timedelta, timezone
    from bot.timeutil import fmt_ts

    t0 = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
    svc = BotChatService()

    # 间隔 5 分钟：不标
    near = svc._to_llm_message(
        {"role": "user", "content": "在吗", "created_at": fmt_ts(t0 + timedelta(minutes=5))},
        prev_ts=t0,
    )
    assert near["content"] == "在吗"

    # 间隔 31 分钟：标（格式 [MM-DD HH:MM]）
    far = svc._to_llm_message(
        {"role": "user", "content": "还在吗", "created_at": fmt_ts(t0 + timedelta(minutes=31))},
        prev_ts=t0,
    )
    assert far["content"].endswith("还在吗")
    assert far["content"].startswith("[")

    # assistant 即便隔了很久也不标（防模型照抄时间戳）
    assist = svc._to_llm_message(
        {"role": "assistant", "content": "在的", "created_at": fmt_ts(t0 + timedelta(hours=3))},
        prev_ts=t0,
    )
    assert assist["content"] == "在的"


@pytest.mark.asyncio
async def test_memory_recall_times_out_without_blocking(monkeypatch):
    """P1-2：召回卡住时 1.5s 硬超时降级为空，不拖垮首字。"""
    import asyncio
    import time

    async def stuck_recall(*a, **kw):
        await asyncio.sleep(60)

    monkeypatch.setattr(cs_mod.memory_mod, "recall", stuck_recall)
    t0 = time.monotonic()
    note = await BotChatService._memory_note("p1", "在吗")
    elapsed = time.monotonic() - t0

    assert note == ""
    assert elapsed < 3.0, f"召回超时保护失效，耗时 {elapsed:.1f}s"


# ── P1-3：无正文时按 finish_reason 归一提示 ──

@pytest.mark.parametrize("reason,fragment", [
    ("length", "超长"),
    ("content_filter", "不太方便接"),
    ("abort", "被中断"),
    ("aborted", "被中断"),
    ("stop", "没生成出内容"),          # 未知/正常终止但无正文 → 通用兜底
    ("", "没生成出内容"),
    ("unknown_future_reason", "没生成出内容"),
])
def test_no_content_error_maps_finish_reason(reason, fragment):
    """归一函数把每种终止原因翻译成人话，未知原因回落通用提示。"""
    err = BotChatService._no_content_error(reason)
    assert isinstance(err, RuntimeError)
    assert fragment in str(err)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,fragment", [
    ("length", "超长"),
    ("content_filter", "不太方便接"),
    ("abort", "被中断"),
])
async def test_stream_reply_no_content_carries_finish_reason(monkeypatch, reason, fragment):
    """端到端：finish_reason 从 chunk 流透传到无正文异常文案。

    这条钉子测试锁住的是"llm_adapter 已透传 finish_reason"这个前提——
    若上游哪天不再透传，这里会红。异常类型仍是 RuntimeError，上游兜底契约不变。
    """
    async def fake_stream(**kw):
        yield _Chunk(content="", finish_reason=reason)

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    monkeypatch.setattr(
        cs_mod.db, "get_bot_conversation",
        lambda cid: {"persona": {"name": "n", "reply_style": "moderate",
                                 "enable_thinking": 0, "personality": "", "background": ""}},
    )
    monkeypatch.setattr(
        BotChatService, "_model_for_persona",
        lambda self, p: {"base_url": "u", "api_key": "k", "model_id": "m",
                         "max_tokens": 8192, "temperature": 0.7, "top_p": 0.9},
    )
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", lambda cid, n: [])
    monkeypatch.setattr(cs_mod.db, "add_bot_message", lambda *a, **kw: None)
    monkeypatch.setattr(cs_mod.memory_mod, "embed_client_for", lambda m: None)

    with pytest.raises(RuntimeError) as ei:
        [x async for x in BotChatService().stream_reply("c1", "在吗", channel="local")]
    assert fragment in str(ei.value)
