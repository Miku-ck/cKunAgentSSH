"""runbook 路由契约守卫 — steps 形状/上限 400 / 执行中 409 / 桥探活失败 503 /
历史 limit 夹取 / SQL 全参数绑定（注入串原样存取且表结构不受影响）。"""
import os

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
import runbook_store as rstore
import routes.runbooks as rb_mod
from agent.electron_bridge import BridgeErrorCode


@pytest.fixture
def base_db(tmp_workspace):
    db_path = os.path.join(tmp_workspace, "runbook_routes.db")
    db._close_all_connections()
    db.init_db(db_path)
    rstore._reset_memo_for_tests()
    yield db_path
    db._close_all_connections()


class _FakeEngine:
    def __init__(self):
        self.executing: set[str] = set()
        self.deleting: set[str] = set()
        self.triggered: list[str] = []
        self.reaped = False
        # 下一次 trigger 返回的 runbook_run_id；None = 模拟「正在执行」被丢弃
        self.next_rrb: str | None = "rrb-1"

    async def ensure_reaped(self):
        self.reaped = True

    def begin_delete(self, runbook_id):
        if runbook_id in self.executing or runbook_id in self.deleting:
            return False
        self.deleting.add(runbook_id)
        return True

    def end_delete(self, runbook_id):
        self.deleting.discard(runbook_id)

    def is_executing(self, runbook_id):
        return runbook_id in self.executing

    async def trigger(self, runbook):
        # 删除占位窗口内拒绝触发（与真引擎同步检查段同语义）
        if runbook["id"] in self.deleting:
            return None
        self.triggered.append(runbook["id"])
        if self.next_rrb is None:
            return None
        return self.next_rrb


@pytest.fixture
def client(base_db, monkeypatch):
    engine = _FakeEngine()
    # routes/runbooks.py 在函数内懒导入 runbook_engine.get_runbook_engine：
    # 必须补丁源头模块，否则函数内重新 import 拿回真值（test_schedule_routes 同款手法）
    monkeypatch.setattr("runbook_engine.get_runbook_engine", lambda: engine)
    app = fastapi.FastAPI()
    app.include_router(rb_mod.router, prefix="/api")
    c = TestClient(app)
    c.engine = engine  # type: ignore[attr-defined]
    return c


def _body(**over):
    body = {
        "name": "nginx 恢复",
        "description": "502 时恢复上游",
        "target_host": "conn-1",
        "steps": [
            {"command": "systemctl restart nginx", "assertCommand": "is-active", "note": ""},
        ],
        "enabled": True,
    }
    body.update(over)
    return body


def _create(client, **over):
    resp = client.post("/api/runbooks", json=_body(**over))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── CRUD 与 steps 校验 ───────────────────────────────────────────────────────

def test_create_and_list_roundtrip(client):
    row = _create(client)
    assert row["id"]
    assert row["steps"][0]["command"] == "systemctl restart nginx"
    listed = client.get("/api/runbooks").json()
    assert [r["id"] for r in listed] == [row["id"]]
    assert listed[0]["steps"][0]["assertCommand"] == "is-active"
    assert listed[0]["last_status"] is None
    # 打开列表即收尸（重启后僵尸 running 在首屏被落终态，按钮不再永久置灰）
    assert client.engine.reaped is True


def test_steps_validation_returns_400(client):
    # 空步骤数组
    resp = client.post("/api/runbooks", json=_body(steps=[]))
    assert resp.status_code == 400
    # 超过 20 步
    resp = client.post("/api/runbooks", json=_body(
        steps=[{"command": f"echo {i}", "assertCommand": "", "note": ""} for i in range(21)]))
    assert resp.status_code == 400
    assert "20" in resp.json()["detail"]
    # 空命令
    resp = client.post("/api/runbooks", json=_body(
        steps=[{"command": "   ", "assertCommand": "", "note": ""}]))
    assert resp.status_code == 400
    # 命令超长（2001）
    resp = client.post("/api/runbooks", json=_body(
        steps=[{"command": "x" * 2001, "assertCommand": "", "note": ""}]))
    assert resp.status_code == 400
    # note 超长（501）
    resp = client.post("/api/runbooks", json=_body(
        steps=[{"command": "ok", "assertCommand": "", "note": "n" * 501}]))
    assert resp.status_code == 400
    # assertCommand 超长
    resp = client.post("/api/runbooks", json=_body(
        steps=[{"command": "ok", "assertCommand": "a" * 2001, "note": ""}]))
    assert resp.status_code == 400


