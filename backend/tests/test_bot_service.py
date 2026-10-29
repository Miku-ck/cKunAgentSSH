import os
from types import SimpleNamespace

import pytest

import database as db


@pytest.fixture
def bot_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-service.db"))
    model = db.create_model_config(
        name="陪聊模型",
        base_url="http://model.test/v1",
        api_key="secret",
        model_id="chat-v1",
        temperature=0.8,
        max_output_tokens=1024,
        top_p=0.95,
    )
    persona = db.create_bot_persona(
        name="小暖",
        personality="温和、幽默",
        background="喜欢听用户讲日常",
        model_id=model["id"],
    )
    conversation = db.create_bot_conversation(persona["id"])
    yield conversation
    db._close_all_connections()


@pytest.mark.asyncio
async def test_stream_reply_uses_persona_history_and_saves_messages(bot_db, monkeypatch):
    from bot.chat_service import BotChatService

    db.add_bot_message(bot_db["id"], "user", "我叫小林")
    db.add_bot_message(bot_db["id"], "assistant", "很高兴认识你")
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(content="记得你叫", reasoning="")
        yield SimpleNamespace(content="小林", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    service = BotChatService()
    chunks = [chunk async for chunk in service.stream_reply(bot_db["id"], "我今天有点累")]

    assert chunks == [("content", "记得你叫"), ("content", "小林")]
    assert captured["model"] == "chat-v1"
    assert captured["api_key"] == "secret"
    assert captured["temperature"] == 0.8
    assert captured["max_tokens"] == 1024
    assert captured["top_p"] == 0.95
    assert captured["tools"] is None
    assert "小暖" in captured["messages"][0]["content"]
    assert "温和、幽默" in captured["messages"][0]["content"]
    assert [m["content"] for m in captured["messages"][1:]] == [
        "我叫小林", "很高兴认识你", "我今天有点累"
    ]
    assert [m["content"] for m in db.get_bot_messages(bot_db["id"])][-2:] == [
        "我今天有点累", "记得你叫小林"
    ]


@pytest.mark.asyncio
async def test_stream_reply_caps_output_when_budget_reaches_context_window(
    tmp_workspace, monkeypatch
):
    """输出预算选到 262144（= 窗口）时不能原样下发。

    实际故障：窗口 262144 + 输出预算 262144 → vLLM 判定 prompt 无剩余空间，
    直接 400（"requested 262144 output tokens"）。陪聊与 routes/agent 同口径，
    由 safe_max_tokens 把贴近窗口的误配降到窗口的 1/4。
    """
    from bot.chat_service import BotChatService

    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-budget.db"))
    model = db.create_model_config(
        name="大窗口模型",
        base_url="http://model.test/v1",
        model_id="chat-256k",
        max_tokens=262144,
        max_output_tokens=262144,
    )
    persona = db.create_bot_persona(name="小暖", model_id=model["id"])
    conversation = db.create_bot_conversation(persona["id"])
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(content="在", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    try:
        [chunk async for chunk in BotChatService().stream_reply(conversation["id"], "在吗")]
    finally:
        db._close_all_connections()

    assert captured["max_tokens"] == 65536


@pytest.mark.asyncio
async def test_complete_reply_returns_full_text_for_qq(bot_db, monkeypatch):
    from bot.chat_service import BotChatService

    async def fake_stream(**_kwargs):
        yield SimpleNamespace(content="抱一抱", reasoning="")
        yield SimpleNamespace(content="，我在。", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    reply = await BotChatService().complete_reply(bot_db["id"], "求安慰")

    assert reply == "抱一抱，我在。"
    assert db.get_bot_messages(bot_db["id"])[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_model_failure_does_not_save_partial_assistant(bot_db, monkeypatch):
    from bot.chat_service import BotChatService

    async def failing_stream(**_kwargs):
        yield SimpleNamespace(content="只说一半", reasoning="")
        raise RuntimeError("upstream failed")

    monkeypatch.setattr("llm_adapter.chat_stream", failing_stream)
    with pytest.raises(RuntimeError, match="upstream failed"):
        async for _ in BotChatService().stream_reply(bot_db["id"], "你好"):
            pass

    messages = db.get_bot_messages(bot_db["id"])
    assert [m["role"] for m in messages] == ["user"]
    assert messages[0]["content"] == "你好"


@pytest.mark.asyncio
async def test_retry_after_failure_feeds_user_message_once(bot_db, monkeypatch):
    """QQ 重投同一事件时，库里已存的用户消息不能被再追加一次。"""
    from bot.chat_service import BotChatService

    captured = {}

    async def failing_stream(**_kwargs):
        raise RuntimeError("upstream failed")
        yield

    async def fake_stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield SimpleNamespace(content="在的", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", failing_stream)
    with pytest.raises(RuntimeError):
        await BotChatService().complete_reply(bot_db["id"], "在吗", external_msg_id="event-1")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    await BotChatService().complete_reply(bot_db["id"], "在吗", external_msg_id="event-1")

    assert [m["content"] for m in captured["messages"][1:]] == ["在吗"]


def test_system_prompt_style_and_channel():
    from bot.chat_service import BotChatService
    svc = BotChatService()
    base = {"name": "小暖", "personality": "温和", "background": "b", "reply_style": "concise"}
    concise = svc._system_prompt(base, "local")
    assert "一两句话" in concise and "Markdown 排版" in concise
    longp = svc._system_prompt({**base, "reply_style": "long"}, "local")
    assert "分点" in longp
    qq = svc._system_prompt(base, "qq")
    assert "禁止表格" in qq and "Markdown 排版" not in qq
    # 向后兼容：不传 channel 默认 local
    assert svc._system_prompt(base) == concise


@pytest.mark.asyncio
async def test_stream_reply_style_output_budget(monkeypatch, tmp_workspace):
    """回复风格直接决定输出预算上限（spec §4.2）：concise 压到 512、moderate 走模型默认、long 放大到 4 倍。"""
    from bot.chat_service import BotChatService

    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-style.db"))
    model = db.create_model_config(
        name="m", base_url="http://x/v1", model_id="m", max_output_tokens=1024
    )
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(content="好", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    try:
        # moderate（默认）= 模型 max_output_tokens
        c_mod = db.create_bot_conversation(db.create_bot_persona(name="n", model_id=model["id"])["id"])
        [x async for x in BotChatService().stream_reply(c_mod["id"], "展开讲讲")]
        assert captured["max_tokens"] == 1024
        captured.clear()
        # long = min(max_tokens or 4096, max_output_tokens*4)
        p_long = db.create_bot_persona(name="n", model_id=model["id"], reply_style="long")
        c_long = db.create_bot_conversation(p_long["id"])
        [x async for x in BotChatService().stream_reply(c_long["id"], "再展开")]
        assert captured["max_tokens"] == 4096
        captured.clear()
        # concise 压到 512
        p_concise = db.create_bot_persona(name="n", model_id=model["id"], reply_style="concise")
        c_concise = db.create_bot_conversation(p_concise["id"])
        [x async for x in BotChatService().stream_reply(c_concise["id"], "简短")]
        assert captured["max_tokens"] == 512
    finally:
        db._close_all_connections()


def test_add_and_read_bot_message_images(bot_db):
    imgs = ["data:image/webp;base64,AAAA", "data:image/png;base64,BBBB"]
    saved = db.add_bot_message(bot_db["id"], "user", "看这张", None, None, imgs)
    assert saved["images"] == imgs
    recent = db.get_recent_bot_messages(bot_db["id"], 10)
    assert recent[-1]["images"] == imgs
    listed = db.get_bot_messages(bot_db["id"], 10)
    assert listed[-1]["images"] == imgs


def test_bot_message_images_default_empty_list(bot_db):
    saved = db.add_bot_message(bot_db["id"], "user", "纯文本")
    assert saved["images"] == []
    assert db.get_recent_bot_messages(bot_db["id"], 10)[-1]["images"] == []
    assert db.get_bot_messages(bot_db["id"], 10)[-1]["images"] == []


def test_bot_messages_images_column_present(bot_db):
    # CREATE TABLE + 迁移闸门都应保证列存在（bot_db fixture 已跑 init_db）
    conn = db._get_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_messages)").fetchall()}
    finally:
        conn.close()
    assert "images" in cols


@pytest.mark.asyncio
async def test_stream_reply_builds_multimodal_for_user_images(bot_db, monkeypatch):
    from bot.chat_service import BotChatService

    captured = {}

    async def fake_stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield SimpleNamespace(content="一只猫", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    imgs = ["data:image/webp;base64,AAAA"]
    [c async for c in BotChatService().stream_reply(bot_db["id"], "这是什么", images=imgs)]

    user_msgs = [m for m in captured["messages"][1:] if m["role"] == "user"]
    assert user_msgs[-1]["content"] == [
        {"type": "text", "text": "这是什么"},
        {"type": "image_url", "image_url": {"url": "data:image/webp;base64,AAAA"}},
    ]
    # 落库带图：用户消息取回带 images（助手消息不带）
    stored = db.get_recent_bot_messages(bot_db["id"], 10)
    assert [m for m in stored if m["role"] == "user"][-1]["images"] == imgs
    assert stored[-1]["images"] == []  # 最后一条是助手回复，不回喂图


@pytest.mark.asyncio
async def test_qq_channel_ignores_images(bot_db, monkeypatch):
    from bot.chat_service import BotChatService

    captured = {}

    async def fake_stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield SimpleNamespace(content="在", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    imgs = ["data:image/webp;base64,AAAA"]
    # 即使带图，channel=qq 也必须忽略（回归安全）
    [c async for c in BotChatService().stream_reply(bot_db["id"], "在吗", channel="qq", images=imgs)]

    for m in captured["messages"][1:]:
        assert isinstance(m["content"], str)  # QQ 面永不构造数组
    assert db.get_recent_bot_messages(bot_db["id"], 10)[-1]["images"] == []


# ── 长期记忆接入 ──


@pytest.mark.asyncio
async def test_memory_block_is_injected_as_its_own_system_message(bot_db, monkeypatch):
    """记忆以独立 system 消息插在人格 prompt 之后、历史之前，且是人话不是记忆 id。"""
    from bot import memory as mem
    from bot.chat_service import BotChatService

    persona_id = db.get_bot_conversation(bot_db["id"])["persona_id"]
    mem.remember(persona_id, bot_db["id"], "我叫小林")
    captured = {}

    async def fake_stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield SimpleNamespace(content="记得", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    [c async for c in BotChatService().stream_reply(bot_db["id"], "你还记得我叫什么吗")]

    messages = captured["messages"]
    assert messages[0]["role"] == "system" and "小暖" in messages[0]["content"]
    assert messages[1]["role"] == "system"
    assert "用户叫小林" in messages[1]["content"]
    assert "[mem:" not in messages[1]["content"]
    # 历史仍然从后面接上
    assert messages[2]["role"] == "user"


@pytest.mark.asyncio
async def test_no_memory_means_no_extra_system_message(bot_db, monkeypatch):
    from bot.chat_service import BotChatService

    captured = {}

    async def fake_stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield SimpleNamespace(content="在", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    [c async for c in BotChatService().stream_reply(bot_db["id"], "在吗")]

    assert [m["role"] for m in captured["messages"]].count("system") == 1


@pytest.mark.asyncio
async def test_memory_block_never_lands_in_visible_history(bot_db, monkeypatch):
    """记忆块是对模型的补充，不是一次对话：不能出现在 bot_messages 里。"""
    from bot import memory as mem
    from bot.chat_service import BotChatService

    persona_id = db.get_bot_conversation(bot_db["id"])["persona_id"]
    mem.remember(persona_id, bot_db["id"], "我叫小林")

    async def fake_stream(**kwargs):
        yield SimpleNamespace(content="在的", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    [c async for c in BotChatService().stream_reply(bot_db["id"], "你还记得我叫什么吗")]

    stored = [m["content"] for m in db.get_bot_messages(bot_db["id"])]
    assert all("用户叫小林" not in text for text in stored)


@pytest.mark.asyncio
async def test_user_message_is_mined_into_memory_after_the_turn(bot_db, monkeypatch):
    from bot import memory as mem
    from bot.chat_service import BotChatService

    async def fake_stream(**kwargs):
        yield SimpleNamespace(content="记住了", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    [c async for c in BotChatService().stream_reply(bot_db["id"], "我住在杭州")]

    persona_id = db.get_bot_conversation(bot_db["id"])["persona_id"]
    assert [row["content"] for row in mem.list_memories(persona_id)] == ["用户住在杭州"]


@pytest.mark.asyncio
async def test_memory_failure_does_not_break_the_reply(bot_db, monkeypatch):
    """记忆模块炸了也不能影响陪聊本身。"""
    from bot import memory as mem
    from bot.chat_service import BotChatService

    def boom(*_args, **_kwargs):
        raise RuntimeError("memory store down")

    async def boom_async(*_args, **_kwargs):
        raise RuntimeError("memory store down")

    # P2：主链路走 recall/remember_async（融合召回 + 带 embed 写入）
    monkeypatch.setattr(mem, "recall", boom_async)
    monkeypatch.setattr(mem, "remember_async", boom_async)
    # 记住（含定向遗忘的内部路径）炸了同样不能外溢
    monkeypatch.setattr(mem, "remember", boom)

    async def fake_stream(**kwargs):
        yield SimpleNamespace(content="在的", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    chunks = [c async for c in BotChatService().stream_reply(bot_db["id"], "在吗")]

    assert chunks == [("content", "在的")]
