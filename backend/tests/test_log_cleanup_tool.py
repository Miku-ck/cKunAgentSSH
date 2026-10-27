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
    # Trae CN 的真实部署路径在家目录下：托管日志标记必须按边界匹配，
    # 不能要求前缀恰好从 / 开始（2026-10-27 事故：dry_run 被目录校验挡回）
    "/home/admin-pl/.trae-cn-server/manager-logs",
    "/home/admin-pl/.trae-cn-server/manager-logs/server-2026",
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
    # 兄弟目录不享受托管日志豁免（边界匹配，不是子串包含）
    "/home/admin-pl/.trae-cn-server/manager-logs-backup",
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


# ── 生产环境闸门：只读预览免预登记，删除仍须预授权 ──────────────────────────
_MANAGED_ROOT = "/home/admin-pl/.trae-cn-server/manager-logs"


class _Saddle:
    def __init__(self):
        self.registry = create_registry()


def _gate(monkeypatch, allowlist=()):
    """构造带真实 ToolRegistry 的 PermissionGate（隔离 allowlist/审计 IO）。"""
    import agent.harness.reins.gate as gate_mod
    from agent.harness.reins.gate import PermissionGate

    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist",
                        lambda self: set(allowlist))
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist",
                        lambda self, keys: None)

    async def _noop_audit(**kwargs):
        return None

    monkeypatch.setattr(gate_mod, "append_record_async", _noop_audit)
    return PermissionGate(saddle=_Saddle())


def _run_ctx(environment, args):
    from agent.harness.contracts import AgentConfig, RunContext

    ctx = RunContext.new(task="t", config=AgentConfig(),
                         environment=environment)
    ctx.tool_calls_log.append(
        {"tool": "log_cleanup", "args": args, "iteration": 1, "success": None})
    return ctx


def _cleanup_args(dry_run):
    return {"root": _MANAGED_ROOT, "pattern": "*", "days": 30, "dry_run": dry_run}


def test_prod_preview_allowed_without_allowlist(monkeypatch):
    """prod 下 dry_run 只读预览无需预登记 —— 否则「先预览再确认」的主路径被堵死。

    2026-10-27 事故：预览被判「生产环境危险工具未预授权」，模型退回手敲
    find -exec / rm，连环触发更高危拦截与澄清向导。
    """
    decision = asyncio.run(_gate(monkeypatch).check(
        _run_ctx("prod", _cleanup_args(dry_run=True))))
    assert decision.intercepted is False


def test_prod_delete_still_requires_allowlist(monkeypatch):
    """预览豁免不能顺带放走删除：无人值守 prod 的删除仍须预登记 allowlist。"""
    from agent.harness.contracts import POLICY_DENY_MARKER

    decision = asyncio.run(_gate(monkeypatch).check(
        _run_ctx("prod", _cleanup_args(dry_run=False))))
    assert decision.intercepted is True
    assert POLICY_DENY_MARKER in decision.intercept_reason


def test_prod_delete_allowed_after_preallocation(monkeypatch):
    decision = asyncio.run(_gate(monkeypatch, allowlist={"log_cleanup"}).check(
        _run_ctx("prod", _cleanup_args(dry_run=False))))
    assert decision.intercepted is False


def test_preview_exemption_rejects_invalid_args(monkeypatch):
    """参数非法的 preview 不享受豁免（fail-closed）：is_preview_call 必须是
    校验通过 + dry_run 双条件，不能只看 dry_run 字段。"""
    args = {"root": "/etc", "pattern": "*", "days": 30, "dry_run": True}
    assert LogCleanupTool.is_preview_call(args) is False
    decision = asyncio.run(_gate(monkeypatch).check(_run_ctx("prod", args)))
    assert decision.intercepted is True
