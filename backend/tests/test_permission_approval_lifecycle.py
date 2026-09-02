# -*- coding: utf-8 -*-
"""C-review M7 回归：审批等待三态处理 + 循环检测豁免 + 审批超时。

修复前：审批期间 run 继续重发（烧 LLM 调用）；重发签名进 LoopDetector 窗口，
第 4 次重发被循环检测抢截（permission_needed 不再发出）→ 授权卡片孤儿、命令
永远执行不了；无审批超时。
修复后：首次请求发卡片并标记「等待审批」；窗口内重发豁免循环检测、不重发卡片；
超时（120s）按拒绝（permission_expired + 强拒绝注入 + 移除标记，循环检测恢复）；
命令执行成功后移除标记。

运行: python -m pytest backend/tests/test_permission_approval_lifecycle.py -v
"""
import asyncio
import time

import pytest

import agent.harness.reins as reins_mod
from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    RunContext,
    State,
    ToolCall,
    ToolResult,
)
from agent.harness.driver import Driver
from agent.harness.reins import FilesystemGuard, LoopDetector, PermissionGate

CMD = "systemctl restart nginx"  # 运维动词 → dangerous（非 hardline，走审批路径）


@pytest.fixture(autouse=True)
def _no_file_io(monkeypatch):
    """隔离 allowlist 文件 IO + 审计落库（真实 DB）"""
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)
    monkeypatch.setattr(reins_mod, "append_record", lambda **kw: None)


def _ctx() -> RunContext:
    return RunContext.new(task="测试", mode="ssh", config=AgentConfig())


def _log(ctx: RunContext, command: str = CMD) -> None:
    ctx.tool_calls_log.append({
        "tool": "shell_exec",
        "args": {"command": command},
        "iteration": len(ctx.tool_calls_log),
        "success": None,
    })


def _guard() -> FilesystemGuard:
    gate = PermissionGate()
    gate._confirmed_permanent = set()
    return FilesystemGuard(permission_gate=gate)


# ── 循环检测豁免 ──

def test_pending_sig_exempt_from_loop_detection():
    """审批窗口内的签名豁免循环检测 — 重发不得被抢截"""
    ctx = _ctx()
    _log(ctx)
    sig = LoopDetector._signature(ctx.tool_calls_log[-1])
    ctx.metadata["permission_pending"] = {sig: time.monotonic()}
    det = LoopDetector()
    for _ in range(6):
        _log(ctx)
        d = asyncio.run(det.check(ctx))
        assert not d.intercepted, "审批窗口内的重发不得触发循环检测拦截"
        assert not d.inject_messages


def test_no_pending_still_detects():
    """无审批标记时同样重发仍会升级循环检测（对照组，豁免不改变正常检测）"""
    ctx = _ctx()
    det = LoopDetector()
    decisions = []
    for _ in range(5):
        _log(ctx)
        decisions.append(asyncio.run(det.check(ctx)))
    assert decisions[-1].intercepted and decisions[-1].terminate, (
        "无豁免时连续 5 次同签名应升级到强制终止"
    )


# ── 审批三态：首次 / 窗口内 / 超时 ──

def test_first_request_emits_card_and_marks_pending():
    ctx = _ctx()
    _log(ctx)
    d = asyncio.run(_guard().check(ctx))
    assert d.intercepted
    assert any(e.kind == "permission_needed" for e in d.events), "首次请求必须发授权卡片事件"
    sig = LoopDetector._signature(ctx.tool_calls_log[-1])
    assert sig in ctx.metadata.get("permission_pending", {}), "首次审批请求必须标记「等待审批」"


def test_resend_within_window_no_duplicate_card():
    """窗口内重发：仍拦截，但不重发 permission_needed（卡片已存在，前端亦去重）"""
    ctx = _ctx()
    _log(ctx)
    asyncio.run(_guard().check(ctx))
    _log(ctx)  # 重发
    d2 = asyncio.run(_guard().check(ctx))
    assert d2.intercepted
    assert not any(e.kind == "permission_needed" for e in d2.events), "窗口内重发不得重发卡片"


def test_approval_timeout_denies():
    """超时：移除标记、发 permission_expired、不再发卡片；循环检测恢复计数"""
    ctx = _ctx()
    _log(ctx)
    asyncio.run(_guard().check(ctx))
    sig = LoopDetector._signature(ctx.tool_calls_log[-1])
    # 模拟超过审批时限
    ctx.metadata["permission_pending"][sig] = time.monotonic() - \
        (reins_mod._PERMISSION_APPROVAL_TIMEOUT_S + 1)
    _log(ctx)  # 超时后重发
    d3 = asyncio.run(_guard().check(ctx))
    assert d3.intercepted
    assert any(e.kind == "permission_expired" for e in d3.events), "超时必须发 permission_expired"
    assert not any(e.kind == "permission_needed" for e in d3.events), "超时后不得再发卡片"
    assert sig not in ctx.metadata.get("permission_pending", {}), "超时后必须移除「等待审批」标记"

    # 标记移除后 LLM 仍重试 → 循环检测接管（第 2 次检测即拦截）
    det = LoopDetector()
    d4 = None
    for _ in range(4):
        _log(ctx)
        d4 = asyncio.run(det.check(ctx))
    assert d4 and d4.intercepted, "审批超时拒绝后 LLM 仍重试，循环检测应接管拦截"


# ── driver：执行成功后释放标记 ──

def test_driver_releases_pending_on_success():
    class _NoopTelemetry:
        async def emit(self, ev):
            return None

    class _StubBridle:
        def build_tool_result_message(self, tc_id, text, priority="normal"):
            return {"role": "tool", "tool_call_id": tc_id, "content": text}

    class _StubSaddle:
        async def execute(self, tc, ctx, auto_rollback=True):
            return ToolResult(success=True, output="ok", duration_ms=1), None

    class _StubReins:
        async def check(self, point, ctx):
            from agent.harness.contracts import ReinDecision
            return ReinDecision()

    class _StubHarness:
        bridle = _StubBridle()
        saddle = _StubSaddle()
        reins = _StubReins()
        cost_tracker = None

    ctx = _ctx()
    sig = LoopDetector._signature({"tool": "shell_exec", "args": {"command": CMD}})
    ctx.metadata["permission_pending"] = {sig: time.monotonic()}

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        driver = Driver()
        llm_out = LLMOutput(tool_calls=[ToolCall(id="tc1", name="shell_exec",
                                                 args={"command": CMD})])
        async for ev in driver._execute_tool_calls(
                llm_out, ctx, _StubHarness(), _NoopTelemetry(), 0):
            pass

    asyncio.run(main())
    assert sig not in ctx.metadata.get("permission_pending", {}), (
        "命令执行成功后必须移除「等待审批」标记"
    )
