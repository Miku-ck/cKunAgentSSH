"""运维智能工具测试 — 注册、元数据、解析与行为契约。

覆盖新增的 6 个运维工具：sys_overview / process_inspect / network_probe /
log_read / service_manage / schedule_op。测试刻意避免依赖真实 POSIX 命令与
真实数据库，通过 monkeypatch 共享执行辅助 / 数据库层保证跨平台确定性。
"""
import asyncio

import pytest

from agent.tools import ToolContext, create_registry_for_mode
from agent.tools.builtin import (
    get_all_tool_classes,
    SysOverviewTool,
    ProcessInspectTool,
    NetworkProbeTool,
    LogReadTool,
    ServiceManageTool,
    ScheduleOpTool,
)
from agent.tools.builtin import ops_schedule

NEW_TOOLS = {
    "sys_overview",
    "process_inspect",
    "network_probe",
    "log_read",
    "service_manage",
    "schedule_op",
}


def _ctx(**kwargs) -> ToolContext:
    base = dict(workspace_path=".", ssh_session_id="", mode="ssh",
                environment="test", visible_terminal=False)
    base.update(kwargs)
    return ToolContext(**base)


async def _run(coro):
    return await coro


# ── 注册与元数据 ─────────────────────────────
def test_new_tools_registered_in_ssh_scope():
    names = {cls().name for cls in get_all_tool_classes("ssh")}
    assert NEW_TOOLS <= names


def test_registry_contains_new_tool_names():
    reg = create_registry_for_mode("ssh")
    assert NEW_TOOLS <= set(reg.names())


def test_new_tools_meta_safe():
    for cls in (SysOverviewTool, ProcessInspectTool, NetworkProbeTool, LogReadTool):
        t = cls()
        assert t.meta.risk_level == "safe"
        assert t.meta.reversible is True
        assert t.meta.idempotent is True


def test_write_tools_meta_moderate():
    for cls in (ServiceManageTool, ScheduleOpTool):
        t = cls()
        assert t.meta.risk_level == "moderate"
        assert t.meta.reversible is False


# ── cron 子集解析（无 DB / 无 OS）─────────────
@pytest.mark.parametrize("cron,expect_type", [
    ("* * * * *", "daily"),
    ("30 3 * * *", "daily"),
    ("5 2 * * 1", "weekly"),
    ("0 22 * * 0", "weekly"),  # 0=周日→engine weekday6
    ("10 4 15 * *", "monthly"),
])
def test_cron_parse_valid(cron, expect_type):
    parsed, err = ops_schedule._parse_cron(cron)
    assert err is None
    assert parsed["schedule_type"] == expect_type


@pytest.mark.parametrize("cron", [
    "*/5 * * * *", "5 3 1,2 * *", "5 3 * 2 *", "30 3 * * 8",
    "5 3 1 *",                       # 字段数错误
    "0 0 1 1 *",                     # month 非 *
])
def test_cron_parse_rejects_unsupported(cron):
    parsed, err = ops_schedule._parse_cron(cron)
    assert parsed is None
    assert err


# ── service_manage 破坏性标记（monkeypatch 执行）──
@pytest.mark.parametrize("action,expect_destructive", [
    ("status", False), ("start", False), ("stop", True), ("restart", True),
])
async def _service_manage_marked(action, expect_destructive, m):
    async def fake_run(ctx, command, timeout):
        return True, f"{action} ok"
    m.setattr("agent.tools.builtin.ops_service.run_headless", fake_run)

    res = await ServiceManageTool().execute(_ctx(), action=action, name="nginx")
    assert "destructive=true" in res.output if expect_destructive else \
        ("destructive=true" not in res.output)


@pytest.mark.parametrize("action,expect", [
    ("status", False), ("start", False), ("stop", True), ("restart", True),
])
def test_service_manage_destructive_marker(action, expect, monkeypatch):
    asyncio.run(_service_manage_marked(action, expect, monkeypatch))


def test_service_manage_validation():
    asyncio.run(_service_fail_validation())


async def _service_fail_validation():
    tool = ServiceManageTool()
    r = await tool.execute(_ctx(), action="rm", name="nginx")          # 非法 action
    assert r.success is False
    r = await tool.execute(_ctx(), action="stop", name="bad;rm -rf /")  # 注入尝试
    assert r.success is False
    r = await tool.execute(_ctx(), action="stop", name="")              # 空名
    assert r.success is False


# ── log_read 参数契约 ─────────────────────────
def test_log_read_rejects_bad_path():
    async def go():
        tool = LogReadTool()
        r = await tool.execute(_ctx(), path="")
        assert r.success is False
        r = await tool.execute(_ctx(), path="x;rm -rf /")
        assert r.success is False
    asyncio.run(go())


def test_log_read_lines_clamped(monkeypatch):
    captured = {}

    async def fake_run(ctx, command, timeout):
        captured["command"] = command
        return True, ""
    monkeypatch.setattr("agent.tools.builtin.ops_obs.run_headless", fake_run)

    async def go():
        tool = LogReadTool()
        await tool.execute(_ctx(), path="/var/log/app.log", lines=99999)
    asyncio.run(go())
    assert "tail -n 1000" in captured["command"]  # 钳制到上限 1000


# ── schedule_op（monkeypatch DB / engine）──────
def test_schedule_op_create_interval(monkeypatch):
    class FakeEngine:
        def ensure_started(self):
            return None

    monkeypatch.setattr(ops_schedule.db, "create_schedule",
                        lambda data: {"id": "sched-xyz"})
    monkeypatch.setattr(ops_schedule.db, "set_schedule_next_run",
                        lambda sid, nxt: None)
    monkeypatch.setattr("agent.schedule_engine.get_schedule_engine",
                        lambda: FakeEngine())

    async def go():
        r = await ScheduleOpTool().execute(
            _ctx(), action="create", mode="interval", interval_minutes=30,
            task="备份并清理日志")
        assert r.success is True
        assert "schedule_id=sched-xyz" in r.output
    asyncio.run(go())


def test_schedule_op_create_bad_cron(monkeypatch):
    async def go():
        r = await ScheduleOpTool().execute(
            _ctx(), action="create", mode="cron", cron="*/5 * * * *")
        assert r.success is False
    asyncio.run(go())


def test_schedule_op_cancel_missing_id():
    async def go():
        r = await ScheduleOpTool().execute(_ctx(), action="cancel")
        assert r.success is False
    asyncio.run(go())


def test_schedule_op_bad_action():
    async def go():
        r = await ScheduleOpTool().execute(_ctx(), action="explode")
        assert r.success is False
    asyncio.run(go())