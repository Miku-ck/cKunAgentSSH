"""第二轮审查回归：Reins 反馈消费链与澄清等待期的循环豁免。

覆盖的缺陷（审查R2修复，修复前全部静默失效）：
  - Driver 的非拦截分支整体丢弃 PRE_TOOL_CALL 聚合出的 events/inject_messages
    → 澄清向导答案的 [用户澄清结果] 注入与 clarification_done 事件蒸发，
      用户答完向导后模型仍然看不到答案；LoopDetector 第 1 级纠偏同灭。
  - Reins.check 在拦截早退分支丢掉此前已聚合的未拦截反馈
    → 澄清答案（priority 7）注入后若被 FilesystemGuard（priority 10）拦下，注入灭。
  - LoopDetector 的「等待期豁免」只覆盖审批（permission_pending），不覆盖澄清
    → 用户离席不答、模型每轮重发同一命令，3 次检测即 terminate 把 run 打成
      FAILED，之后的答案 POST 成孤儿。

运行: python -m pytest backend/tests/test_harness_feedback_loop.py -v
"""
import asyncio

import pytest

from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    ReinDecision,
    ReinPoint,
    RunContext,
    State,
    ToolCall,
    ToolResult,
    TraceEvent,
)
from agent.harness.driver import Driver
from agent.harness.reins import (
    _CLARIFICATION_CONTEXT,
    _CLARIFICATION_DONE,
    ClarificationInterceptor,
    LoopDetector,
    tool_call_signature,
)
from agent.harness.reins.core import AWAIT_APPROVAL_KEY, Reins

CMD = "rm -rf *"


def _ctx() -> RunContext:
    ctx = RunContext.new(task="测试", config=AgentConfig())
    ctx.metadata[AWAIT_APPROVAL_KEY] = True
    return ctx


def _log_call(ctx: RunContext, tool: str = "shell_exec", command: str = CMD) -> None:
    ctx.tool_calls_log.append({
        "tool": tool,
        "args": {"command": command},
        "iteration": len(ctx.tool_calls_log),
        "success": None,
    })


class _RecordingTelemetry:
    def __init__(self):
        self.emitted = []

    async def emit(self, ev):
        self.emitted.append(ev)


class _StubBridle:
    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text}


class _StubSaddle:
    async def execute(self, tc, ctx, auto_rollback=True):
        return ToolResult(success=True, output="ok", duration_ms=1), None


class _FixedDecisionReins:
    """PRE_TOOL_CALL 返回预置决策，其余点返回空决策"""

    def __init__(self, pre: ReinDecision):
        self._pre = pre

    async def check(self, point, ctx):
        if point == ReinPoint.PRE_TOOL_CALL:
            return self._pre
        return ReinDecision()


class _StubHarness:
    def __init__(self, pre: ReinDecision):
        self.bridle = _StubBridle()
        self.saddle = _StubSaddle()
        self.cost_tracker = None
        self.reins = _FixedDecisionReins(pre)


# ── 一、Driver 必须消费非拦截决策携带的反馈 ──────────────────────────────────

def test_driver_consumes_non_intercepting_pre_tool_feedback():
    ctx = _ctx()
    ev = TraceEvent(kind="clarification_done", source="reins.clarification",
                    run_id=ctx.run_id, iteration=0, payload={"message": "done"})
    injected = {"role": "user", "content": "[用户澄清结果] 目标目录：/tmp/x"}
    decision = ReinDecision(intercepted=False, events=[ev], inject_messages=[injected])
    telemetry = _RecordingTelemetry()

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        driver = Driver()
        llm_out = LLMOutput(tool_calls=[ToolCall(id="tc1", name="shell_exec",
                                                 args={"command": CMD})])
        events = [e async for e in driver._execute_tool_calls(
            llm_out, ctx, _StubHarness(decision), telemetry, 0)]
        return events

    yielded = asyncio.run(main())
    assert any(getattr(e, "kind", "") == "clarification_done" for e in yielded), (
        "非拦截决策的 events 既未 yield 给前端，TraceViewer 里也看不到"
    )
    assert ev in telemetry.emitted, "事件未写入 TelemetryBus"
    assert injected in ctx.messages, "澄清答案未进入消息历史 → 模型看不到用户的答案"


