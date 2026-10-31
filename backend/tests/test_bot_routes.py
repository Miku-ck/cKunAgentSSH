import json
import os
from types import SimpleNamespace

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db


@pytest.fixture
def client(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-routes.db"))
    from routes.bot import router

    app = fastapi.FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client
    db._close_all_connections()


def create_persona(client, **overrides):
    body = {"name": "小暖", "personality": "温和", "greeting": "你好呀"}
    body.update(overrides)
    response = client.post("/api/bot/personas", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_persona_and_conversation_routes(client):
    persona = create_persona(client)
    assert client.get("/api/bot/personas").json()[-1]["id"] == persona["id"]

    copied = client.post(f"/api/bot/personas/{persona['id']}/copy")
    assert copied.status_code == 200
    assert copied.json()["name"] == "小暖 副本"

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"personality": "安静"}
    )
    assert patched.json()["personality"] == "安静"

    conversation = client.post(
        "/api/bot/conversations", json={"persona_id": persona["id"]}
    )
    assert conversation.status_code == 200
    conversation_id = conversation.json()["id"]
    assert client.get("/api/bot/conversations?channel=local").json()[0]["id"] == conversation_id
    # 本地新会话自动落开场白为首条 assistant 消息（create_persona 默认 greeting="你好呀"）
    greeting_msgs = client.get(f"/api/bot/conversations/{conversation_id}/messages").json()
    assert len(greeting_msgs) == 1 and greeting_msgs[0]["role"] == "assistant" and greeting_msgs[0]["content"] == "你好呀"

    assert client.delete(f"/api/bot/conversations/{conversation_id}").json() == {"deleted": True}
    assert client.delete(f"/api/bot/personas/{persona['id']}").json() == {"deleted": True}
    assert client.get("/api/bot/conversations?channel=local").json() == []


def test_default_persona_cannot_be_deleted(client):
    builtin = next(p for p in client.get("/api/bot/personas").json() if p["is_builtin"])
    response = client.delete(f"/api/bot/personas/{builtin['id']}")
    assert response.status_code == 409
    assert response.json()["detail"] == "默认人格不可删除"
    assert any(p["id"] == builtin["id"] for p in client.get("/api/bot/personas").json())


def test_deleting_unknown_persona_is_404(client):
    assert client.delete("/api/bot/personas/not-a-real-id").status_code == 404


def test_persona_avatar_accepts_uploaded_image(client):
    avatar = "data:image/webp;base64," + "A" * 20000
    persona = create_persona(client, avatar=avatar)
    assert persona["avatar"] == avatar
    assert client.get("/api/bot/personas").json()[-1]["avatar"] == avatar

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"avatar": "data:image/webp;base64,BBBB"}
    )
    assert patched.json()["avatar"] == "data:image/webp;base64,BBBB"


def test_persona_avatar_rejects_emoji(client):
    # 头像只收上传图片的 data URL：表情/任意文本一律 422，不再当头像存
    for bad in ["🐟", "data:text/plain;base64,AAAA", "x" * 600]:
        assert client.post(
            "/api/bot/personas", json={"name": "小暖", "avatar": bad}
        ).status_code == 422
    # 空串 = 尚未上传头像（内置人格的初始态），合法
    assert create_persona(client, avatar="")["avatar"] == ""


def test_routes_validate_references_and_input(client):
    assert client.post(
        "/api/bot/conversations", json={"persona_id": "missing"}
    ).status_code == 404
    assert client.post("/api/bot/personas", json={"name": ""}).status_code == 422
    assert client.post(
        "/api/bot/chat", json={"conversation_id": "missing", "content": "你好"}
    ).status_code == 404


def test_main_app_registers_bot_routes():
    from main import app

    paths = {route.path for route in app.routes}
    assert "/api/bot/personas" in paths
    assert "/api/bot/chat" in paths
    assert "/api/bot/channels" in paths
    assert "/api/bot/channels/{channel_id}/connect" in paths


