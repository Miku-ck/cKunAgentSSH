"""log_cleanup 工具契约 — 有界日志清理能力。

设计目标：把「清理应用日志」从一条需要逐字登记的高危命令，收敛成一个
参数有界、服务端可校验的能力：
  - 只允许 /tmp、/var/tmp、/var/log 的应用子目录、agent 托管日志目录；
  - 只清理目标目录**第一层**的普通文件（-maxdepth 1 -type f）；
  - 默认试运行（dry_run=True），删除必须显式关闭试运行；
  - 系统/审计日志（audit/journal/private/auth.log/syslog…）不在清理范围内。

运行: python -m pytest backend/tests/test_log_cleanup_tool.py -v
"""
import asyncio

import pytest

from agent.tools import ToolContext
from agent.tools.builtin import LogCleanupTool, get_all_tool_classes
from agent.tools.builtin.ops_log_cleanup import log_cleanup_cmd
from agent.tools import create_registry


def _ctx(**kwargs) -> ToolContext:
    base = dict(ssh_session_id="sess-1", environment="test", visible_terminal=False)
    base.update(kwargs)
    return ToolContext(**base)


async def _run(**kwargs):
    return await LogCleanupTool().execute(_ctx(), **kwargs)


# ── 命令模板 ────────────────────────────────────────────────────────────────
def test_cmd_dry_run_prints_without_delete():
    cmd = log_cleanup_cmd("/var/log/app", "*.log", 30, False)
    assert cmd == (
        "find /var/log/app -maxdepth 1 -type f -name '*.log' -mtime +30 -print"
    )
    assert "-delete" not in cmd


def test_cmd_delete_prints_after_delete():
    cmd = log_cleanup_cmd("/var/log/app", "*.log", 30, True)
    assert cmd == (
        "find /var/log/app -maxdepth 1 -type f -name '*.log' -mtime +30 -delete -print"
    )


def test_cmd_quotes_pattern():
    cmd = log_cleanup_cmd("/tmp", "app-*.log", 7, True)
    assert "-name 'app-*.log'" in cmd
    assert "-mtime +7" in cmd


# ── 注册与元数据 ────────────────────────────────────────────────────────────
def test_registered():
    assert LogCleanupTool in get_all_tool_classes()
    assert "log_cleanup" in create_registry().names()


def test_meta_marks_destructive():
    tool = LogCleanupTool()
    assert tool.meta.risk_level == "dangerous"
    assert tool.meta.reversible is False


# ── 根目录边界 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("root", [
    "/tmp",
    "/tmp/app",
    "/var/tmp",
    "/var/tmp/app",
    "/var/log/app",
    "/var/log/app/sub",
    "/.trae-cn-server/manager-logs",
])
def test_allowed_roots(root):
    assert LogCleanupTool.bound_command(
        {"root": root, "pattern": "*.log", "days": 7, "dry_run": True}) is not None


@pytest.mark.parametrize("root", [
    "",
    "var/log/app",
    "/etc",
    "/home/app/logs",
    "/var/log",
    "/var/log/audit",
    "/var/log/journal",
    "/var/log/private",
    "/var/log/sssd",
    "/var/log/auth.log",
    "/var/log/secure",
    "/var/log/syslog",
    "/var/log/messages",
    "/var/log/kern.log",
    "/var/log/wtmp",
    "/var/log/app/",
    "/var/log/../etc",
    "/var/log/*",
    "/tmp/../etc",
    "/var/log/app\n",
])
def test_rejected_roots(root):
    assert LogCleanupTool.bound_command(
        {"root": root, "pattern": "*.log", "days": 7, "dry_run": True}) is None
    res = asyncio.run(_run(root=root, pattern="*.log", days=7, dry_run=True))
    assert res.success is False
    assert res.error


# ── 文件名模式边界 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("pattern", [
    "",
    "a/b",
    "*.log; rm -rf /",
    "*.log && id",
    "-delete",
    "x" * 129,
])
def test_rejected_patterns(pattern):
    assert LogCleanupTool.bound_command(
        {"root": "/var/log/app", "pattern": pattern, "days": 7, "dry_run": True}) is None
    res = asyncio.run(_run(root="/var/log/app", pattern=pattern, days=7))
    assert res.success is False
    assert res.error


@pytest.mark.parametrize("pattern", ["*.log", "app-*.log", "*.log.1", "access.log"])
def test_allowed_patterns(pattern):
    assert LogCleanupTool.bound_command(
        {"root": "/var/log/app", "pattern": pattern, "days": 7, "dry_run": True}) is not None


