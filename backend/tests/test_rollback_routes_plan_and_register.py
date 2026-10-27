"""回滚路由契约测试 — plan 组装 / register 幂等 / undo-actions（功能 B）。

守护点（设计文档 §2.5）：
- plan：LIFO 序、manual 项被排除、task_text 内嵌 source_run_id / host /
  conversation_id；
- **task_text 长度有上限，响应与 items 均不含 snapshot_b64（大字段不出库）**；
- 非终态 run / 无记录 → 4xx；register 幂等（重复同对不重复插行）；
- undo-actions：无记录 404、响应不含 snapshot_b64、含 warning/terminal；
- 外部形态诡异的 run id 走参数绑定路径（404 而非 500）。
"""
import base64
import os

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
import undo_store
import routes.rollbacks as rb_mod


@pytest.fixture
def undo_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "rollback-routes.db"))
    yield
    db._close_all_connections()


@pytest.fixture
def client(undo_db):
    app = fastapi.FastAPI()
    app.include_router(rb_mod.router, prefix="/api")
    return TestClient(app)


def _mk_run(run_id: str, status: str = "completed") -> str:
    """建会话+run 行，返回会话 id（agent_runs.conversation_id 挂 FK，父行先建）"""
    conv_id = str(db.create_conversation(title="t", mode="ssh")["id"])
    db.create_agent_run(run_id, conv_id, task="t")
    db.update_agent_run(run_id, status)
    return conv_id


def _record(run_id: str, rows: list[dict], run_status: str = "completed") -> None:
    assert undo_store._record_rows_sync(run_id, rows, run_status) is True


def _shell_row(seq: int, command: str = "mkdir /tmp/a", reverse: str = "rmdir /tmp/a") -> dict:
    return {"seq": seq, "kind": "shell", "tool": "shell_exec", "command": command,
            "reverse_command": reverse, "path": None, "snapshot_b64": None,
            "existed": None, "reason": None, "confidence": "derived",
            "ssh_session_id": "sess-old", "host": "web-01", "environment": "test"}


def _file_row(seq: int, path: str = "/etc/app.conf", existed: bool = True,
              snapshot: bytes = b"cfg", confidence: str = "snapshot") -> dict:
    return {"seq": seq, "kind": "file", "tool": "file_write", "command": "",
            "reverse_command": None, "path": path,
            "snapshot_b64": base64.b64encode(snapshot).decode() if snapshot else None,
            "existed": existed, "reason": None, "confidence": confidence,
            "ssh_session_id": "sess-old", "host": "web-01", "environment": "test"}


def _noop_row(seq: int) -> dict:
    return {"seq": seq, "kind": "noop", "tool": "file_read", "command": "",
            "reverse_command": None, "path": None, "snapshot_b64": None,
            "existed": None, "reason": "readonly", "confidence": "manual",
            "ssh_session_id": "sess-old", "host": "web-01", "environment": "test"}


def _assert_no_snapshot(payload) -> None:
    """响应任何角落都不许出现 snapshot_b64（大字段不出库）"""
    if isinstance(payload, dict):
        assert "snapshot_b64" not in payload
        for v in payload.values():
            _assert_no_snapshot(v)
    elif isinstance(payload, list):
        for v in payload:
            _assert_no_snapshot(v)


# ── GET undo-actions ──