def test_patch_partial_and_validation(client):
    row = _create(client)
    # 局部更新：只发改名，不动其他字段
    resp = client.patch(f"/api/runbooks/{row['id']}", json={"name": "改名"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "改名"
    assert body["target_host"] == "conn-1"
    assert [s["command"] for s in body["steps"]] == ["systemctl restart nginx"]
    # 显式空串是合法值（清空说明）
    resp = client.patch(f"/api/runbooks/{row['id']}", json={"description": ""})
    assert resp.status_code == 200
    assert resp.json()["description"] == ""
    # 空 PATCH 名被拒（S7-11 同款：PATCH 不得绕过 POST 的校验；Pydantic min_length
    # 先行拦下 → 422，语义与 routes/schedules.py 的 PATCH 一致）
    resp = client.patch(f"/api/runbooks/{row['id']}", json={"name": ""})
    assert resp.status_code in (400, 422)
    # PATCH steps 也要过形状校验
    resp = client.patch(f"/api/runbooks/{row['id']}", json={"steps": []})
    assert resp.status_code == 400
    # 不存在的剧本
    resp = client.patch("/api/runbooks/no-such", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_404_and_conflict(client):
    resp = client.delete("/api/runbooks/no-such")
    assert resp.status_code == 404
    row = _create(client)
    client.engine.executing.add(row["id"])
    resp = client.delete(f"/api/runbooks/{row['id']}")
    assert resp.status_code == 409
    client.engine.executing.clear()
    resp = client.delete(f"/api/runbooks/{row['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_run_refused_during_delete_window(client, monkeypatch):
    """评审意见 3 回归：DELETE 的检查→删除之间存在 await 窗口，占位（begin_delete）
    之后、落库之前的并发 POST /run 必须被拒 —— 否则刚建的 running 行会被
    FK CASCADE 静默吞掉，执行体对着已删剧本跑完、历史凭空消失。"""
    row = _create(client)
    import agent.electron_bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "ElectronBridge", _UpBridge)

    assert client.engine.begin_delete(row["id"]) is True
    # 占位窗口内触发：engine.trigger 因 _deleting 拒绝 → 路由回 409，不建 run 行
    resp = client.post(f"/api/runbooks/{row['id']}/run")
    assert resp.status_code == 409
    assert client.engine.triggered == []

    client.engine.end_delete(row["id"])
    resp = client.post(f"/api/runbooks/{row['id']}/run")
    assert resp.status_code == 200


# ── 一键执行：404 / 503 / 409 / 触发返回历史行 id ────────────────────────────

def test_run_404(client):
    resp = client.post("/api/runbooks/no-such/run")
    assert resp.status_code == 404


def test_run_bridge_unreachable_returns_503(client, monkeypatch):
    row = _create(client)

    class _DownBridge:
        async def probe(self):
            return BridgeErrorCode.WS_LOST

    import agent.electron_bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "ElectronBridge", _DownBridge)
    resp = client.post(f"/api/runbooks/{row['id']}/run")
    assert resp.status_code == 503
    assert "桥当前不可达" in resp.json()["detail"]
    assert client.engine.triggered == []


class _UpBridge:
    """探活恒可达（触发主链的 404/409 用例需要绕过 503 前置）。"""

    async def probe(self):
        return None


def test_run_conflict_when_executing(client, monkeypatch):
    row = _create(client)
    import agent.electron_bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "ElectronBridge", _UpBridge)
    client.engine.next_rrb = None  # 模拟上一次还在跑
    resp = client.post(f"/api/runbooks/{row['id']}/run")
    assert resp.status_code == 409


def test_run_returns_runbook_run_id(client, monkeypatch):
    row = _create(client)
    import agent.electron_bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "ElectronBridge", _UpBridge)
    resp = client.post(f"/api/runbooks/{row['id']}/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["runbook_run_id"] == "rrb-1"
    assert client.engine.triggered == [row["id"]]


# ── 执行历史：404 + limit 夹取 ───────────────────────────────────────────────

def test_runs_404(client):
    resp = client.get("/api/runbooks/no-such/runs")
    assert resp.status_code == 404


def test_runs_limit_clamped(client, monkeypatch):
    row = _create(client)
    seen: list[int] = []

    def fake_list(runbook_id, limit=50):
        seen.append(limit)
        return []

    monkeypatch.setattr(rstore, "list_runbook_runs", fake_list)
    client.get(f"/api/runbooks/{row['id']}/runs?limit=9999")
    client.get(f"/api/runbooks/{row['id']}/runs?limit=0")
    client.get(f"/api/runbooks/{row['id']}/runs?limit=3")
    client.get(f"/api/runbooks/{row['id']}/runs")
    assert seen == [200, 1, 3, 50]


def test_runs_returns_parsed_steps_state(client):
    row = _create(client)
    rrb = rstore.create_runbook_run(row["id"], run_id="run-1")
    rstore.update_runbook_run(
        rrb["id"], steps_state=[{"seq": 0, "status": "success", "command": "echo"}])
    rstore.finish_runbook_run(rrb["id"], "success", diagnosis="无异常")
    resp = client.get(f"/api/runbooks/{row['id']}/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert runs[0]["status"] == "success"
    assert runs[0]["steps_state"][0]["command"] == "echo"
    assert runs[0]["diagnosis"] == "无异常"
    # 列表侧 last_run 摘要同步可见（前端 isRunning 判据）
    listed = client.get("/api/runbooks").json()
    assert listed[0]["last_status"] == "success"


# ── 参数绑定铁律：注入串原样存取 ─────────────────────────────────────────────

def test_hostile_input_stays_bound_and_table_alive(client):
    hostile = "x'; DROP TABLE runbooks;-- %_% ' OR '1'='1"
    row = _create(client, name=hostile, description="100% 纯_文本")
    got = client.get("/api/runbooks").json()
    assert any(r["name"] == hostile for r in got)
    assert any(r["description"] == "100% 纯_文本" for r in got)
    # 表结构未被动过：仍能正常写入
    resp = client.post("/api/runbooks", json=_body(name="still alive"))
    assert resp.status_code == 200
