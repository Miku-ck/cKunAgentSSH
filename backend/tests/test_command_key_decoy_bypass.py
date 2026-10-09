# -*- coding: utf-8 -*-
"""审批绕过的钥匙孔：判定层与执行层读的不是同一个参数键。

`command_from_args` 原本按 `command → shell → cmd` 取**第一个非空**，而
`JobRunTool.execute(self, context, **kwargs)` 只读 `cmd`。于是模型只要发

    {"command": "echo DECOY", "cmd": "rm -rf /var/log"}

FilesystemGuard / PermissionGate / 计划模式 / Sandbox 底线 / 澄清这五层看到的都是
`echo DECOY`（safe，直接放行），真正跑起来的是 `rm -rf /var/log`。`ToolRegistry.execute`
把 args 原样 `**args` 展开、不对照 `parameters` 校验，多余键不会被丢掉；`shell_exec`
因为是位置签名，多余键触发 TypeError 才侥幸没中招 —— 两条命令入口一严一松，这种不对称
本身就是洞。

修法两层，缺一不可：
1. 提取器把所有命令形状的键**全部**纳入判定（换行拼成多段，分类器逐段判，最严的算）；
2. `job_run` 自己在执行前拒绝歧义参数 —— 只做第 1 层的话，用户批的是「两段都要」，
   执行却只跑其中一段，仍然是审批对象与执行对象不一致。
"""
import asyncio

import pytest

import agent.harness.reins.gate as _gate_mod
import agent.harness.reins.guards as _guards_mod
from agent.harness.contracts import (
    AgentConfig, ReinDecision, ReinPoint, RunContext, TraceEvent)
from agent.harness.reins import FilesystemGuard, PermissionGate
from agent.harness.reins.core import (
    MODE_KEY, Reins, command_from_args, extract_bound_command)
from agent.harness.reins.plan_mode import PlanModeGuard
from agent.tools import ToolContext, create_registry
from agent.tools.builtin.ops_job import JobRunTool
from agent.tools.builtin.shell_exec import ShellExecTool

DECOY = {"command": "echo DECOY", "cmd": "rm -rf /var/log"}
REAL = "rm -rf /var/log"


# ── 提取器：不许「挑一个键」──────────────────────────────────────────────────

def test_extractor_collects_every_command_key():
    out = command_from_args(DECOY)
    assert "echo DECOY" in out and REAL in out, out
    # 换行拼接后分类器逐段判定，任何一段高危都躲不掉
    segs = out.split("\n")
    assert segs == ["echo DECOY", REAL], "顺序必须是 command→shell→cmd 的登记顺序"


def test_extractor_is_unchanged_for_single_key_calls():
    """合法调用一个键 ⇒ 串不变。修绕过不能顺手改变正常审批键的哈希对象。"""
    assert command_from_args({"command": "ls"}) == "ls"
    assert command_from_args({"cmd": "ls"}) == "ls"
    assert command_from_args({"shell": "ls"}) == "ls"
    assert command_from_args({"command": "ls", "timeout": 30}) == "ls"


def test_identical_values_are_not_duplicated():
    """两个键给同一条命令（模型偶尔重复填）不该被当成「两段脚本」。"""
    assert command_from_args({"command": "ls", "cmd": "ls"}) == "ls"


def test_non_string_command_key_is_not_judged_as_a_command():
    """`{"cmd": ["rm -rf /"]}` 这种脏值不能进分类器，也不能被当成空判成 safe+放行。"""
    assert command_from_args({"cmd": ["rm -rf /"]}) == ""


# ── 五层共用同一个提取器 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,extra", [
    ("job_run", DECOY),
    ("shell_exec", {"command": "echo DECOY", "cmd": REAL}),
])
def test_bound_command_exposes_the_hidden_segment(extra, tool):
    cmd, is_free = extract_bound_command(create_registry(), tool, extra)
    assert is_free is True, "命令入口的自由文本必须走 classify_command"
    assert REAL in cmd and "echo DECOY" in cmd, cmd


def test_plan_mode_blocks_the_decoy_shape():
    """计划模式原来只看见 `echo DECOY`：删库那段藏在没被读的键里，照过。"""
    ctx = RunContext.new(task="测试任务")
    ctx.metadata[MODE_KEY] = "plan"
    ctx.tool_calls_log.append({"tool": "job_run", "args": DECOY,
                               "iteration": 0, "success": None})
    d = asyncio.run(PlanModeGuard().check(ctx))
    assert d.intercepted, "计划模式下藏一写段不该放行"


# ── 真实审批链：卡片/阻断都必须针对真命令 ────────────────────────────────────

