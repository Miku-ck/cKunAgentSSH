"""定时任务路由契约测试 — /api/schedules CRUD + 手动触发。

历史缺陷（本轮修复，防回归）：
- PATCH 的 Optional 字段接受显式 null，`{"interval_minutes": null}` 直落
  `_validate_fields` 的 `int(None)` → 500；`name/target_host` 等同理会把 NULL
  写进 NOT NULL 列。现按"未提供"丢弃。
- DELETE 返回 `{"ok": true}`（前端 client 曾声明 `{deleted: boolean}`，已对齐）。
- 此前 /schedules 全部端点零 HTTP 测试覆盖。
"""
import os

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
import routes.schedules as sched_mod


class _FakeEngine:
    def __init__(self):
        self.triggered: list[str] = []
        # 与 triggered 平行：每次触发是否带 manual=True（手动触发不得推进 next_run）
        self.trigger_manual: list[bool] = []
        self.removed: list[str] = []
        self.started = False
        # True = 本次触发真的起了一个 run；False = 上一次仍在执行被丢弃
        self.accepts = True

    def ensure_started(self):
        self.started = True

    async def trigger(self, sched, *, manual: bool = False):
        self.triggered.append(sched["id"])
        self.trigger_manual.append(manual)
        return self.accepts

    async def remove_schedule(self, sid):
        self.removed.append(sid)
        return db.delete_schedule(sid)


@pytest.fixture
def base_db(tmp_workspace):
    db_path = os.path.join(tmp_workspace, "schedules.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


@pytest.fixture
def client(base_db, monkeypatch):
    engine = _FakeEngine()
    monkeypatch.setattr(sched_mod, "get_schedule_engine", lambda: engine)
    app = fastapi.FastAPI()
    app.include_router(sched_mod.router, prefix="/api")
    c = TestClient(app)
    c.engine = engine  # type: ignore[attr-defined]
    return c


def _create(client, **over):
    body = {"name": "每日巡检", "schedule_type": "daily", "time_hour": 9, "task_prompt": "检查磁盘"}
    body.update(over)
    resp = client.post("/api/schedules", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_returns_row_with_future_next_run(client):
    row = _create(client)
    assert row["name"] == "每日巡检"
    assert row["next_run_at"]
    assert client.engine.started is True
    listed = client.get("/api/schedules").json()
    assert [r["id"] for r in listed] == [row["id"]]


def test_create_rejects_unknown_type(client):
    resp = client.post("/api/schedules", json={"name": "x", "schedule_type": "cron"})
    assert resp.status_code == 400


def test_patch_explicit_nulls_treated_as_absent(client):
    """回归：null 曾让 int(None) 抛 ValueError → 500。"""
    row = _create(client, schedule_type="interval", interval_minutes=30)
    resp = client.patch(
        f"/api/schedules/{row['id']}",
        json={"interval_minutes": None, "time_hour": None, "name": None, "enabled": None},
    )
    assert resp.status_code == 200, resp.text
    after = db.get_schedule(row["id"])
    # 值不变，且没有把 NULL 写进去
    assert after["interval_minutes"] == 30
    assert after["name"] == "每日巡检"
    assert after["enabled"] == row["enabled"]


def test_patch_toggle_enabled_does_not_reschedule(client):
    row = _create(client)
    resp = client.patch(f"/api/schedules/{row['id']}", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] == 0
    assert resp.json()["next_run_at"] == row["next_run_at"]


def test_patch_reenable_recomputes_next_run(client):
    """停用后恢复必须重算：带着过期 next_run_at 启用会在下一个 tick 立即补跑"""
    row = _create(client, schedule_type="interval", interval_minutes=30)
    assert client.patch(f"/api/schedules/{row['id']}", json={"enabled": False}).status_code == 200
    # 模拟停用多日：next_run_at 已过期（同一秒内重算的时间戳与原值相同，
    # 不能靠"晚于创建时刻"断言重算发生）
    stale = "2020-01-01 00:00:00"
    db.set_schedule_next_run(row["id"], stale)
    resp = client.patch(f"/api/schedules/{row['id']}", json={"enabled": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] == 1
    assert body["next_run_at"] > stale
    assert body["next_run_at"] == db.get_schedule(row["id"])["next_run_at"]


def test_patch_schedule_param_change_recomputes_next_run(client):
    row = _create(client, schedule_type="interval", interval_minutes=30)
    resp = client.patch(f"/api/schedules/{row['id']}", json={"interval_minutes": 120})
    assert resp.status_code == 200
    body = resp.json()
    # 回归：响应体曾是写 next_run_at 之前取的行 —— 返回旧下次时间
    assert body["next_run_at"] != row["next_run_at"]
    assert body["next_run_at"] == db.get_schedule(row["id"])["next_run_at"]
    assert body["interval_minutes"] == 120


def test_patch_out_of_range_rejected(client):
    row = _create(client)
    assert client.patch(f"/api/schedules/{row['id']}", json={"time_hour": 24}).status_code == 400
    assert client.patch(f"/api/schedules/{row['id']}", json={"interval_minutes": 0}).status_code == 422


def test_delete_then_404(client):
    row = _create(client)
    resp = client.delete(f"/api/schedules/{row['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # 删除必须走 engine.remove_schedule（取消在途执行体 + 落定 running 行）
    assert client.engine.removed == [row["id"]]
    assert client.delete(f"/api/schedules/{row['id']}").status_code == 404
    assert client.get("/api/schedules").json() == []


def test_run_now_triggers_engine(client):
    row = _create(client)
    resp = client.post(f"/api/schedules/{row['id']}/run")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "schedule_id": row["id"]}
    assert client.engine.triggered == [row["id"]]
    # 手动触发必须 manual=True：不推进 next_run_at，不吞掉本次定时
    assert client.engine.trigger_manual == [True]


def test_run_now_conflict_is_not_reported_as_success(client):
    """engine 丢弃重复触发时不得回 ok:true —— 前端据此弹「已触发立即执行」"""
    row = _create(client)
    client.engine.accepts = False
    resp = client.post(f"/api/schedules/{row['id']}/run")
    assert resp.status_code == 409
    assert "正在执行" in resp.json()["detail"]
    assert client.engine.triggered == [row["id"]]


def test_name_length_bounded(client):
    """名称随每 3s 一轮的列表响应整份下发，不设上限等于允许贴一篇文档"""
    resp = client.post("/api/schedules", json={"name": "x" * 101, "schedule_type": "daily"})
    assert resp.status_code == 422


def test_unknown_ids_return_404(client):
    assert client.patch("/api/schedules/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/schedules/nope").status_code == 404
    assert client.post("/api/schedules/nope/run").status_code == 404
    assert client.get("/api/schedules/nope/runs").status_code == 404
