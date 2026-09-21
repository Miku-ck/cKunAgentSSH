"""
技能 / 对话 / 终端预测路由回归（第 5 轮审查）

覆盖本轮修掉的问题：
- skill_mode_config 无外键：删除技能后 per-mode 开关残留（GET /skills 与每次
  按 mode 过滤都要全表扫它）
- PATCH /skills/<id>/enabled?mode=ssh 对不存在的技能写脏配置并返回 200
- POST /skills/sync-builtin 收到空上报时把已同步的内置技能全当孤儿删掉
- 上传后缀白名单里 .makefile/.dockerfile 永远匹配不上（Path.suffix 为空）
- 会话删除的级联清理按后端 id 归档，前端历史上按本地 id 上传 → 附件永久孤儿
- 终端预测超时路径不关闭 LLM 流式生成器（in-flight 计数不归零）
"""
import os

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db


@pytest.fixture
def base_db(tmp_workspace):
    db_path = os.path.join(tmp_workspace, "round5.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


def _make_client(*routers) -> TestClient:
    app = fastapi.FastAPI()
    for r in routers:
        app.include_router(r, prefix="/api")
    return TestClient(app)


def _mode_config_rows(skill_id: str = None) -> list:
    conn = db._get_conn()
    try:
        if skill_id:
            return conn.execute(
                "SELECT * FROM skill_mode_config WHERE skill_id = ?", (skill_id,)
            ).fetchall()
        return conn.execute("SELECT * FROM skill_mode_config").fetchall()
    finally:
        conn.close()


# ── skill_mode_config 生命周期 ─────────────────────────────────────────────


def test_delete_skill_prunes_mode_config(base_db):
    """删普通技能必须连 per-mode 开关一起删（表上无外键，不会级联）"""
    skill = db.create_skill(name="S", slug="s-prune", system_prompt="p")
    db.upsert_skill_mode_config(skill["id"], "ssh", 0)
    assert len(_mode_config_rows(skill["id"])) == 1

    assert db.delete_skill(skill["id"]) is True
    assert _mode_config_rows(skill["id"]) == []


def test_delete_builtin_by_slug_prunes_mode_config(base_db):
    """孤儿内置技能按 slug 清理时同样不留配置行"""
    skill = db.upsert_builtin_skill(name="B", slug="b-prune", system_prompt="p")
    db.upsert_skill_mode_config(skill["id"], "ssh", 0)

    assert db.delete_builtin_skill_by_slug("b-prune") is True
    assert _mode_config_rows(skill["id"]) == []


def test_upsert_mode_config_returns_real_id_on_conflict(base_db):
    """ON CONFLICT 命中时返回既有行 id，而不是本次生成的幽灵 id"""
    skill = db.create_skill(name="S", slug="s-id", system_prompt="p")
    first = db.upsert_skill_mode_config(skill["id"], "ssh", 1)
    second = db.upsert_skill_mode_config(skill["id"], "ssh", 0)
    assert second["id"] == first["id"]
    rows = _mode_config_rows(skill["id"])
    assert len(rows) == 1 and rows[0]["id"] == second["id"]


def test_patch_mode_enabled_unknown_skill_404(base_db):
    """per-mode 分支同样要校验技能存在，否则任意 skill_id 都能写入配置表"""
    import routes.skills as skills_mod
    client = _make_client(skills_mod.router)
    resp = client.patch(
        "/api/skills/does-not-exist/enabled?mode=ssh", json={"enabled": True}
    )
    assert resp.status_code == 404
    assert _mode_config_rows() == []


def test_sync_builtin_empty_payload_keeps_existing(base_db):
    """空上报 ≠ 用户删光技能：扫描失败交空列表时不得清空已同步的内置技能"""
    import routes.skills as skills_mod
    db.upsert_builtin_skill(name="Keep", slug="keep-me", system_prompt="p")
    client = _make_client(skills_mod.router)

    resp = client.post("/api/skills/sync-builtin", json={"skills": []})
    assert resp.status_code == 200
    assert resp.json()["orphans_removed"] == 0
    assert "keep-me" in {s["slug"] for s in db.list_skills(limit=100)}


# ── 上传与级联清理 ─────────────────────────────────────────────────────────


def test_upload_accepts_extensionless_build_files(base_db):
    """Makefile/Dockerfile 无后缀，白名单只按 suffix 匹配会永远拒掉"""
    import routes.chat as chat_mod
    client = _make_client(chat_mod.router)

    for name in ("Makefile", "Dockerfile", "notes.md"):
        resp = client.post(
            "/api/chat/upload",
            files={"file": (name, b"all:\n\techo hi\n", "text/plain")},
            data={"conversation_id": "conv-1"},
        )
        assert resp.status_code == 200, f"{name}: {resp.text}"

    resp = client.post(
        "/api/chat/upload",
        files={"file": ("payload.exe", b"\x00\x01", "application/octet-stream")},
        data={"conversation_id": "conv-1"},
    )
    assert resp.status_code == 400


def test_delete_conversation_removes_uploads_dir(base_db):
    """按后端会话 id 归档的附件目录，在删除会话时确实被回收"""
    import routes.chat as chat_mod
    client = _make_client(chat_mod.router)

    conv = db.create_conversation(title="t", mode="ssh")
    resp = client.post(
        "/api/chat/upload",
        files={"file": ("a.md", b"# hello", "text/markdown")},
        data={"conversation_id": conv["id"]},
    )
    assert resp.status_code == 200
    conv_dir = os.path.join(os.path.dirname(base_db), "uploads", conv["id"])
    assert os.path.isdir(conv_dir) and os.listdir(conv_dir)

    assert client.delete(f"/api/chat/conversations/{conv['id']}").status_code == 200
    assert not os.path.exists(conv_dir)


def test_create_conversation_title_bounded(base_db):
    """建会话与改会话同一上限：标题会整份进 localStorage 缓存"""
    import routes.chat as chat_mod
    client = _make_client(chat_mod.router)

    assert client.post("/api/chat/conversations", json={"title": "x" * 201}).status_code == 422
    assert client.post("/api/chat/conversations", json={"title": "ok"}).status_code == 200


# ── 终端预测：流式生成器释放 ───────────────────────────────────────────────


def test_predict_closes_llm_stream_on_timeout(base_db, monkeypatch):
    """超时后必须 aclose 生成器，否则 llm_adapter 的 in-flight 计数挡住客户端回收"""
    import asyncio

    import routes.terminal as term

    db.create_model_config(name="M", base_url="http://x", api_key="k",
                           model_id="m", is_default_ssh=True)
    monkeypatch.setattr(term, "_LLM_TIMEOUT_S", 0.05)

    closed = {"v": False}

    async def hanging_stream(**_kwargs):
        try:
            yield type("C", (), {"content": ""})()
            await asyncio.sleep(5)
        finally:
            closed["v"] = True

    monkeypatch.setattr(term, "chat_stream", hanging_stream)
    client = _make_client(term.router)
    resp = client.post("/api/terminal/predict", json={
        "session_id": "s1", "recent_commands": ["git status"], "cwd": "/tmp",
    })

    assert resp.status_code == 200
    assert resp.json()["predictions"] == []
    assert closed["v"] is True, "超时路径未关闭 LLM 流"


def test_predict_handles_validation_detail_as_text(base_db):
    """recent_commands 超上限时返回 422（前端据此提示，不再拼成 [object Object]）"""
    import routes.terminal as term

    db.create_model_config(name="M", base_url="http://x", api_key="k",
                           model_id="m", is_default_ssh=True)
    client = _make_client(term.router)
    resp = client.post("/api/terminal/predict", json={
        "session_id": "s1", "recent_commands": ["c"] * 51,
    })
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)
