# -*- coding: utf-8 -*-
"""S10-21 / S10-19 回归 — 超时报错要说准哪一相到点，服务端故障不许冒充输入错误。

S10-21：`http_timeout` 把 connect 夹到 min(10,t)、read 抬到 max(60,t)，而旧文案报的是
用户设定的那个标量 —— 用户设 5s、端点连上却慢回时报「连接超时 (5s)」，真正到点的是
60s，用户会按 5s 去查网络。且「连不上」与「连上了没回应」被压成同一句话。
S10-19：create_model_config 把**任何**异常折成 400 + str(e)，于是 "database is locked"
在面板上显示成「你填的东西不对」。
"""
import sqlite3

import httpx
import pytest

import netconfig


@pytest.fixture
def clean_policy():
    saved = dict(netconfig._policy)
    saved_loaded = netconfig._loaded
    netconfig._policy = {"proxy_url": "", "timeout_seconds": 0, "retry_count": 1}
    netconfig._loaded = True
    yield netconfig
    netconfig._policy = saved
    netconfig._loaded = saved_loaded


# ── S10-21：上限的单一定义点 ─────────────────────────────────────────────────

def test_bounds_follow_call_site_default_when_user_unset(clean_policy):
    assert clean_policy.effective_timeout_bounds(10.0) == (10.0, 10.0)


def test_bounds_report_the_real_phases_when_user_set(clean_policy):
    # 用户设 5s：connect=min(10,5)=5，read=max(60,5)=60 —— 报 5 就是假信号
    clean_policy.set_policy("", 5, 1)
    assert clean_policy.effective_timeout_bounds(10.0) == (5.0, 60.0)


def test_bounds_cap_connect_at_ten(clean_policy):
    clean_policy.set_policy("", 600, 1)
    assert clean_policy.effective_timeout_bounds(10.0) == (10.0, 600.0)


# ── S10-21：路由文案 ─────────────────────────────────────────────────────────

class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        raise self._exc


@pytest.fixture
def policy_env(tmp_workspace):
    import os

    import database as db
    import routes.settings as rs

    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "s1021.db"))
    cfg = db.create_model_config(name="Qwen", base_url="http://vllm.local:8000/v1",
                                model_id="qwen3")
    yield rs, cfg["id"]
    db._close_all_connections()


def _run(monkeypatch, policy_env, clean_policy, exc, user_timeout):
    import asyncio

    rs, cfg_id = policy_env
    monkeypatch.setattr(netconfig, "new_client",
                        lambda **kw: _RaisingClient(exc))
    clean_policy.set_policy("", user_timeout, 1)
    return asyncio.run(rs.test_model_connection(cfg_id))


def test_connect_timeout_says_connect_and_reports_it(monkeypatch, policy_env,
                                                     clean_policy):
    out = _run(monkeypatch, policy_env, clean_policy,
               httpx.ConnectTimeout("connect timed out"), 5)
    assert out["status"] == "error"
    assert "建立连接" in out["detail"], out["detail"]
    assert out["timeout_seconds"] == 5, "connect 相的真实上限是 min(10,5)=5"
    assert "读取" not in out["detail"]


def test_read_timeout_reports_the_floored_read_not_the_user_scalar(monkeypatch,
                                                                   policy_env,
                                                                   clean_policy):
    """核心断言：用户设 5s，read 实际到点的是 60s —— 文案不许再报 5。"""
    out = _run(monkeypatch, policy_env, clean_policy,
               httpx.ReadTimeout("read timed out"), 5)
    assert out["status"] == "error"
    assert out["timeout_seconds"] == 60, (
        f"报了 {out['timeout_seconds']}s：netconfig 把 read 抬到 max(60,t)，"
        f"按 5s 报会让用户查错方向")
    assert "60" in out["detail"]
    assert "未在时限内响应" in out["detail"]


# ── S10-19：状态码归位 ───────────────────────────────────────────────────────

@pytest.fixture
def models_client(tmp_workspace):
    import os

    import fastapi
    from fastapi.testclient import TestClient

    import database as db
    import routes.settings as rs

    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "s1019.db"))
    app = fastapi.FastAPI()
    app.include_router(rs.router, prefix="/api")
    yield TestClient(app)
    db._close_all_connections()


_BODY = {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
         "model_id": "deepseek-chat"}


def test_server_side_failure_is_500_not_400(monkeypatch, models_client):
    import routes.settings as rs

    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rs.db, "create_model_config", _locked)
    resp = models_client.post("/api/settings/models", json=_BODY)
    assert resp.status_code == 500, (
        f"运维故障被报成 {resp.status_code} —— 面板会把它显示成输入错误，"
        f"用户只会反复改表单")
    assert "database is locked" in resp.json()["detail"]


def test_genuine_input_error_still_422(monkeypatch, models_client):
    """改成 500 不能顺手把真正的入参校验也放掉（Field 约束先于 handler）。"""
    resp = models_client.post("/api/settings/models",
                              json={**_BODY, "temperature": 99.0})
    assert resp.status_code == 422


def test_happy_path_still_creates(models_client):
    resp = models_client.post("/api/settings/models", json=_BODY)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "DeepSeek"
