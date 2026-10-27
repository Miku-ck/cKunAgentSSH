import os
from types import SimpleNamespace

import pytest

import database as db


@pytest.fixture
def qq_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "qq-channel.db"))
    persona = db.create_bot_persona(name="QQ 小暖")
    yield persona
    db._close_all_connections()


class FakeMessage:
    def __init__(self, event_id, content, group_openid=None, user_openid=None):
        self.id = event_id
        self.event_id = event_id
        self.content = content
        self.group_openid = group_openid
        self.author = SimpleNamespace(member_openid="member-1", user_openid=user_openid)
        self.replies = []

    async def reply(self, content, msg_type=0):
        self.replies.append(content)


class FakeChatService:
    def __init__(self):
        self.calls = []

    async def complete_reply(self, conversation_id, content, external_msg_id=None):
        self.calls.append((conversation_id, content, external_msg_id))
        db.add_bot_message(conversation_id, "user", content, external_msg_id)
        reply = f"回复：{content}"
        db.add_bot_message(conversation_id, "assistant", reply)
        return reply


@pytest.mark.asyncio
async def test_group_event_replies_once_and_persists_external_id(qq_db):
    from bot.qq_channel import QQEventProcessor

    service = FakeChatService()
    processor = QQEventProcessor(service, persona_id=qq_db["id"], context_id="ctx-1")
    message = FakeMessage("event-1", "  你好  ", group_openid="group-1")

    await processor.handle_group(message)
    await processor.handle_group(message)

    assert message.replies == ["回复：你好"]
    assert len(service.calls) == 1
    conversation_id = service.calls[0][0]
    conversation = db.get_bot_conversation(conversation_id)
    assert conversation["channel"] == "qq"
    assert conversation["external_id"] == "group:group-1:ctx-1"
    messages = db.get_bot_messages(conversation_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["external_msg_id"] == "event-1"


@pytest.mark.asyncio
async def test_group_and_private_messages_use_separate_contexts(qq_db):
    from bot.qq_channel import QQEventProcessor

    service = FakeChatService()
    processor = QQEventProcessor(service, persona_id=qq_db["id"], context_id="ctx-1")
    group = FakeMessage("event-g", "群里好", group_openid="group-1")
    private = FakeMessage("event-c", "私下好", user_openid="user-1")

    await processor.handle_group(group)
    await processor.handle_c2c(private)

    assert service.calls[0][0] != service.calls[1][0]
    external_ids = {row["external_id"] for row in db.list_bot_conversations(channel="qq")}
    assert external_ids == {"group:group-1:ctx-1", "c2c:user-1:ctx-1"}


@pytest.mark.asyncio
async def test_model_error_returns_safe_qq_message(qq_db):
    from bot.qq_channel import QQEventProcessor

    class FailingService:
        async def complete_reply(self, *_args, **_kwargs):
            raise RuntimeError("secret upstream detail")

    processor = QQEventProcessor(FailingService(), persona_id=qq_db["id"], context_id="ctx-1")
    message = FakeMessage("event-1", "你好", user_openid="user-1")
    await processor.handle_c2c(message)

    assert message.replies == ["我这边刚才没接上话，请稍后再试一次。"]
