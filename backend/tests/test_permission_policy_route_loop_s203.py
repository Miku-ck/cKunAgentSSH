"""S2-03 回归 — 改进程内审批运行时状态的路由必须是 async def（跑在事件循环上）。

同步 `def` 路由会被 FastAPI 丢进 anyio 线程池，于是三个运行时 mutator 在非事件循环
线程上改全局状态。最重的一条是 `set_allowlist_ttl_hours`：它直接动进程级
PermissionGate 单例并调用 `prune_expired_allowlist()`（删键 + 落 allowlist 文件），
而同一时刻循环上的 check_permission/grant 正在读同一批状态。同族的
grant/revoke/release-run/deny-command 四条端点早就为消掉这一类竞态改成 async def，
口径写在 routes/agent.py 的 grant docstring 里（「回到循环后互斥天然成立」）。
"""
import ast
import inspect
from pathlib import Path

import pytest

import routes.settings as rs

SETTINGS_ROUTES = (Path(__file__).resolve().parents[1] / "routes" / "settings.py")

# 事件循环也会读、且必须由循环自己写的进程内状态
RUNTIME_MUTATORS = {
    "set_allowlist_ttl_hours",
    "set_db_enabled",
    "set_approval_fallback_seconds",
}


def _router_handlers() -> list[ast.FunctionDef]:
    tree = ast.parse(SETTINGS_ROUTES.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "router"
                    and dec.func.attr in ("put", "post", "delete", "patch")):
                out.append(node)
    return out


def test_every_handler_touching_runtime_state_is_async():
    """守卫本身：下一个「同族漏一处」的路由会在这条测试里暴露，而不是靠注释提醒。"""
    offenders = []
    for fn in _router_handlers():
        called = {
            n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if called & RUNTIME_MUTATORS and not isinstance(fn, ast.AsyncFunctionDef):
            offenders.append((fn.name, sorted(called & RUNTIME_MUTATORS)))
    assert not offenders, (
        f"这些路由在 anyio 线程池里改进程内审批运行时状态，与事件循环上的 gate "
        f"消费方无锁并发：{offenders}")


def test_permission_policy_put_is_a_coroutine_function():
    assert inspect.iscoroutinefunction(rs.update_permission_policy), (
        "update_permission_policy 退回同步 def 了 —— 它会在线程池里 prune allowlist")


@pytest.fixture
def policy_client(tmp_workspace):
    import os

    import fastapi
    from fastapi.testclient import TestClient

    import database as db
    from agent.harness.reins import auto_approve, core, gate

    saved = (gate._ALLOWLIST_TTL_HOURS, auto_approve.AUTO_APPROVE_ENABLED,
             core._approval_fallback_s)
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "s203.db"))
    app = fastapi.FastAPI()
    app.include_router(rs.router, prefix="/api")
    yield TestClient(app), db
    db._close_all_connections()
    gate._ALLOWLIST_TTL_HOURS, auto_approve.AUTO_APPROVE_ENABLED, \
        core._approval_fallback_s = saved


def test_put_applies_values_to_library_and_runtime(policy_client):
    """改 async 之后功能不变：库里落新值，运行时也立刻跟上（不重启就生效）。"""
    from agent.harness.reins import auto_approve, core, gate

    client, _ = policy_client
    resp = client.put("/api/settings/permission-policy", json={
        "allowlist_ttl_hours": 24,
        "auto_approve_enabled": False,
        "approval_fallback_s": 90,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allowlist_ttl_hours"] == 24
    assert body["auto_approve_enabled"] is False
    assert body["approval_fallback_s"] == 90
    assert gate._ALLOWLIST_TTL_HOURS == 24
    assert auto_approve.AUTO_APPROVE_ENABLED is False
    assert core._approval_fallback_s == 90.0


def test_partial_put_leaves_other_keys_untouched(policy_client):
    """「不传 = 不改」的口径不能因为改 async 而退化（请求模型全字段 Optional）。"""
    client, _ = policy_client
    client.put("/api/settings/permission-policy", json={"allowlist_ttl_hours": 48})
    resp = client.put("/api/settings/permission-policy",
                      json={"approval_fallback_s": 30})
    assert resp.json()["allowlist_ttl_hours"] == 48


def test_runtime_not_touched_when_db_write_fails(policy_client):
    """落库失败必须整体回滚且不推运行时 —— 否则库里旧、内存新，两边各说各话。"""
    import database as db
    from agent.harness.reins import gate

    client, _ = policy_client
    original = gate._ALLOWLIST_TTL_HOURS
    boom = pytest.MonkeyPatch()
    try:
        def _fail(*a, **k):
            raise RuntimeError("disk on fire")
        boom.setattr(db, "update_app_settings_bulk", _fail)
        resp = client.put("/api/settings/permission-policy",
                          json={"allowlist_ttl_hours": original + 7})
        assert resp.status_code == 500
        assert gate._ALLOWLIST_TTL_HOURS == original, "写库失败却把运行时推上去了"
    finally:
        boom.undo()