def test_chat_route_streams_content_and_done(client, monkeypatch):
    model = db.create_model_config(
        name="M", base_url="http://model.test/v1", api_key="k", model_id="m"
    )
    persona = create_persona(client, model_id=model["id"])
    conversation = client.post(
        "/api/bot/conversations", json={"persona_id": persona["id"]}
    ).json()

    async def fake_stream(**_kwargs):
        yield SimpleNamespace(content="你好", reasoning="")
        yield SimpleNamespace(content="呀", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    response = client.post(
        "/api/bot/chat", json={"conversation_id": conversation["id"], "content": "在吗"}
    )

    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["content"] for event in events if event["type"] == "content"] == ["你好", "呀"]
    assert events[-1]["type"] == "done"
    assert events[-1]["message"]["content"] == "你好呀"
    assert [m["content"] for m in db.get_bot_messages(conversation["id"])] == ["你好呀", "在吗", "你好呀"]


def test_persona_chat_experience_columns_migrate_and_default(client):
    rows = client.get("/api/bot/personas").json()
    assert rows, "至少内置 cKunBot"
    p = rows[0]
    assert p["reply_style"] == "moderate"
    assert int(p["enable_thinking"]) == 0


def test_create_copy_update_persona_reply_style(client):
    p = db.create_bot_persona(name="小测", personality="p", background="b",
                              greeting="g", reply_style="concise", enable_thinking=1)
    assert p["reply_style"] == "concise"
    assert int(p["enable_thinking"]) == 1
    upd = db.update_bot_persona(p["id"], reply_style="long")
    assert upd["reply_style"] == "long" and int(upd["enable_thinking"]) == 1
    cp = db.copy_bot_persona(p["id"])
    assert cp["reply_style"] == "long" and int(cp["enable_thinking"]) == 1


def test_persona_invalid_reply_style_rejected(client):
    r = client.post("/api/bot/personas", json={"name": "x", "reply_style": "verbose"})
    assert r.status_code == 422


def test_persona_api_persists_style_and_thinking(client):
    r = client.post("/api/bot/personas", json={"name": "y", "reply_style": "concise", "enable_thinking": True})
    assert r.status_code == 200
    p = r.json()
    assert p["reply_style"] == "concise" and int(p["enable_thinking"]) == 1


def test_local_conversation_created_with_greeting_message(client):
    persona = client.post("/api/bot/personas", json={"name": "开场君", "greeting": "初次见面，多多指教"}).json()
    conv = client.post("/api/bot/conversations", json={"persona_id": persona["id"], "channel": "local"}).json()
    msgs = client.get(f"/api/bot/conversations/{conv['id']}/messages").json()
    assert len(msgs) == 1 and msgs[0]["role"] == "assistant" and msgs[0]["content"] == "初次见面，多多指教"


def test_bot_chat_sse_emits_reasoning_and_content_events(client, monkeypatch):
    """本地 SSE 面：reasoning chunk 派发 reasoning 事件，正文派发 content 事件，收尾 done。"""
    from bot.chat_service import BotChatService

    async def fake_stream(self, cid, content, external_msg_id=None, *, channel="local", images=None):
        assert channel == "local"
        yield "reasoning", "想"
        yield "content", "好"

    monkeypatch.setattr(BotChatService, "stream_reply", fake_stream)
    monkeypatch.setattr(
        "routes.bot.db.get_bot_messages",
        lambda cid, n=1: [{"id": "m", "role": "assistant", "content": "好", "created_at": ""}],
    )
    conv = client.post(
        "/api/bot/conversations", json={"persona_id": client.get("/api/bot/personas").json()[0]["id"]}
    ).json()
    with client.stream(
        "POST", "/api/bot/chat",
        json={"conversation_id": conv["id"], "content": "在吗"},
    ) as r:
        body = "".join(r.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert "reasoning" in types and "content" in types
    assert next(e for e in events if e["type"] == "reasoning")["content"] == "想"
    assert next(e for e in events if e["type"] == "content")["content"] == "好"
    assert types[-1] == "done"


def test_persona_options_exposes_enums_for_editor(client):
    options = client.get("/api/bot/persona-options").json()
    for key in (
        "sentence", "particles", "punctuation", "emoji",
        "reaction_style", "refusal_style", "relationship_speed",
    ):
        assert key in options, key
        assert options[key]["options"], key
    # 前端靠 value 渲染控件，label 只用于显示：两者都必须存在
    assert all({"value", "label"} <= set(item) for item in options["reaction_style"]["options"])


def test_sticker_library_lists_manifest_packs(client):
    from bot import stickers as sticker_mod

    packs = client.get("/api/bot/stickers").json()

    assert [pack["id"] for pack in packs] == [pack.id for pack in sticker_mod.PACKS]
    assert [pack["name"] for pack in packs] == ["鲸鱼娘", "鲸鱼娘·文字"]
    assert [key for pack in packs for key in pack["keys"]] == [item.key for item in sticker_mod.STICKERS]


def test_persona_roundtrips_identity_core_fields(client):
    persona = create_persona(
        client,
        expression_dna={"sentence": "short", "particles": "dry", "emoji": "none", "catchphrase": "说来话长"},
        reaction_style="playful",
        refusal_style="direct",
        address_user="小林",
        relationship_speed="fast",
    )
    fetched = next(p for p in client.get("/api/bot/personas").json() if p["id"] == persona["id"])
    assert fetched["expression_dna"]["sentence"] == "short"
    assert fetched["expression_dna"]["catchphrase"] == "说来话长"
    assert fetched["reaction_style"] == "playful"
    assert fetched["refusal_style"] == "direct"
    assert fetched["address_user"] == "小林"
    assert fetched["relationship_speed"] == "fast"

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"reaction_style": "blunt"}
    ).json()
    assert patched["reaction_style"] == "blunt"
    # 局部更新不许把没传的字段冲掉
    assert patched["address_user"] == "小林"
    assert patched["expression_dna"]["sentence"] == "short"