def test_undo_actions_shape_and_404s(client, undo_db):
    resp = client.get("/api/rollback/runs/run-x/undo-actions")
    assert resp.status_code == 404  # run 不存在

    _mk_run("run-1")
    resp = client.get("/api/rollback/runs/run-1/undo-actions")
    assert resp.status_code == 404  # run 存在但无 undo 记录

    _record("run-1", [_shell_row(0), _file_row(1), _noop_row(2)])
    resp = client.get("/api/rollback/runs/run-1/undo-actions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_no_snapshot(body)
    assert body["run_id"] == "run-1" and body["run_status"] == "completed" and body["terminal"] is True
    assert [it["seq"] for it in body["items"]] == [0, 1, 2]
    file_item = body["items"][1]
    assert file_item["confidence"] == "snapshot" and file_item["path"] == "/etc/app.conf"
    assert file_item["warning"] == ""  # 内容级还原无删除动作


def test_undo_actions_delete_created_warning(client, undo_db):
    _mk_run("run-dc")
    _record("run-dc", [_file_row(0, path="/tmp/new.txt", existed=False, snapshot=None,
                                confidence="delete_created")])
    body = client.get("/api/rollback/runs/run-dc/undo-actions").json()
    assert body["items"][0]["warning"]  # 反向是删除 → 必须有警示语
    _assert_no_snapshot(body)


# ── POST plan ──

def test_plan_lifo_manual_excluded_and_ids_embedded(client, undo_db):
    conv_id = _mk_run("run-p", status="completed")
    _record("run-p", [
        _noop_row(0),            # manual：排除
        _shell_row(1),           # derived：进计划
        _file_row(2),            # snapshot：进计划
        _shell_row(3, command="touch /tmp/b", reverse="rm /tmp/b"),  # derived：进计划
    ])
    resp = client.post("/api/rollback/plan", json={"source_run_id": "run-p"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_no_snapshot(body)
    assert body["source_run_id"] == "run-p"
    assert body["conversation_id"] == conv_id
    assert body["host"] == "web-01" and body["environment"] == "test"
    assert body["ssh_session_hint"] == "sess-old"
    # LIFO：seq 倒序
    assert [it["seq"] for it in body["items"]] == [3, 2, 1]
    text = body["task_text"]
    assert "run-p" in text
    assert "web-01" in text
    assert conv_id not in text  # conversation_id 不必进任务书，但响应里必须有
    assert body["conversation_id"] == conv_id
    # file 项只写 (run_id, seq) 引用，快照内容留给 undo_restore
    assert 'undo_restore(run_id="run-p", seq=2)' in text
    # 逐项带可信度与反向命令
    assert "快照还原" in text or "snapshot" in text
    assert "rmdir /tmp/a" in text
    # LIFO 顺序在文本里可辨：第 1 项是 seq=3
    assert text.index("[seq=3]") < text.index("[seq=2]") < text.index("[seq=1]")
    # manual 的 noop 项不进计划
    assert "seq=0" not in text


def test_plan_long_reverse_command_not_truncated(client, undo_db):
    """反向命令是模型唯一的执行输入（undo_restore 拒 shell 项、无别的读通道）：
    task_text 必须携带全量反向命令，绝不截断 —— 截断版原样执行会 mv/rm 到
    截断前缀的错误路径。原命令（仅展示）允许截断。"""
    long_reverse = "mv " + "/very/deep/dir/" + "y" * 500
    _mk_run("run-longrev")
    _record("run-longrev", [_shell_row(0, command="mv " + "x" * 500 + " " + "y" * 500,
                                    reverse=long_reverse)])
    resp = client.post("/api/rollback/plan", json={"source_run_id": "run-longrev"})
    assert resp.status_code == 200, resp.text
    text = resp.json()["task_text"]
    assert long_reverse in text          # 反向命令全量下发
    # 原命令（仅展示用）被截断且有截断标记 —— 证明两条通道语义分开
    assert "截断展示" in text


def test_plan_rejects_non_terminal_run(client, undo_db):
    _mk_run("run-running", status="running")
    _record("run-running", [_shell_row(0)])
    resp = client.post("/api/rollback/plan", json={"source_run_id": "run-running"})
    assert resp.status_code == 400
    assert "running" in resp.json()["detail"]


def test_plan_failed_run_not_rollbackable(client, undo_db):
    """FAILED run 的条目已 auto_rolled_back：plan 无 pending 项 → 404"""
    _mk_run("run-failed", status="failed")
    _record("run-failed", [_shell_row(0)], run_status="failed")
    resp = client.post("/api/rollback/plan", json={"source_run_id": "run-failed"})
    assert resp.status_code in (400, 404)  # failed 直接被终态闸拒绝


def test_plan_no_rollbackable_items_404(client, undo_db):
    _mk_run("run-manual")
    _record("run-manual", [_noop_row(0)])
    resp = client.post("/api/rollback/plan", json={"source_run_id": "run-manual"})
    assert resp.status_code == 404


def test_plan_task_text_length_cap(client, undo_db):
    """超长回滚任务 → 400（不静默截断、不让超大 run 撑爆回滚任务书）"""
    _mk_run("run-huge")
    rows = [_shell_row(i, command="x" * 400, reverse="y" * 400) for i in range(80)]
    _record("run-huge", rows)
    resp = client.post("/api/rollback/plan", json={"source_run_id": "run-huge"})
    assert resp.status_code == 400
    assert "超上限" in resp.json()["detail"]


def test_plan_exotic_run_id_hits_parameterized_404(client, undo_db):
    """注入形态的 run id：走参数绑定路径 → 404，不 500、不影响库"""
    weird = "run'; DROP TABLE run_undo_actions; --"
    resp = client.post("/api/rollback/plan", json={"source_run_id": weird})
    assert resp.status_code == 404
    # 表健在且没有行（顺带经 store 读取路径确认 schema 可用）
    assert undo_store.get_undo_actions(weird) == []


# ── POST register ──

def test_register_idempotent_and_validates_runs(client, undo_db):
    _mk_run("src-9")
    _mk_run("rbk-9")
    resp = client.post("/api/rollback/register", json={"source_run_id": "src-9", "rollback_run_id": "rbk-9"})
    assert resp.status_code == 200
    first = resp.json()
    assert first["source_run_id"] == "src-9" and first["rollback_run_id"] == "rbk-9"
    # 幂等：重复登记返回同一行，不重复插
    second = client.post("/api/rollback/register", json={"source_run_id": "src-9", "rollback_run_id": "rbk-9"}).json()
    assert second["id"] == first["id"]
    assert len(undo_store.list_rollbacks_for_source("src-9")) == 1
    # 未登记的 run → 404
    resp = client.post("/api/rollback/register", json={"source_run_id": "nope", "rollback_run_id": "rbk-9"})
    assert resp.status_code == 404
    # 同 id 自滚 → 400
    resp = client.post("/api/rollback/register", json={"source_run_id": "src-9", "rollback_run_id": "src-9"})
    assert resp.status_code == 400
