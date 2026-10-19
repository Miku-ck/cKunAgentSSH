"""回归：executed 审计记录必须带得出「实际执行了什么」。

AuditLogger 原先固定 `command = str(command_from_args(args))`，而 command_from_args
只认 command/shell/cmd 三个槽位（core.py）。file_write / service_manage / schedule_op
这类工具自己拼命令 ⇒ 取到空串，哈希链里那条 executed 只有 action=file_write、
command_raw='' —— 谁在什么时候改了哪个路径、停了哪个服务，取证时点不出来。
C-review C2 补的就是"执行侧也要入链"，这一条是它缺的后半截。
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import database as db
from agent import audit
from agent.harness.contracts import RunContext
from agent.harness.reins import AuditLogger
from agent.tools import create_registry


def _audit_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


def _logged_ctx(tool: str, args: dict, monkeypatch) -> RunContext:
    """按生产接线准备 ctx：harness.py:244 会把 per-run saddle 放进 metadata，
    resolve_registry 正是从那里取注册表（AuditLogger 自己没有 saddle）。"""
    inner = _audit_conn()
    # 生产的 _get_conn 交出去的是 _PooledConnection（close = 回滚并保留复用）。
    # 给裸连接的话，链路上任何一次合规的 conn.close() 都会真的把库关掉 —— 本用例
    # 要走 create_registry，那条路上就有这样的调用。
    conn = db._PooledConnection(inner)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    ctx = RunContext.new(task="t", ssh_session_id="sess1")
    ctx.metadata["saddle"] = SimpleNamespace(registry=create_registry())
    ctx.tool_calls_log.append({"tool": tool, "args": args, "iteration": 1, "success": True})
    return ctx


def _rows():
    conn = db._get_conn()
    return conn.execute("SELECT action, conclusion, command_raw FROM audit_log").fetchall()


def test_file_write_executed_row_names_the_path(monkeypatch):
    ctx = _logged_ctx(
        "file_write",
        {"path": "/etc/nginx/nginx.conf", "content": "worker_processes 1;"},
        monkeypatch,
    )
    asyncio.run(AuditLogger().check(ctx))

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["action"] == "file_write"
    assert rows[0]["conclusion"] == "executed"
    # 修复前这里是空串：链里只写着"某次 file_write 成功了"
    assert rows[0]["command_raw"], "executed 记录不能没有真实命令"
    assert "/etc/nginx/nginx.conf" in rows[0]["command_raw"]


def test_service_manage_executed_row_names_the_unit(monkeypatch):
    ctx = _logged_ctx("service_manage", {"action": "stop", "name": "sshd"}, monkeypatch)
    asyncio.run(AuditLogger().check(ctx))

    rows = _rows()
    assert rows[0]["command_raw"] == "systemctl stop sshd"


def test_shell_exec_keeps_the_free_text_command(monkeypatch):
    """自由命令那条腿的口径不能被我改坏 —— 它一直是链上的主要证据。"""
    ctx = _logged_ctx("shell_exec", {"command": "uptime -p"}, monkeypatch)
    asyncio.run(AuditLogger().check(ctx))

    rows = _rows()
    assert rows[0]["command_raw"] == "uptime -p"
