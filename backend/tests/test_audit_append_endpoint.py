"""S4-07 回归：渲染层通道的审计写入口 POST /api/audit/append。

S4-07 的原缺口是「通道级零审计」：批量下发（bulk SSH）与 Docker 变更动作走的是
`ssh:exec` IPC，Electron 侧从不写 audit_log，于是这两条人工交互通道在审计页上
完全不存在。本端点给渲染层一个受控写入口，**只追加、不查询/修改**；operator
由服务端派生（客户端不许伪造），结论必须命中 LEGAL_CONCLUSIONS。

与内部 `append_record` 的关键差别：内部对非法结论做「静默改 executed + 留痕」，
那是为了不阻塞 Agent；HTTP 边界上客户端 bug 必须当场 422，否则一条拼错的
"executed " 会被链式签名记录声称「执行过了」。写失败返回 500，但渲染层调用方
必须降级为提示、不阻塞用户动作（见 src/utils/remote-audit.ts）。
"""
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database as db
from agent import audit
from routes.audit import router as audit_router


def _conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


@pytest.fixture
def wired(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    app = FastAPI()
    app.include_router(audit_router, prefix="/api")
    return TestClient(app), conn


def _payload(**over):
    body = {
        "action": "bulk_ssh_exec",
        "conclusion": "executed",
        "target_type": "ssh",
        "target_id": "web-1 (root@10.0.0.1)",
        "environment": "prod",
        "command_raw": "systemctl restart nginx",
        "result": "exit=0 durationMs=123",
        "run_id": "",
        "iteration": 0,
    }
    body.update(over)
    return body


def _row_count(conn):
    return conn.execute(f"SELECT COUNT(*) FROM {audit.AUDIT_TABLE}").fetchone()[0]


def test_append_writes_signed_record_and_returns_identity(wired):
    client, conn = wired
    resp = client.post("/api/audit/append", json=_payload())
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"] is True
    assert out["id"] == 1
    assert len(out["hash"]) == 64

    rec = conn.execute(f"SELECT * FROM {audit.AUDIT_TABLE} WHERE id=1").fetchone()
    assert rec["action"] == "bulk_ssh_exec"
    assert rec["conclusion"] == "executed"
    assert rec["environment"] == "prod"
    assert rec["target_type"] == "ssh"
    assert rec["target_id"] == "web-1 (root@10.0.0.1)"
    assert rec["command_raw"] == "systemctl restart nginx"
    assert rec["result"] == "exit=0 durationMs=123"
    assert rec["prev_hash"] == ""
    assert rec["operator"], "operator 必须由服务端派生"
    assert len(rec["command_hash"]) == 16, "命令原文要带规范化哈希，导出侧才可对账"


def test_two_appends_chain_and_verify_intact(wired):
    client, _conn_ = wired
    first = client.post("/api/audit/append", json=_payload()).json()
    second = client.post(
        "/api/audit/append",
        json=_payload(action="docker_action", command_raw="docker rm -f web"),
    ).json()
    assert second["id"] == first["id"] + 1

    rows = _conn_.execute(
        f"SELECT id, prev_hash, hash FROM {audit.AUDIT_TABLE} ORDER BY id"
    ).fetchall()
    assert rows[0]["prev_hash"] == ""
    assert rows[1]["prev_hash"] == rows[0]["hash"] == first["hash"]
    assert client.get("/api/audit/verify").json()["intact"] is True


def test_environment_defaults_to_test_when_omitted(wired):
    client, conn = wired
    payload = _payload()
    payload.pop("environment")
    assert client.post("/api/audit/append", json=payload).status_code == 200
    assert conn.execute(f"SELECT environment FROM {audit.AUDIT_TABLE}").fetchone()[0] == "test"


@pytest.mark.parametrize("conclusion", ["looks_fine", "", "EXECUTED", "executed "])
def test_illegal_conclusion_is_rejected_without_a_write(wired, conclusion):
    """内部 append_record 会把非法结论改成 executed；HTTP 边界不能沿用这条兜底。"""
    client, conn = wired
    resp = client.post("/api/audit/append", json=_payload(conclusion=conclusion))
    assert resp.status_code == 422, resp.text
    assert _row_count(conn) == 0


@pytest.mark.parametrize("environment", ["staging", "", "PROD", None])
def test_unknown_environment_is_rejected(wired, environment):
    client, conn = wired
    resp = client.post("/api/audit/append", json=_payload(environment=environment))
    assert resp.status_code == 422, resp.text
    assert _row_count(conn) == 0


@pytest.mark.parametrize("field,over", [
    ("action", ""),
    ("action", "   "),
    ("action", "x" * 129),
    ("target_type", "x" * 65),
    ("target_id", "x" * 257),
    ("command_raw", "x" * 8193),
    ("result", "x" * 2049),
    ("run_id", "x" * 129),
    ("iteration", -1),
])
def test_out_of_range_fields_are_rejected(wired, field, over):
    client, conn = wired
    resp = client.post("/api/audit/append", json=_payload(**{field: over}))
    assert resp.status_code == 422, resp.text
    assert _row_count(conn) == 0


def test_client_cannot_forge_the_operator(wired):
    """operator 是归因字段，只能由服务端取 OS 用户名；多传即拒绝。"""
    client, conn = wired
    resp = client.post("/api/audit/append", json=_payload(operator="root"))
    assert resp.status_code == 422, resp.text
    assert _row_count(conn) == 0


def test_db_failure_is_a_500_not_a_lying_ok(wired, monkeypatch):
    """append_record 失败会静默返回 None；路由必须把 None 翻译成 500，而不是 ok。"""
    client, _conn_ = wired

    def _locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_get_conn", _locked)
    resp = client.post("/api/audit/append", json=_payload())
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert "审计写入失败" in detail
    assert "locked" not in json.dumps(resp.json(), ensure_ascii=False), \
        "不把 sqlite 原始串端给用户（S0-24 同口径）"
