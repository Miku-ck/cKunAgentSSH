"""清空执行历史契约 — DELETE /schedules/{id}/runs 与 /runbooks/{id}/runs

spec 2026-10-27-clear-history-runs（新增功能，破坏性操作）钉：
- 只删本条任务/剧本的历史行，保留任务/剧本本身（与 remove 删整实体区分开）；
- 不误伤别的任务/剧本的历史（按 id 隔离）；
- 返回删除计数正确；
- 有 running 行时 409 拒绝且不删（进行中闸）；
- 未知 id → 404。
"""

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
import runbook_store as rstore
import routes.runbooks as rb_mod
import routes.schedules as sched_mod


@pytest.fixture
def sched_client(runbook_db):
    app = fastapi.FastAPI()
    app.include_router(sched_mod.router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def rb_client(runbook_db):
    app = fastapi.FastAPI()
    app.include_router(rb_mod.router, prefix="/api")
    return TestClient(app)


def _mk_schedule(name="任务"):
    return db.create_schedule({"name": name, "task_type": "ops"})["id"]


# ── schedule 域 ────────────────────────────────────────────────────────────────

def test_clear_schedule_runs_只删本条并返回计数(runbook_db):
    s1 = _mk_schedule("s1")
    s2 = _mk_schedule("s2")
    for _ in range(3):
        r = db.create_schedule_run(s1, run_id="")
        db.update_schedule_run(r["id"], status="completed")
    for _ in range(2):
        r = db.create_schedule_run(s2, run_id="")
        db.update_schedule_run(r["id"], status="completed")

    deleted = db.clear_schedule_runs(s1)
    assert deleted == 3
    assert db.list_schedule_runs(s1, limit=50) == []          # 本条清空
    assert len(db.list_schedule_runs(s2, limit=50)) == 2      # 别条不受影响
    assert db.get_schedule(s1) is not None                    # 任务本身保留


def test_schedule_route_清空成功(sched_client, runbook_db):
    s = _mk_schedule()
    r = db.create_schedule_run(s, run_id="")
    db.update_schedule_run(r["id"], status="failed")

    resp = sched_client.delete(f"/api/schedules/{s}/runs")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": 1}
    assert db.list_schedule_runs(s, limit=50) == []


def test_schedule_route_有进行中则_409_且不删(sched_client, runbook_db):
    s = _mk_schedule()
    db.create_schedule_run(s, run_id="")  # 默认 running

    resp = sched_client.delete(f"/api/schedules/{s}/runs")
    assert resp.status_code == 409
    assert len(db.list_schedule_runs(s, limit=50)) == 1        # 一行没删


def test_schedule_route_未知任务_404(sched_client, runbook_db):
    assert sched_client.delete("/api/schedules/no-such/runs").status_code == 404


# ── runbook 域 ─────────────────────────────────────────────────────────────────

def _mk_runbook(name="剧本"):
    return rstore.create_runbook({"name": name, "steps": [{"command": "uptime"}]})["id"]


def test_clear_runbook_runs_只删本条并返回计数(runbook_db):
    b1 = _mk_runbook("b1")
    b2 = _mk_runbook("b2")
    for _ in range(2):
        r = rstore.create_runbook_run(b1)
        rstore.finish_runbook_run(r["id"], "success")
    r = rstore.create_runbook_run(b2)
    rstore.finish_runbook_run(r["id"], "success")

    deleted = rstore.clear_runbook_runs(b1)
    assert deleted == 2
    assert rstore.list_runbook_runs(b1, limit=50) == []
    assert len(rstore.list_runbook_runs(b2, limit=50)) == 1
    assert rstore.get_runbook(b1) is not None


def test_runbook_route_清空成功(rb_client, runbook_db):
    b = _mk_runbook()
    r = rstore.create_runbook_run(b)
    rstore.finish_runbook_run(r["id"], "failed")

    resp = rb_client.delete(f"/api/runbooks/{b}/runs")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": 1}
    assert rstore.list_runbook_runs(b, limit=50) == []


def test_runbook_route_有进行中则_409_且不删(rb_client, runbook_db):
    b = _mk_runbook()
    rstore.create_runbook_run(b)  # 默认 running

    resp = rb_client.delete(f"/api/runbooks/{b}/runs")
    assert resp.status_code == 409
    assert len(rstore.list_runbook_runs(b, limit=50)) == 1


def test_runbook_route_未知剧本_404(rb_client, runbook_db):
    assert rb_client.delete("/api/runbooks/no-such/runs").status_code == 404
