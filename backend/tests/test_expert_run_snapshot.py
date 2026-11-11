"""专家快照落库守卫：legacy run 三列为空且迭代数不变 /
专家 run 的快照可无损回读 / 请求模型接受 expert_id 且默认 None。"""
import json

import pytest

import database as db
from agent.expert_policy import build_snapshot, resolve_expert


@pytest.fixture(autouse=True)
def _seed_conversation(base_db):
    """agent_runs.conversation_id 有外键，落库前必须先有会话。"""
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO conversations "
            "(id, title, status, mode, created_at, updated_at) "
            "VALUES ('conv-1', '测试会话', 'completed', 'ssh', datetime('now'), datetime('now'))"
        )
        conn.commit()
    finally:
        conn.close()


def _read_run(run_id):
    conn = db._get_conn()
    try:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _seed_expert(monkeypatch=None):
    return {
        "id": "expert-1", "slug": "db-ops", "name": "数据库专家",
        "revision": 3, "enabled": 1,
        "identity_prompt": "你是 DBA", "model_id": None,
        "tool_names_json": None, "plugin_ids_json": "[]", "skill_ids_json": None,
        "permission_ceiling": "plan", "max_iterations": 20, "memory_scope": "none",
    }


def test_legacy_run_keeps_expert_columns_null(base_db):
    db.create_agent_run("run-legacy", "conv-1", "排查磁盘",
                        max_iterations=100, mode="ssh", model_used="model-a")
    row = _read_run("run-legacy")
    assert row["expert_id"] is None
    assert row["expert_name"] is None
    assert row["expert_snapshot_json"] is None
    assert row["max_iterations"] == 100


def test_expert_run_snapshot_roundtrip(base_db):
    policy = resolve_expert(_seed_expert())
    snapshot = build_snapshot(
        policy, model_id="model-b", permission_mode="plan", max_iterations=20,
        tool_names=["file_read"], skill_ids=["skill-a"], memory_scopes=[],
    )
    db.create_agent_run(
        "run-expert", "conv-1", "检查索引",
        max_iterations=20, mode="ssh", model_used="model-b",
        expert_id="expert-1", expert_name="数据库专家",
        expert_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
    )
    row = _read_run("run-expert")
    assert row["expert_id"] == "expert-1"
    assert row["expert_name"] == "数据库专家"
    assert json.loads(row["expert_snapshot_json"]) == snapshot


def test_snapshot_recorded_on_run_contains_no_secret(base_db):
    policy = resolve_expert(_seed_expert())
    snapshot = build_snapshot(
        policy, model_id="model-b", permission_mode="plan", max_iterations=20,
        tool_names=[], skill_ids=[], memory_scopes=[],
    )
    forbidden = {"api_key", "env", "env_ciphertext", "secret", "token", "password"}
    assert not (set(snapshot) & forbidden)
    db.create_agent_run(
        "run-nosecret", "conv-1", "检查索引",
        expert_id="expert-1", expert_name="数据库专家",
        expert_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
    )
    stored = json.loads(_read_run("run-nosecret")["expert_snapshot_json"])
    assert not (set(stored) & forbidden)


def test_run_request_has_optional_expert_id():
    from routes.agent import AgentRunRequest
    field = AgentRunRequest.model_fields.get("expert_id")
    assert field is not None
    assert field.default is None
