"""会话终结回收 session 授权池回归（审查发现：session 池无任何回收点）。

修复前：PermissionGate._confirmed_session 按 conversation_id 键控，唯一的
删除点是 revoke —— once/denied 池都有 run 终态清理（cleanup_run_once /
cleanup_run_denied + routes 兜底），session 池没有任何回收点；后端长驻
进程，已消失的会话把授权键逐会话留在内存里。

修复后语义（本文件钉住）：
  · gate.cleanup_conversation_session 清空目标会话的授权键、不动其他会话；
  · 会话终结的两条路径都回收该会话的 session 授权 —— 用户删除
    （DELETE /chat/conversations/{cid}）与保留清理（prune_conversations +
    _maybe_prune_agent_runs），与 once 池随 run 终结回收同一口径。

运行: python -m pytest backend/tests/test_session_pool_conversation_cleanup.py -v
"""
import asyncio

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
from agent.harness.reins import PermissionGate, _get_global_permission_gate_sync

# 会话删除路由用例的工具名：独占名字避免与进程级单例里其他测试的授权互相干扰
_TOOL = "pool_cleanup_tool"


@pytest.fixture(autouse=True)
def _no_allowlist_io(monkeypatch):
    """隔离 ~/.ckunagentic/allowlist.txt 文件 IO（沙箱 + 避免读入开发机真实 allowlist）"""
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)


def _gate() -> PermissionGate:
    return PermissionGate()


# ── gate 层：cleanup_conversation_session ──────────────────────────────────


def test_cleanup_conversation_session_clears_target_only():
    g = _gate()
    g.grant("t1", scope="session", conversation_id="convA")
    g.grant("t2", scope="session", conversation_id="convA")
    g.grant("t3", scope="session", conversation_id="convB")
    released = g.cleanup_conversation_session("convA")
    assert released == 2, "convA 有两条 session 授权"
    # convA 清空（重发需再授权），convB 不受影响
    assert g.check_permission("t1", {}, conversation_id="convA") is False
    assert g.check_permission("t2", {}, conversation_id="convA") is False
    assert g.check_permission("t3", {}, conversation_id="convB") is True
    # 重复清理 / 不存在的会话 → 返回 0（幂等）
    assert g.cleanup_conversation_session("convA") == 0
    assert g.cleanup_conversation_session("conv-missing") == 0


def test_cleanup_conversation_session_covers_host_scoped_keys():
    """主机限域 session 授权（@host|key）与裸键同池，随会话一并回收"""
    g = _gate()
    g.grant("t1", scope="session", conversation_id="convA", host="db-01")
    g.grant("t2", scope="session", conversation_id="convA")
    released = g.cleanup_conversation_session("convA")
    assert released == 2
    assert g.check_permission("t1", {}, conversation_id="convA",
                              host="db-01") is False
    assert g.check_permission("t2", {}, conversation_id="convA") is False


# ── 路由层：会话删除即终结路径 ─────────────────────────────────────────────


def _make_client(*routers) -> TestClient:
    app = fastapi.FastAPI()
    for r in routers:
        app.include_router(r, prefix="/api")
    return TestClient(app)


def test_delete_conversation_releases_session_pool(base_db, monkeypatch):
    """删除会话（会话唯一的终结点）必须顺带清空该会话的 session 授权键"""
    import routes.chat as chat_mod
    client = _make_client(chat_mod.router)

    conv = db.create_conversation(title="t", mode="ssh")
    other = db.create_conversation(title="o", mode="ssh")

    # 进程级单例：与生产 grant 端点（POST /agent/permission/grant）写的是同一个池。
    # conversation_id 用本用例独占的 DB 会话 id；永久池清空是防开发机真实
    # allowlist 命中同名工具的预授权干扰「已回收」判定（monkeypatch 自动还原）
    pg = _get_global_permission_gate_sync()
    monkeypatch.setattr(pg, "_confirmed_permanent", set())
    pg.grant(_TOOL, scope="session", conversation_id=conv["id"])
    pg.grant(_TOOL, scope="session", conversation_id=other["id"])
    assert pg.check_permission(_TOOL, {}, conversation_id=conv["id"]) is True

    assert client.delete(f"/api/chat/conversations/{conv['id']}").status_code == 200
    # 终结路径已清空被删会话的授权；其他会话不受牵连
    assert pg.check_permission(_TOOL, {}, conversation_id=conv["id"]) is False
    assert pg.check_permission(_TOOL, {}, conversation_id=other["id"]) is True
    # 不留空壳：被删会话的键整个出池
    assert conv["id"] not in pg._confirmed_session


def test_retention_prune_releases_session_pool(base_db, monkeypatch):
    """保留清理（conv_retention.enabled）是会话终结的第二条路径：
    被批量回收的会话，其 session 授权键必须一并出池（_maybe_prune_agent_runs
    拿 prune_conversations 返回的被删 id 逐个回收）"""
    import routes.agent as agent_mod

    db.set_app_setting("conv_retention.enabled", "true")
    db.set_app_setting("conv_retention.keep_count", "2")
    # 4 个 completed 会话，用消息时间控制新旧：c0 最旧 … c3 最新 → 回收 c0/c1
    conn = db._get_conn()
    try:
        cids = [db.create_conversation(title=f"c{i}")["id"] for i in range(4)]
        for i, cid in enumerate(cids):
            conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (?, ?, 'user', 'x', ?)",
                (f"m{i}", cid, f"2026-01-0{i+1} 00:00:00"),
            )
        conn.commit()
    finally:
        conn.close()

    # 进程级单例：与生产授权端点写的是同一个池（永久池清空防开发机真实
    # allowlist 干扰判定，monkeypatch 自动还原）
    pg = _get_global_permission_gate_sync()
    monkeypatch.setattr(pg, "_confirmed_permanent", set())
    for cid in cids:
        pg.grant(_TOOL, scope="session", conversation_id=cid)
    # 节流置零：_maybe_prune_agent_runs 每 6h 最多一次，测试需要它真跑
    monkeypatch.setattr(agent_mod, "_last_agent_runs_prune_at", 0.0)

    asyncio.run(agent_mod._maybe_prune_agent_runs())

    # 被回收的 c0/c1 授权出池；保留的 c2/c3 不受牵连
    assert pg.check_permission(_TOOL, {}, conversation_id=cids[0]) is False
    assert pg.check_permission(_TOOL, {}, conversation_id=cids[1]) is False
    assert pg.check_permission(_TOOL, {}, conversation_id=cids[2]) is True
    assert pg.check_permission(_TOOL, {}, conversation_id=cids[3]) is True
    assert cids[0] not in pg._confirmed_session
    assert cids[2] in pg._confirmed_session