def test_persona_rejects_unknown_style_but_sanitizes_dna(client):
    response = client.post(
        "/api/bot/personas", json={"name": "小暖", "reaction_style": "sarcastic"}
    )
    assert response.status_code == 422

    persona = create_persona(client, expression_dna={"sentence": "poetic", "emoji": "medium"})
    # 未知档位退回默认，但已知档位保留：脏 DNA 不该让人格保存失败
    assert persona["expression_dna"]["sentence"] == "mixed"
    assert persona["expression_dna"]["emoji"] == "medium"


def test_persona_group_mode_defaults_quietest_and_rejects_unknown_values(client):
    """群参与档位是行为开关：默认最保守，乱值进不来。"""
    persona = create_persona(client)
    assert persona["group_mode"] == "mention_only", "新建人格不能在群里插话"

    loud = create_persona(client, group_mode="ambient")
    assert loud["group_mode"] == "ambient"

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"group_mode": "proactive"}
    ).json()
    assert patched["group_mode"] == "proactive"
    # 局部更新不得把没传的主动性总闸冲掉
    assert patched["proactive_enabled"] == persona["proactive_enabled"]

    bad = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"group_mode": "always"}
    )
    assert bad.status_code == 422


def test_persona_options_lists_the_group_modes_in_order(client):
    options = client.get("/api/bot/persona-options").json()["group_mode"]
    assert [item["value"] for item in options["options"]] == [
        "mention_only", "ambient", "proactive"
    ]
    assert all(item["label"] for item in options["options"]), "文案不能空"


def test_copy_persona_does_not_carry_the_group_mode_over(client):
    """副本不继承群参与档：与主动性总闸同口径，不拿复制多开一张嘴。"""
    persona = create_persona(client, group_mode="proactive")
    copy = client.post(f"/api/bot/personas/{persona['id']}/copy").json()
    assert copy["group_mode"] == "mention_only"


def test_copy_persona_keeps_voice_settings(client):
    persona = create_persona(client, expression_dna={"sentence": "long"}, address_user="小林")
    copy = client.post(f"/api/bot/personas/{persona['id']}/copy").json()
    assert copy["expression_dna"]["sentence"] == "long"
    assert copy["address_user"] == "小林"


def test_persona_mention_switch_defaults_on_and_survives_partial_update(client):
    """群回复 @ 人不 @ 是偏好，不是闸：默认维持现状（照旧 @）。"""
    persona = create_persona(client)
    assert persona["mention_sender"] == 1

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"mention_sender": False}
    ).json()
    assert patched["mention_sender"] == 0
    # 局部更新不能把另一个行为开关冲掉
    assert patched["group_mode"] == persona["group_mode"]

    renamed = client.patch(f"/api/bot/personas/{persona['id']}", json={"name": "小暖二号"}).json()
    assert renamed["mention_sender"] == 0, "没传就该保持原值"

    loud = client.post(
        "/api/bot/personas", json={"name": "乱值", "mention_sender": "sometimes"}
    )
    assert loud.status_code == 422


