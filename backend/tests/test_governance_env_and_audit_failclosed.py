"""治理层 P0 回归：环境标签服务端判定 + 授权写前审计（fail-closed）。

## 钉住的两件事

1. **环境标签不能被客户端降级**（agent.environment_policy）
   修复前 `AgentRunRequest.environment` 是渲染层自报、默认 "test"，而 prod 强化
   （危险命令只认预登记 allowlist）正是靠它分流 —— 一次请求把 prod 主机声明成
   test，整套生产强化静默失效，审批卡上还写着「环境：test」。
   现在以主进程连接配置的 environment 为准，只能收紧不能放宽，且 source 会带到
   审批卡与审计记录里（未核实时如实标注）。

2. **授权必须 write-ahead**（audit.append_record(critical=True)）
   `append_record` 历史上「失败静默返回 None」，而 grant/allow 端点不看返回值：
   一次磁盘或锁故障就产出一条「授权已生效、审计链上没有」的缺口 —— 事后取证看到
   的是一条干净但不完整的链，比空链更危险。现在 critical=True 写不进去就抛
   AuditWriteError（HTTP 层转 503），授权不落地。
"""
import asyncio

import pytest

import database as db
from agent import audit
from agent.audit import AuditWriteError
from agent.environment_policy import (
    EnvironmentResolution,
    resolve_run_environment,
)
from agent.harness.reins import PermissionGate
from agent.harness.reins.core import permission_display_fields
from agent.harness.contracts import RunContext
import routes.agent as agent_mod


# ──────────────────────────────────────────────────────────────────────────────
# 环境标签：服务端权威判定
# ──────────────────────────────────────────────────────────────────────────────

def _patch_session_info(monkeypatch, payload):
    async def _fake(session_id):
        return payload

    monkeypatch.setattr(
        "agent.environment_policy._fetch_session_info", _fake)


def test_连接配置标prod时_客户端声称test不能降级(monkeypatch):
    _patch_session_info(monkeypatch, {
        "connectionId": "c1", "host": "10.0.0.9", "environment": "prod"})
    res = asyncio.run(resolve_run_environment("test", "sess-1"))
    assert res.effective == "prod"
    assert res.source == "connection"
    assert res.tightening is True
    assert res.host == "10.0.0.9"


def test_客户端声称prod时_连接配置test也按prod执行(monkeypatch):
    # 只能收紧：客户端主动升级到 prod 是合法请求，不该被连接配置拉回 test
    _patch_session_info(monkeypatch, {
        "connectionId": "c2", "host": "h2", "environment": "test"})
    res = asyncio.run(resolve_run_environment("prod", "sess-2"))
    assert res.effective == "prod"
    assert res.tightening is False


def test_读不到主机信息时按客户端声明但标注未核实(monkeypatch):
    _patch_session_info(monkeypatch, None)
    res = asyncio.run(resolve_run_environment("test", "sess-3"))
    assert res.effective == "test"
    assert res.source == "bridge_unavailable"
    assert res.source_label  # 审批卡要有一句「未核实」


def test_无ssh会话时不问桥接(monkeypatch):
    async def _boom(session_id):
        raise AssertionError("无会话时不该发起桥接查询")

    monkeypatch.setattr("agent.environment_policy._fetch_session_info", _boom)
    res = asyncio.run(resolve_run_environment("test", ""))
    assert (res.effective, res.source) == ("test", "client")


def test_非法环境值按test处理且留痕(monkeypatch):
    _patch_session_info(monkeypatch, None)
    res = asyncio.run(resolve_run_environment("production", "sess-4"))
    assert res.effective == "test"
    assert "非法" in res.note


def test_审批卡展示环境来源(monkeypatch):
    ctx = RunContext.new(task="t", environment="test")
    ctx.metadata["environment_source"] = "connection"
    fields = permission_display_fields(ctx, "rm -f /tmp/x")
    assert fields["environment"] == "test"
    assert fields["environment_source"] == "connection"
    assert fields["environment_source_label"]


# ──────────────────────────────────────────────────────────────────────────────
# 审计写入：critical 抛错 + health 可见
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_audit_health():
    audit.reset_health()
    yield
    audit.reset_health()


def test_非关键审计写失败静默但计入health(monkeypatch):
    def _boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "_get_conn", _boom)
    assert audit.append_record(
        action="shell_exec", conclusion="executed") is None
    snap = audit.health()
    assert snap["ok"] is False
    assert snap["consecutive_failures"] >= 1
    assert snap["last_failed_action"] == "shell_exec"
    assert "disk full" in snap["last_error"]


def test_关键审计写失败抛异常(monkeypatch):
    def _boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "_get_conn", _boom)
    with pytest.raises(AuditWriteError):
        audit.append_record(
            action="permission_grant:shell_exec", conclusion="approved",
            critical=True)


# ──────────────────────────────────────────────────────────────────────────────
# 授权端点：写前审计 + 环境列
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def gate_env(monkeypatch):
    """屏蔽 allowlist.txt 宿主文件 IO（本文件只关心端点的裁决与审计顺序）。"""
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    yield
    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    for run_id in ("run-wa", "run-env"):
        pg.cleanup_run_once(run_id)
    pg.revoke("shell_exec")


def test_审计写不进去时授权不生效(monkeypatch, gate_env):
    """write-ahead：审计先落，才允许授权。写失败必须让授权回滚（不落地）。"""
    monkeypatch.setattr(db, "get_agent_run", lambda run_id: {"status": "running"})

    async def _boom(**kwargs):
        raise AuditWriteError("audit chain unwritable")

    monkeypatch.setattr(agent_mod, "append_record_async", _boom)

    with pytest.raises(AuditWriteError):
        asyncio.run(agent_mod.grant_tool_permission(
            agent_mod.PermissionGrantRequest(
                tool="shell_exec", scope="once",
                command="rm -f /tmp/wa", run_id="run-wa")))

    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    assert pg.check_permission(
        "shell_exec", {"command": "rm -f /tmp/wa"}, risk="dangerous",
        conversation_id="", run_id="run-wa",
        bound_command="rm -f /tmp/wa") is False, "审计失败却已授权 → 会出现无证据的放行"


def test_授权审计带run的环境列(monkeypatch, gate_env):
    monkeypatch.setattr(db, "get_agent_run", lambda run_id: {"status": "running"})
    agent_mod._remember_run_environment("run-env", EnvironmentResolution(
        effective="prod", requested="test", source="connection"))

    seen: list[dict] = []

    async def _rec(**kwargs):
        seen.append(kwargs)
        return None

    monkeypatch.setattr(agent_mod, "append_record_async", _rec)
    try:
        body = asyncio.run(agent_mod.grant_tool_permission(
            agent_mod.PermissionGrantRequest(
                tool="shell_exec", scope="once",
                command="rm -f /tmp/env", run_id="run-env")))
    finally:
        agent_mod._forget_run_environment("run-env")

    assert body["granted"] is True
    assert seen[-1]["environment"] == "prod", (
        "prod 上的审批若记成 test，审计页的 prod 视图里根本看不到这次授权")
    assert seen[-1]["critical"] is True, "授权审计必须是关键写（写不进去就不授权）"
