"""运维智能工具测试 — 注册、元数据、解析与行为契约。

覆盖新增的 6 个运维工具：sys_overview / process_inspect / network_probe /
log_read / service_manage / schedule_op。测试刻意避免依赖真实 POSIX 命令与
真实数据库，通过 monkeypatch 共享执行辅助 / 数据库层保证跨平台确定性。
"""
import asyncio

import pytest

from agent.tools import ToolContext, create_registry
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
    base = dict(ssh_session_id="",
                environment="test", visible_terminal=False)
    base.update(kwargs)
    return ToolContext(**base)


async def _run(coro):
    return await coro


# ── 注册与元数据 ─────────────────────────────
def test_new_tools_registered_in_ssh_scope():
    names = {cls().name for cls in get_all_tool_classes()}
    assert NEW_TOOLS <= names


def test_registry_contains_new_tool_names():
    reg = create_registry()
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


# ── schedule_op 与 /schedules 路由的校验口径对齐 ──────
def _patch_schedule_db(monkeypatch, captured: dict):
    class FakeEngine:
        def ensure_started(self):
            return None

    def fake_create(data):
        captured.update(data)
        return {"id": "sched-1"}

    monkeypatch.setattr(ops_schedule.db, "create_schedule", fake_create)
    monkeypatch.setattr(ops_schedule.db, "set_schedule_next_run", lambda sid, nxt: None)
    monkeypatch.setattr("agent.schedule_engine.get_schedule_engine", lambda: FakeEngine())


@pytest.mark.parametrize("minutes,expect_ok", [
    ("abc", False),   # 原先 int() 直接 ValueError 冒出 _create，模型拿到裸栈
    (0, False),       # 0 会让 compute_next_run 每 tick 触发
    (-5, False),
    (10 ** 9, False),  # 约 1900 年，POST 的 le=525600 拒收
    (None, True),     # 默认 60，与路由 default 一致
    (30, True),
])
def test_schedule_op_interval_minutes_matches_route_field_bounds(monkeypatch, minutes, expect_ok):
    captured = {}
    _patch_schedule_db(monkeypatch, captured)

    async def go():
        r = await ScheduleOpTool().execute(
            _ctx(), action="create", mode="interval", interval_minutes=minutes, task="t")
        assert r.success is expect_ok, r.error or r.output
    asyncio.run(go())
    if minutes is None:
        assert captured["interval_minutes"] == 60
    elif expect_ok:
        assert captured["interval_minutes"] == minutes


def test_schedule_op_monthly_cron_day_caps_at_28():
    """dom 29-31 会让 PATCH（含界面勾选启停）恒 400：_validate_fields 校验 merged 全量"""
    parsed, err = ops_schedule._parse_cron("0 9 29 * *")
    assert parsed is None and "1-28" in err
    parsed, err = ops_schedule._parse_cron("0 9 28 * *")
    assert err is None
    assert parsed["schedule_type"] == "monthly" and parsed["day_of_month"] == 28


def test_schedule_op_cron_row_keeps_editable_defaults(monkeypatch):
    """cron 派生的行不能带 interval_minutes=0 / weekday=-1：前者撞 ge=1，后者永不触发"""
    captured = {}
    _patch_schedule_db(monkeypatch, captured)

    async def go():
        r = await ScheduleOpTool().execute(
            _ctx(), action="create", mode="cron", cron="30 8 * * 0", task="轮转日志")
        assert r.success is True, r.error
    asyncio.run(go())

    assert captured["schedule_type"] == "weekly"
    assert captured["weekday"] == 6        # cron 周日 → engine weekday 6
    assert captured["interval_minutes"] == 60
    assert captured["time_hour"] == 8 and captured["time_minute"] == 30
    assert captured["day_of_month"] == 1


def test_schedule_op_list_empty_is_success_with_output(monkeypatch):
    """空列表是成功态：驱动只把 output 喂回模型，提示不能只写在 error 里"""
    monkeypatch.setattr(ops_schedule.db, "list_enabled_schedules", lambda: [])

    async def go():
        r = await ScheduleOpTool().execute(_ctx(), action="list")
        assert r.success is True
        assert r.output.strip()
    asyncio.run(go())


def test_network_probe_监听段不被退出码哨兵吞掉(monkeypatch):
    """S6-07 回归：[本地监听] 段曾经恒为空。

    run_headless 对每一次成功执行都无条件把 [exit_code=…] 拼在正文之后，而原判据是
    整串 `EXIT_CODE_PREFIX not in ss_out` ⇒ 恒假 ⇒ 这个成文的运维入口对「端口是否在
    听」恒答否（输出「（无监听信息）」），模型与用户都会把它读成事实；netstat 回退支
    也因同一判据不可达。同文件进程段早已用逐行跳过 —— 口径收敛到一处。
    """
    from agent.remote_protocol import EXIT_CODE_PREFIX
    sentinel = f"{EXIT_CODE_PREFIX}0]"

    async def fake_run(context, command, timeout=None):
        if "ss -ltn" in command:
            return True, "tcp  LISTEN  0  128  0.0.0.0:8080  0.0.0.0:*\n" + sentinel
        if "netstat -an" in command:
            return True, "tcp  LISTEN  0  128  0.0.0.0:9999\n" + sentinel
        return True, ""

    monkeypatch.setattr("agent.tools.builtin.ops_obs.run_headless", fake_run)

    async def go():
        return await NetworkProbeTool().execute(_ctx(), host="127.0.0.1", port=8080)

    res = asyncio.run(go())
    assert res.success is True
    assert "0.0.0.0:8080" in res.output, "ss 的监听信息被整串哨兵判据吞掉"
    assert "（无监听信息）" not in res.output
