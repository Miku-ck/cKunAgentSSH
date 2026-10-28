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

    def fake_add(conv, role, content, external_msg_id=None, reasoning=None):
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
