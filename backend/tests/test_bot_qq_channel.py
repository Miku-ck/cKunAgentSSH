import pytest

from bot.qq_channel import QQEventProcessor


class _Msg:
    def __init__(self):
        self.content = "你好"
        self.event_id = "e1"
        self.id = "e1"
        self.group_openid = "g1"
        self.sent_replies = []

    async def reply(self, content, msg_type=0):
        self.sent_replies.append(content)


class _Svc:
    async def complete_reply(self, cid, content, external_msg_id=None, *, channel="qq"):
        return "正文"


@pytest.mark.asyncio
async def test_first_turn_prepends_greeting(monkeypatch):
    import database as db

    conv = {"id": "c1", "persona": {"greeting": "嗨"}}
    state = {"history": []}
    monkeypatch.setattr(db, "get_or_create_bot_conversation", lambda *a, **k: conv)
    monkeypatch.setattr(db, "get_bot_message_by_external_id", lambda *a, **k: None)
    monkeypatch.setattr(db, "get_recent_bot_messages", lambda cid, n: list(state["history"]))
    monkeypatch.setattr(db, "mark_bot_message_answered", lambda *a, **k: None)
    proc = QQEventProcessor(_Svc(), "p1", "ctx")

    m = _Msg()
    await proc._handle(m, "group:g1:ctx")
    assert m.sent_replies[0].startswith("嗨")

    # 已有往来：后续回合不前置 greeting
    state["history"] = [{"role": "user", "content": "x"}]
    m2 = _Msg()
    m2.event_id = m2.id = "e2"
    await proc._handle(m2, "group:g1:ctx")
    assert not m2.sent_replies[0].startswith("嗨")