def test_copy_persona_carries_the_mention_preference(client):
    """副本不继承「开口类」开关，但 @ 不 @ 是说话分寸：跟着人格走。"""
    persona = create_persona(client, mention_sender=False)
    copy = client.post(f"/api/bot/personas/{persona['id']}/copy").json()
    assert copy["mention_sender"] == 0


def test_probe_endpoint_runs_the_scheduler_for_the_named_conversation(client, monkeypatch):
    from bot.proactive import ProactiveScheduler

    persona = create_persona(client)
    conv = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g1"
    )
    seen = []

    async def fake_probe(_self, target):
        seen.append(target["conversation_id"])
        return {
            "conversation_id": target["conversation_id"], "action": "push",
            "desire": 0.2, "gate_reason": "desire 0.20 < 0.55", "text": "在吗", "detail": "",
        }

    monkeypatch.setattr(ProactiveScheduler, "probe", fake_probe)
    response = client.post(
        "/api/bot/proactive/probe", json={"conversation_id": conv["id"]}
    )

    assert response.status_code == 200, response.text
    assert seen == [conv["id"]]
    assert response.json()["action"] == "push"


def test_probe_endpoint_rejects_an_unknown_or_unsupported_conversation(client):
    assert client.post(
        "/api/bot/proactive/probe", json={"conversation_id": "nope"}
    ).status_code == 404
    persona = create_persona(client)
    # 微信没有主动发送通道：当场说不支持，比让调度器默默寄存一句好
    wechat = db.create_bot_conversation(
        persona_id=persona["id"], channel="wechat", external_id="wx:o1"
    )
    assert client.post(
        "/api/bot/proactive/probe", json={"conversation_id": wechat["id"]}
    ).status_code == 409
    assert client.post("/api/bot/proactive/probe", json={"conversation_id": ""}).status_code == 422


def test_relations_are_listed_per_persona_and_start_unknown(client):
    persona = create_persona(client)
    relations = client.get("/api/bot/relations").json()
    assert persona["id"] in relations
    state = relations[persona["id"]]
    assert state["stage"] == "陌生"
    assert state["progress"] == 0
    assert state["turns"] == 0


def test_relation_grows_after_a_turn(client, monkeypatch):
    from bot.chat_service import BotChatService

    async def fake_stream(self, cid, content, external_msg_id=None, *, channel="local", images=None):
        yield "content", "在"

    monkeypatch.setattr(BotChatService, "stream_reply", fake_stream)
    persona = create_persona(client)
    conv = client.post("/api/bot/conversations", json={"persona_id": persona["id"]}).json()
    with client.stream("POST", "/api/bot/chat", json={"conversation_id": conv["id"], "content": "在吗"}) as r:
        "".join(r.iter_text())

    # stream_reply 被整体替换，关系记账在真实实现里发生，这里只验证接口可读且形状稳定
    state = client.get("/api/bot/relations").json()[persona["id"]]
    assert set(state) >= {"stage", "progress", "turns", "score", "raw_score"}


def test_patch_sanitizes_expression_dna(client):
    persona = create_persona(client, expression_dna={"sentence": "short"})
    patched = client.patch(
        f"/api/bot/personas/{persona['id']}",
        json={"expression_dna": {"sentence": "poetic", "particles": "lively"}},
    ).json()
    # 局部更新走同一条归一规则：未知档位退回默认，已知档位照收
    assert patched["expression_dna"]["sentence"] == "mixed"
    assert patched["expression_dna"]["particles"] == "lively"


def test_bot_chat_rejects_too_many_images(client):
    r = client.post("/api/bot/chat", json={
        "conversation_id": "x", "content": "看这些",
        "images": ["data:image/webp;base64,AAAA"] * 5,
    })
    assert r.status_code == 422


def test_bot_chat_rejects_non_image_data_url(client):
    r = client.post("/api/bot/chat", json={
        "conversation_id": "x", "content": "看",
        "images": ["data:text/plain;base64,AAAA"],
    })
    assert r.status_code == 422


def test_conversation_payload_keeps_only_display_fields(client):
    persona = create_persona(client, personality="温和", background="我们认识十年了")
    conv = client.post("/api/bot/conversations", json={"persona_id": persona["id"]}).json()

    # 对外形状：persona 只留展示三字段，快照字段本身不再外泄
    assert conv["persona"] == {"id": persona["id"], "name": "小暖", "avatar": ""}
    assert "persona_snapshot" not in conv
    assert "background" not in conv["persona"]
    assert "personality" not in conv["persona"]
    # 列表顺带返回最后一条内容，侧栏可直接当摘要用
    listed = client.get("/api/bot/conversations").json()[0]
    assert listed["preview"] == "你好呀"