# ── 保留天数边界 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("days", [0, -1, 3651, "abc", None])
def test_rejected_days(days):
    assert LogCleanupTool.bound_command(
        {"root": "/var/log/app", "pattern": "*.log", "days": days, "dry_run": True}) is None
    res = asyncio.run(_run(root="/var/log/app", pattern="*.log", days=days))
    assert res.success is False
    assert res.error


@pytest.mark.parametrize("days", [1, 30, 3650])
def test_allowed_days(days):
    assert LogCleanupTool.bound_command(
        {"root": "/var/log/app", "pattern": "*.log", "days": days, "dry_run": True}) is not None


# ── 执行行为 ────────────────────────────────────────────────────────────────
def test_dry_run_executes_print_command(monkeypatch):
    captured = {}

    async def fake_run(ctx, command, timeout):
        captured["command"] = command
        captured["timeout"] = timeout
        return True, "/var/log/app/a.log\n/var/log/app/b.log\n"

    monkeypatch.setattr(
        "agent.tools.builtin.ops_log_cleanup.run_headless", fake_run)
    res = asyncio.run(_run(root="/var/log/app", pattern="*.log", days=30, dry_run=True))
    assert res.success is True
    assert "-delete" not in captured["command"]
    assert "-print" in captured["command"]
    assert "试运行" in res.output
    assert "a.log" in res.output and "b.log" in res.output


def test_delete_executes_delete_command(monkeypatch):
    captured = {}

    async def fake_run(ctx, command, timeout):
        captured["command"] = command
        return True, "/var/log/app/a.log\n"

    monkeypatch.setattr(
        "agent.tools.builtin.ops_log_cleanup.run_headless", fake_run)
    res = asyncio.run(_run(root="/var/log/app", pattern="*.log", days=30, dry_run=False))
    assert res.success is True
    assert captured["command"].endswith("-delete -print")
    assert "已删除 1 个文件" in res.output


def test_delete_reports_empty_match(monkeypatch):
    async def fake_run(ctx, command, timeout):
        return True, ""

    monkeypatch.setattr(
        "agent.tools.builtin.ops_log_cleanup.run_headless", fake_run)
    res = asyncio.run(_run(root="/tmp", pattern="*.log", days=7, dry_run=False))
    assert res.success is True
    assert "0 个文件" in res.output


def test_remote_failure_propagates(monkeypatch):
    async def fake_run(ctx, command, timeout):
        return False, "agent-exec DENY: no whitelisted template matched"

    monkeypatch.setattr(
        "agent.tools.builtin.ops_log_cleanup.run_headless", fake_run)
    res = asyncio.run(_run(root="/var/log/app", pattern="*.log", days=30, dry_run=False))
    assert res.success is False
    assert "DENY" in res.output or "DENY" in res.error


def test_dry_run_defaults_true(monkeypatch):
    captured = {}

    async def fake_run(ctx, command, timeout):
        captured["command"] = command
        return True, ""

    monkeypatch.setattr(
        "agent.tools.builtin.ops_log_cleanup.run_headless", fake_run)
    res = asyncio.run(_run(root="/var/log/app", pattern="*.log", days=30))
    assert res.success is True
    assert "-delete" not in captured["command"]


# ── 能力级授权键 ────────────────────────────────────────────────────────────
def test_capability_key_is_tool_level():
    """log_cleanup 的授权键取工具名：一次批准覆盖所有有界清理参数组合。

    普通命令入口必须保持命令哈希绑定（换命令 = 换授权）。
    """
    from agent.harness.reins.gate import PermissionGate

    cmd = log_cleanup_cmd("/var/log/app", "*.log", 30, True)
    assert PermissionGate._grant_key("log_cleanup", cmd) == "log_cleanup"
    assert PermissionGate._grant_key("log_cleanup", None) == "log_cleanup"
    assert PermissionGate._grant_key("shell_exec", cmd) != "shell_exec"


def test_capability_grant_covers_any_bounded_cleanup():
    """预登记 log_cleanup 后，换目录/模式/天数不再逐条重新登记。"""
    from agent.harness.reins.gate import PermissionGate

    gate = PermissionGate()
    gate._confirmed_permanent = {"log_cleanup"}
    for args in (
        {"root": "/var/log/app", "pattern": "*.log", "days": 30, "dry_run": False},
        {"root": "/tmp", "pattern": "app-*.log", "days": 7, "dry_run": False},
    ):
        bound = LogCleanupTool.bound_command(args)
        assert gate.check_permission(
            "log_cleanup", args, risk="dangerous", bound_command=bound) is True
