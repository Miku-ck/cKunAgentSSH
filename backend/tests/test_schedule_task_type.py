"""定时任务双类型（运维/日常）— 数据层、REST、AI 工具契约测试。

spec 2026-10-27-schedule-task-type
"""
import asyncio

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
import routes.schedules as sched_mod
from test_schedule_routes import _FakeEngine, _fake_compute_next_run  # noqa: F401


@pytest.fixture
def client(base_db, monkeypatch):
    engine = _FakeEngine()
    monkeypatch.setattr("agent.schedule_engine.get_schedule_engine", lambda: engine)
    monkeypatch.setattr("agent.schedule_engine.compute_next_run", _fake_compute_next_run)
    app = fastapi.FastAPI()
    app.include_router(sched_mod.router, prefix="/api")
    c = TestClient(app)
    c.engine = engine  # type: ignore[attr-defined]
    return c


def _make(**over):
    data = {
        "name": "t", "schedule_type": "interval", "interval_minutes": 60,
        "time_hour": 9, "time_minute": 0, "weekday": 0, "day_of_month": 1,
        "target_host": "", "task_prompt": "p", "enabled": 1,
        "next_run_at": "2099-01-01 00:00:00",
    }
    data.update(over)
    return db.create_schedule(data)


def test_default_task_type_is_ops(base_db):
    row = _make()
    assert row["task_type"] == "ops"


def test_create_daily_task_type(base_db):
    row = _make(task_type="daily")
    assert row["task_type"] == "daily"


def test_update_cannot_change_task_type(base_db):
    row = _make(task_type="ops")
    updated = db.update_schedule(row["id"], {"task_type": "daily"})
    assert updated["task_type"] == "ops"


def test_legacy_db_gets_task_type_column(base_db):
    # 模拟旧库：建库后手动删列不现实，改为直接查列存在性
    cols = {row[1] for row in
            db._get_conn().execute("PRAGMA table_info(schedules)").fetchall()}
    db._close_all_connections()
    assert "task_type" in cols


def test_post_invalid_task_type_422(client):
    r = client.post("/api/schedules", json={"name": "x", "task_type": "weekly"})
    assert r.status_code == 422


def test_post_daily_roundtrip(client):
    r = client.post("/api/schedules", json={"name": "x", "task_type": "daily"})
    assert r.status_code == 200 and r.json()["task_type"] == "daily"


def test_post_default_type_is_ops(client):
    r = client.post("/api/schedules", json={"name": "x"})
    assert r.json()["task_type"] == "ops"


def test_patch_change_type_rejected(client):
    sid = client.post("/api/schedules", json={"name": "x"}).json()["id"]
    r = client.patch(f"/api/schedules/{sid}", json={"task_type": "daily"})
    assert r.status_code == 422


def test_patch_same_type_allowed(client):
    # 前端保存时可能带原值回填，相同类型不应报错（也不改库）
    sid = client.post("/api/schedules", json={"name": "x"}).json()["id"]
    r = client.patch(f"/api/schedules/{sid}", json={"task_type": "ops", "name": "y"})
    assert r.status_code == 200 and r.json()["task_type"] == "ops"


@pytest.fixture
def tool_env(base_db, monkeypatch):
    # create 分支会调真引擎与 compute_next_run，与 REST 测试同口径挨打补丁
    engine = _FakeEngine()
    monkeypatch.setattr("agent.schedule_engine.get_schedule_engine", lambda: engine)
    monkeypatch.setattr("agent.schedule_engine.compute_next_run", _fake_compute_next_run)
    return base_db


def test_tool_create_daily(tool_env):
    from agent.tools.builtin.ops_schedule import ScheduleOpTool
    res = asyncio.run(ScheduleOpTool().execute(
        None, "create", mode="interval", interval_minutes=60,
        task="巡检", name="d", task_type="daily"))
    assert res.success, res.error
    assert db.list_schedules()[-1]["task_type"] == "daily"


def test_tool_create_default_ops(tool_env):
    from agent.tools.builtin.ops_schedule import ScheduleOpTool
    res = asyncio.run(ScheduleOpTool().execute(
        None, "create", mode="interval", interval_minutes=60,
        task="巡检", name="o"))
    assert res.success, res.error
    assert db.list_schedules()[-1]["task_type"] == "ops"


def test_tool_create_invalid_type_errors(tool_env):
    from agent.tools.builtin.ops_schedule import ScheduleOpTool
    res = asyncio.run(ScheduleOpTool().execute(
        None, "create", mode="interval", interval_minutes=60,
        task="巡检", name="x", task_type="bogus"))
    assert not res.success and "task_type" in res.error
