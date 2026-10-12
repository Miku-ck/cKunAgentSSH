# -*- coding: utf-8 -*-
"""回归测试：C-review C2 — 审计链必须覆盖"执行"与"批准"，不只是"拒绝"。

修复前：append_record 只被 FilesystemGuard 的 blocked/denied 分支调用，
成功执行的命令、grant/revoke 批准全部不入哈希链，/audit/export 导出的
合规证据缺了最关键的"谁批了什么、执行了什么"。
"""
import asyncio
import sqlite3

import database as db
from agent import audit
from agent.harness.contracts import RunContext
from agent.harness.reins import AuditLogger
from routes.agent import PermissionGrantRequest, grant_tool_permission, revoke_tool_permission


def _audit_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


def _rows(conn):
    return conn.execute(
        "SELECT action, conclusion, command_raw, target_id, result FROM audit_log"
    ).fetchall()


def test_audit_logger_appends_executed(monkeypatch):
    conn = _audit_conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    ctx = RunContext.new(task="t", ssh_session_id="sess1")
    ctx.tool_calls_log.append(
        {"tool": "shell_exec", "args": {"command": "ls -la"}, "iteration": 1, "success": True}
    )
    decision = asyncio.run(AuditLogger().check(ctx))
    assert not decision.intercepted  # AuditLogger 不拦截

    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["action"] == "shell_exec"
    assert rows[0]["conclusion"] == "executed"
    assert rows[0]["command_raw"] == "ls -la"
    assert rows[0]["target_id"] == "sess1"


def test_audit_logger_appends_failed(monkeypatch):
    conn = _audit_conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    ctx = RunContext.new(task="t")
    ctx.tool_calls_log.append(
        {"tool": "read_file", "args": {"path": "x"}, "iteration": 0, "success": False}
    )
    asyncio.run(AuditLogger().check(ctx))
    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["conclusion"] == "failed"
    assert rows[0]["command_raw"] == ""  # 非 shell 工具不带命令


def test_grant_and_revoke_are_audited(monkeypatch):
    conn = _audit_conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    # P2 修复（2026-10-11）给 grant 加了 once 前置的 run 状态查询；get_agent_run
    # 会 close 掉 _get_conn 返回的连接（生产中每次调用自建连接，无碍），这里共享
    # 内存库会被关掉，故直接 stub 掉查询 —— 本测试只关心审计链，不关心该前置。
    monkeypatch.setattr(db, "get_agent_run", lambda run_id: {"run_id": run_id, "status": "running"})
    # 屏蔽 allowlist.txt 文件 IO（测试不落宿主目录；_load 置空排除环境干扰）
    from agent.harness.reins import PermissionGate
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())

    # 审查R2修复：grant/revoke 路由改为 async def（消除 PermissionGate 跨线程竞态），
    # 直接调用返回协程，需经事件循环执行；审计 to_thread 用的连接是
    # check_same_thread=False 的内存库，跨线程写入安全。
    asyncio.run(grant_tool_permission(PermissionGrantRequest(
        tool="shell_exec", scope="once", command="rm -f /tmp/x",
        run_id="run-audit",
    )))
    asyncio.run(revoke_tool_permission("shell_exec", command="rm -f /tmp/x"))

    rows = _rows(conn)
    assert len(rows) == 2
    assert rows[0]["conclusion"] == "approved"
    assert rows[0]["command_raw"] == "rm -f /tmp/x"
    assert "scope=once" in rows[0]["result"]
    assert rows[1]["conclusion"] == "revoked"
    assert rows[1]["command_raw"] == "rm -f /tmp/x"