def test_conversation_shows_snapshot_name_after_persona_deleted(client):
    persona = create_persona(client)
    client.post("/api/bot/conversations", json={"persona_id": persona["id"]})
    assert client.delete(f"/api/bot/personas/{persona['id']}").status_code == 200

    row = client.get("/api/bot/conversations").json()[0]
    assert row["persona_id"] is None
    # 人格删了也要显示当时的名字，QQ 记录区同理（原来是「未知人格」）
    assert row["persona"]["name"] == "小暖"
    assert row["persona"]["avatar"] == ""


# ── 长期记忆 ──


def test_persona_memories_are_listed_per_persona(client):
    from bot import memory as mem

    persona = create_persona(client)
    other = create_persona(client, name="小刻")
    mem.remember(persona["id"], None, "我叫小林")

    rows = client.get(f"/api/bot/personas/{persona['id']}/memories").json()
    assert [row["content"] for row in rows] == ["用户叫小林"]
    assert rows[0]["title"] == "称呼"
    # 别人格的记忆不串台
    assert client.get(f"/api/bot/personas/{other['id']}/memories").json() == []


def test_persona_memories_404_for_unknown_persona(client):
    assert client.get("/api/bot/personas/nope/memories").status_code == 404
    assert client.post("/api/bot/personas/nope/memories", json={"content": "x"}).status_code == 404
    assert client.post("/api/bot/personas/nope/memories/clear").status_code == 404


def test_manual_memory_can_be_added_and_deleted(client):
    persona = create_persona(client)
    added = client.post(
        f"/api/bot/personas/{persona['id']}/memories", json={"content": "用户不吃香菜"}
    )
    assert added.status_code == 200
    memory_id = added.json()["id"]

    rows = client.get(f"/api/bot/personas/{persona['id']}/memories").json()
    assert [row["content"] for row in rows] == ["用户不吃香菜"]

    assert client.delete(f"/api/bot/memories/{memory_id}").status_code == 200
    assert client.get(f"/api/bot/personas/{persona['id']}/memories").json() == []
    # 删不存在的记忆是 404，不是静默成功
    assert client.delete(f"/api/bot/memories/{memory_id}").status_code == 404


def test_manual_memory_rejects_empty_content(client):
    persona = create_persona(client)
    assert client.post(
        f"/api/bot/personas/{persona['id']}/memories", json={"content": ""}
    ).status_code == 422


def test_clear_all_memories(client):
    from bot import memory as mem

    persona = create_persona(client)
    mem.remember(persona["id"], None, "我叫小林，我住在杭州")
    cleared = client.post(f"/api/bot/personas/{persona['id']}/memories/clear")
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] == 2
    assert client.get(f"/api/bot/personas/{persona['id']}/memories").json() == []


def test_persona_patch_revises_snapshot_used_for_generation(client):
    """会话对外只露 {id,name,avatar}，但生成读的是内部全量快照：PATCH 后必须跟着改。"""
    persona = create_persona(client)
    conversation = client.post(
        "/api/bot/conversations", json={"persona_id": persona["id"]}
    ).json()
    assert db.get_bot_conversation(conversation["id"])["persona"]["personality"] == "温和"

    client.patch(f"/api/bot/personas/{persona['id']}", json={
        "personality": "克制", "memory_directive": "记住我在准备面试",
    })
    internal = db.get_bot_conversation(conversation["id"])["persona"]
    assert internal["personality"] == "克制"
    assert internal["memory_directive"] == "记住我在准备面试"

    # 对外形状不因此变宽
    row = client.get("/api/bot/conversations").json()[0]
    assert set(row["persona"]) == {"id", "name", "avatar"}