# ── 二、Reins 拦截早退不得丢掉已聚合的反馈 ──────────────────────────────────

class _Feedback:
    """未拦截、只带注入消息/事件的拦截器（澄清答案放行轮的姿态）"""

    def __init__(self, priority: int, point: ReinPoint = ReinPoint.PRE_TOOL_CALL):
        self.point = point
        self.priority = priority
        self.safety_critical = False

    async def check(self, ctx):
        return ReinDecision(
            intercepted=False,
            events=[TraceEvent(kind="clarification_done", source="feedback",
                               run_id=ctx.run_id, iteration=0, payload={})],
            inject_messages=[{"role": "user", "content": "[用户澄清结果] A"}],
        )


class _Blocker:
    def __init__(self, priority: int, point: ReinPoint = ReinPoint.PRE_TOOL_CALL):
        self.point = point
        self.priority = priority
        self.safety_critical = True

    async def check(self, ctx):
        return ReinDecision(intercepted=True, intercept_reason="需要审批",
                            inject_messages=[{"role": "user", "content": "已被拦截"}])


def test_intercept_keeps_earlier_aggregated_feedback():
    reins = Reins()
    reins.register(_Feedback(priority=7))
    reins.register(_Blocker(priority=10))
    decision = asyncio.run(reins.check(ReinPoint.PRE_TOOL_CALL, _ctx()))

    assert decision.intercepted is True
    contents = [m.get("content", "") for m in decision.inject_messages]
    assert "[用户澄清结果] A" in contents, "拦截早退丢掉了先序拦截器已聚合的注入"
    assert any(e.kind == "clarification_done" for e in decision.events)
    # 顺序：先序反馈在前，拦截者的文案在后
    assert contents[0] == "[用户澄清结果] A"


# ── 三、澄清等待期同样豁免循环检测 ──────────────────────────────────────────

def _detector_run(ctx: RunContext, rounds: int):
    det = LoopDetector()

    async def main():
        out = []
        for i in range(rounds):
            _log_call(ctx)
            out.append(await det.check(ctx))
        return out

    return asyncio.run(main())


def test_loop_detector_terminates_repeated_call_without_wait():
    """反向锁：无等待标记时，重复签名仍要按 C-review M6 升级/终止"""
    ctx = _ctx()
    decisions = _detector_run(ctx, 5)
    assert any(d.intercepted for d in decisions), "循环检测不得形同虚设"


def test_loop_detector_exempts_clarification_pending():
    ctx = _ctx()
    sig = tool_call_signature({"tool": "shell_exec", "args": {"command": CMD}})
    ctx.metadata["clarification_pending"] = {sig}
    decisions = _detector_run(ctx, 6)
    assert all(not d.intercepted for d in decisions), (
        "澄清等待期重发同一命令被判循环 → run 被自己打成 FAILED，用户答案变孤儿"
    )


@pytest.fixture
def _clean_clarification():
    yield
    _CLARIFICATION_CONTEXT.clear()
    _CLARIFICATION_DONE.clear()


def test_clarification_interceptor_registers_pending_sig(_clean_clarification):
    """拦截器建卡/重发时必须登记等待签名，答案消费后清除（恢复常态检测）"""
    interceptor = ClarificationInterceptor()
    ctx = _ctx()
    _log_call(ctx)
    sig = tool_call_signature(ctx.tool_calls_log[-1])

    assert (asyncio.run(interceptor.check(ctx))).intercepted is True
    assert sig in ctx.metadata.get("clarification_pending", set()), "建卡未登记等待签名"

    # 用户答完 → done 分支注入并放行，等待标记必须撤销
    _CLARIFICATION_DONE[ctx.run_id] = {"0": {"choice": "A"}}
    info = _CLARIFICATION_CONTEXT.get(ctx.run_id)
    if info:
        info["_done"] = True
    decision = asyncio.run(interceptor.check(ctx))
    assert decision.intercepted is False
    assert sig not in ctx.metadata.get("clarification_pending", set()), (
        "答案已消费仍留着豁免标记 → 该命令从此永不做循环检测"
    )
