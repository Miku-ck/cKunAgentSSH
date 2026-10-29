import pytest

import bot.chat_service as cs_mod
from bot.chat_service import BotChatService


class _Chunk:
    def __init__(self, content=None, reasoning=None):
        self.content, self.reasoning = content, reasoning


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