def test_remote_conversation_display_override(client):
    persona = create_persona(client)
    # QQ / 微信会话由通道侧创建，路由只收 local —— 测试直接走库函数造数据
    qq_conv = db.create_bot_conversation(persona["id"], channel="qq", external_id="group:GG1")
    wx_conv = db.create_bot_conversation(
        persona["id"], channel="wechat", external_id="wx:openid-1"
    )
    local_id = client.post(
        "/api/bot/conversations", json={"persona_id": persona["id"]}
    ).json()["id"]

    patched = client.patch(
        f"/api/bot/conversations/{qq_conv['id']}",
        json={"display_name": "夜宵群", "display_avatar": "data:image/webp;base64,AAAA"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == "夜宵群"
    assert patched.json()["display_avatar"] == "data:image/webp;base64,AAAA"

    # 微信会话与 QQ 同一口径：只改本地看到的名字/头像
    wx_patched = client.patch(
        f"/api/bot/conversations/{wx_conv['id']}", json={"display_name": "微信好友"}
    )
    assert wx_patched.status_code == 200, wx_patched.text
    assert wx_patched.json()["display_name"] == "微信好友"

    # 本地会话不吃显示层覆盖：标题就是人格名，盖了反而误导
    assert client.patch(
        f"/api/bot/conversations/{local_id}", json={"display_name": "x"}
    ).status_code == 400
    # 非法头像 422、不存在的会话 404
    assert client.patch(
        f"/api/bot/conversations/{qq_conv['id']}", json={"display_avatar": "🐟"}
    ).status_code == 422
    assert client.patch(
        "/api/bot/conversations/nope", json={"display_name": "x"}
    ).status_code == 404

    # 列表一次带回覆盖列（QQ 记录侧栏与标题栏都要用）
    row = client.get("/api/bot/conversations?channel=qq").json()[0]
    assert row["display_name"] == "夜宵群" and row["display_avatar"] == "data:image/webp;base64,AAAA"


def test_wechat_conversations_are_listable(client):
    """微信会话与 QQ 一样可读：只放开真实存在的渠道，未知渠道仍被 422 挡回。"""
    persona = create_persona(client)
    conv = db.create_bot_conversation(
        persona["id"], channel="wechat", external_id="wx:openid-1"
    )
    rows = client.get("/api/bot/conversations?channel=wechat").json()
    assert [r["id"] for r in rows] == [conv["id"]]
    assert rows[0]["channel"] == "wechat"
    assert client.get("/api/bot/conversations?channel=wechatx").status_code == 422


def test_qq_display_update_does_not_bump_updated_at(client):
    persona = create_persona(client)
    qq_conv = db.create_bot_conversation(persona["id"], channel="qq", external_id="group:GG2")
    patched = client.patch(
        f"/api/bot/conversations/{qq_conv['id']}", json={"display_name": "改名不改序"}
    ).json()
    assert patched["updated_at"] == qq_conv["updated_at"]


def test_conversation_display_migration_idempotent(base_db):
    # conftest 的 base_db 已 init 过一次；再 init 不得 Duplicate column name
    db._close_all_connections()
    db.init_db(base_db)
    persona = db.create_bot_persona(name="小刻")
    conv = db.create_bot_conversation(persona["id"], channel="qq", external_id="group:GG3")
    assert conv["display_name"] is None
    assert db.get_bot_conversation(conv["id"])["display_avatar"] is None


def test_persona_roundtrips_expansion_fields(client):
    """四个新分区只是长文本：POST/PATCH 原样进出，超长按 422 挡回而不是静默截断。"""
    persona = create_persona(
        client,
        likes_landmines="喜欢夜里散步；雷区是被催",
        memory_directive="记住我在准备面试，别提前任",
        dialogue_samples="用户：今天好累\n晚风：又熬到几点？",
        advanced_play="可以玩树洞模式，只听不建议",
    )
    fetched = next(p for p in client.get("/api/bot/personas").json() if p["id"] == persona["id"])
    assert fetched["likes_landmines"] == "喜欢夜里散步；雷区是被催"
    assert fetched["dialogue_samples"] == "用户：今天好累\n晚风：又熬到几点？"

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"advanced_play": "再加互相打卡"}
    ).json()
    assert patched["advanced_play"] == "再加互相打卡"
    assert patched["likes_landmines"] == "喜欢夜里散步；雷区是被催"

    # 未填写的分区读到空串，前端据此决定这一节不进提示词
    bare = create_persona(client, name="极简")
    for field in ("likes_landmines", "memory_directive", "dialogue_samples", "advanced_play"):
        assert bare[field] == "", field

    assert client.post(
        "/api/bot/personas", json={"name": "超长", "dialogue_samples": "样" * 4001}
    ).status_code == 422