@pytest.fixture
def audits(monkeypatch):
    records = []

    async def _rec(**kw):
        records.append(kw)

    monkeypatch.setattr(
        PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(
        PermissionGate, "_save_permanent_allowlist", lambda self: None)
    for _m in (_guards_mod, _gate_mod):
        monkeypatch.setattr(_m, "append_record_async", _rec)
    return records


def _saddle():
    from types import SimpleNamespace
    return SimpleNamespace(registry=create_registry())


async def _chain(ctx, gate, guard):
    d = await gate.check(ctx)
    if d.intercepted:
        return d
    return await guard.check(ctx)


def _hardline_shape(audits):
    gate = PermissionGate(saddle=_saddle())
    guard = FilesystemGuard(permission_gate=gate, saddle=_saddle())
    ctx = RunContext.new(task="测试", config=AgentConfig(), environment="test")
    ctx.tool_calls_log.append({"tool": "job_run", "args": DECOY,
                               "iteration": 0, "success": None})
    d = asyncio.run(_chain(ctx, gate, guard))
    return d, gate, ctx, audits


def test_hardline_hidden_segment_is_blocked_not_carded(audits):
    d, _gate, _ctx, audits = _hardline_shape(audits)
    assert d.intercepted
    assert not any(e.kind == "permission_needed" for e in d.events), \
        f"藏着的 rm -rf 变成了一张可以点的卡：{d.events}"
    blocked = [r for r in audits if r.get("conclusion") == "blocked"]
    assert blocked, f"没有阻断审计行：{audits}"
    assert REAL in blocked[-1]["command_raw"], blocked[-1]


def test_dangerous_hidden_segment_shows_the_real_payload(audits):
    """`{"command": "id", "cmd": "cat /etc/shadow"}`：卡片必须把读凭据那段摊给人看。"""
    args = {"command": "id", "cmd": "cat /etc/shadow"}
    gate = PermissionGate(saddle=_saddle())
    guard = FilesystemGuard(permission_gate=gate, saddle=_saddle())
    ctx = RunContext.new(task="测试", config=AgentConfig(), environment="test")
    ctx.tool_calls_log.append({"tool": "job_run", "args": args,
                               "iteration": 0, "success": None})
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted
    cards = [e for e in d.events if e.kind == "permission_needed"]
    assert len(cards) == 1, d.events
    payload = cards[0].payload
    assert "cat /etc/shadow" in payload["command"], payload["command"]
    assert payload["explain"], "后果说明必须跟着真实那段走"


def test_approving_the_decoy_pair_does_not_free_the_real_command(audits):
    """批了「两段都要」这个键，不能变成「单独跑 rm 那段也免审」。"""
    d, gate, ctx, _a = _hardline_shape(audits)
    assert d.intercepted
    decoy_key = gate._grant_key("job_run", command_from_args(DECOY))
    real_key = gate._grant_key("job_run", REAL)
    assert decoy_key != real_key


# ── 执行层：歧义参数在工具里就被拒绝 ─────────────────────────────────────────

def test_job_run_refuses_command_shaped_extra_keys():
    tool = JobRunTool()
    res = asyncio.run(tool.execute(ToolContext(), **DECOY))
    assert res.success is False
    assert "只接受 cmd" in (res.error or ""), res.error
    assert "command" in res.error, "要说清是哪个多余键，模型才知道怎么改"


def test_job_run_still_runs_a_plain_cmd_call():
    """反向锁：拒绝歧义不能顺手把正常调用打死（这里只验它没被歧义分支拦下）。"""
    tool = JobRunTool()
    res = asyncio.run(tool.execute(ToolContext(), cmd="", command=""))
    assert res.success is False
    assert "cmd 不能为空" in (res.error or ""), res.error


def test_shell_exec_rejects_the_same_shape_via_signature():
    """两条命令入口都必须拒绝：一条靠位置签名，一条靠显式检查。"""
    tool = ShellExecTool()
    with pytest.raises(TypeError):
        asyncio.run(tool.execute(ToolContext(), **DECOY))
    reg = create_registry()
    res = asyncio.run(reg.execute(
        "shell_exec", {"command": "echo DECOY", "cmd": REAL}, ToolContext()))
    assert res.success is False, "registry 必须把多余键变成失败，而不是悄悄少跑一条"


# ── 附带：安全拦截器异常时的反馈完整性（同一条审批链上的另一处早退）──────────

class _Injected:
    """低优先级、不拦截，但要往本轮塞纠偏事件与注入消息"""

    point = ReinPoint.PRE_TOOL_CALL
    priority = 1
    safety_critical = False

    async def check(self, ctx):
        return ReinDecision(
            intercepted=False,
            events=[TraceEvent(kind="warning", source="loop",
                               run_id=ctx.run_id, iteration=ctx.iteration,
                               payload={"message": "循环纠偏"})],
            inject_messages=[{"role": "user", "content": "[系统提示] 换个做法"}])


class _Boom:
    point = ReinPoint.PRE_TOOL_CALL
    priority = 2
    safety_critical = True

    async def check(self, ctx):
        raise RuntimeError("分类器炸了")


def test_fail_closed_return_keeps_aggregated_feedback():
    """拦截早退不能顺手把已聚合的纠偏/注入丢掉 —— 拦截分支与异常分支口径要一致。

    修复前：`safety_critical` 异常返回的是裸 ReinDecision，前面拦截器已经产出的
    events/inject_messages 整份蒸发（模型只看到"被安全模块拦了"，看不到本轮原本
    要它改什么）。拦截返回那条分支早已修过这个形状，异常这条是同一条路的另一半。
    """
    reins = Reins()
    reins.register(_Injected())
    reins.register(_Boom())
    ctx = RunContext.new(task="测试", config=AgentConfig(), environment="test")
    d = asyncio.run(reins.check(ReinPoint.PRE_TOOL_CALL, ctx))
    assert d.intercepted, "安全拦截器异常必须 fail-closed"
    assert any("循环纠偏" in str(e.payload) for e in d.events), d.events
    assert any("换个做法" in m["content"] for m in d.inject_messages), d.inject_messages

