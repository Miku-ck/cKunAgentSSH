"""澄清卡片待答期间 run 必须真的停住（对齐审批等待闸）。

用户报的现象：意图模糊、界面弹出澄清向导后，AI 仍在继续输出 —— 因为
ClarificationInterceptor 只拦下当次工具调用，Driver 把「工具调用被拦截」
回灌给模型就直接进入下一轮；向导显示期间还会跑只读工具，严重时轮次耗尽、
run 先结束，用户答完的答案落到已终止的 run 上被丢弃。

本文件钉五件事：
  1. 澄清未答时不得进入下一轮 LLM、不得执行工具、run 不得结束；
  2. 作答后重判放行，答案注入下一轮，原工具执行；
  3. 非交互式 run（定时任务）不等待 —— 没人可答，等下去就是挂死；
  4. 澄清放行后若落到权限门，审批等待闸仍要生效（不会因重判绕过）；
  5. 等待超时落回原拦截路径，run 继续但不重复挂起。

运行: python -m pytest backend/tests/test_clarification_blocking.py -v
"""
import asyncio
import time

import pytest

import agent.harness.reins.clarification as clarification_mod
from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    ReinDecision,
    ReinPoint,
    RunContext,
    ToolCall,
    ToolResult,
    TraceEvent,
)
from agent.harness.driver import Driver
from agent.harness.reins import approval_wait
from agent.harness.reins.clarification import (
    ClarificationInterceptor,
    _CLARIFICATION_CONTEXT,
    _CLARIFICATION_DONE,
    record_clarification_answers,
)
from agent.harness.reins.core import AWAIT_APPROVAL_KEY

VAGUE = "rm -rf *"
APPROVAL_KEY = "shell_exec:deadbeefdeadbeef"


class _NoopTelemetry:
    async def emit(self, ev):
        return None


class _StubBridle:
    async def assemble(self, ctx):
        return list(ctx.messages)

    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text}


class _StubSaddle:
    def __init__(self):
        self.executed = []

    def schemas(self):
        return []

    async def execute(self, tc, ctx, auto_rollback=True):
        self.executed.append(tc.name)
        return ToolResult(success=True, output="ok", duration_ms=1), None


class _ScriptedStirrups:
    """前 N 轮发模糊命令，之后一律纯文本；记录每轮看到的 messages。"""

    def __init__(self, tool_rounds: int = 1):
        self.rounds = 0
        self.round_messages = []
        self.round_started = asyncio.Event()
        self._tool_rounds = tool_rounds

    async def call_llm(self, ctx, messages, tools_schema):
        self.rounds += 1
        self.round_messages.append(list(messages))
        if self.rounds <= self._tool_rounds:
            return LLMOutput(tool_calls=[
                ToolCall(id=f"tc{self.rounds}", name="shell_exec",
                         args={"command": VAGUE}),
            ])
        self.round_started.set()
        return LLMOutput(content="等待期间不应出现的输出")

    async def undo_all(self, ctx):
        return None


class _ClarReins:
    def __init__(self):
        self.clar = ClarificationInterceptor()

    async def check(self, point, ctx):
        if point is ReinPoint.PRE_TOOL_CALL:
            return await self.clar.check(ctx)
        return ReinDecision()


class _ClarThenApprovalReins:
    """澄清放行后模拟权限门发卡；授权后放行（复刻真实链路的两个等待闸）。"""

    def __init__(self):
        self.clar = ClarificationInterceptor()
        self.cards = 0

    async def check(self, point, ctx):
        if point is not ReinPoint.PRE_TOOL_CALL:
            return ReinDecision()
        decision = await self.clar.check(ctx)
        if decision.intercepted:
            return decision
        self.cards += 1
        if self.cards == 1:
            decision.intercepted = True
            decision.intercept_reason = "需审批命令: 递归删除"
            decision.approval_key = APPROVAL_KEY
            decision.events = [TraceEvent(
                kind="permission_needed",
                source="test",
                run_id=ctx.run_id,
                payload={"tool": "shell_exec", "command": VAGUE},
            )]
        return decision


class _Harness:
    def __init__(self, reins=None):
        self.telemetry = _NoopTelemetry()
        self.bridle = _StubBridle()
        self.saddle = _StubSaddle()
        self.stirrups = _ScriptedStirrups()
        self.reins = reins or _ClarReins()
        self.cost_tracker = None


def _ctx(interactive: bool, max_iterations: int = 2) -> RunContext:
    config = AgentConfig(
        max_iterations=max_iterations,
        enable_intention_guide=False,
    )
    ctx = RunContext.new(task="清理临时文件", config=config)
    if interactive:
        ctx.metadata[AWAIT_APPROVAL_KEY] = True
    return ctx


async def _collect(harness: _Harness, ctx: RunContext):
    events = []
    async for ev in Driver().drive(ctx, harness):
        events.append(ev)
    return events


async def _wait_until(pred, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def _cleanup(run_id: str) -> None:
    _CLARIFICATION_CONTEXT.pop(run_id, None)
    _CLARIFICATION_DONE.pop(run_id, None)
    approval_wait.reset()


@pytest.mark.asyncio
async def test_pending_clarification_blocks_until_answer():
    """卡片待答：第二轮 LLM 不得启动、工具不得执行、run 不得结束。"""
    ctx = _ctx(interactive=True, max_iterations=2)
    harness = _Harness()
    task = asyncio.create_task(_collect(harness, ctx))
    try:
        assert await _wait_until(lambda: ctx.run_id in _CLARIFICATION_CONTEXT)
        await asyncio.sleep(0.3)

        assert harness.stirrups.rounds == 1, "澄清未答时不得进入下一轮 LLM"
        assert not harness.stirrups.round_started.is_set()
        assert harness.saddle.executed == [], "澄清未答时不得执行工具"
        assert not task.done(), "澄清未答时 run 不得结束"

        record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}})
        events = await asyncio.wait_for(task, 3)
    finally:
        _cleanup(ctx.run_id)

    assert harness.stirrups.rounds == 2
    assert harness.saddle.executed == ["shell_exec"], "作答后应执行原工具"
    assert harness.stirrups.round_started.is_set()
    assert any(ev.kind == "clarification_done" for ev in events)
    second_round_users = [
        m.get("content", "") for m in harness.stirrups.round_messages[-1]
        if m.get("role") == "user"
    ]
    assert any("[用户澄清结果]" in c for c in second_round_users), second_round_users


@pytest.mark.asyncio
async def test_unattended_run_does_not_wait():
    """定时任务无人可答：不得挂起，按原路径继续。"""
    ctx = _ctx(interactive=False, max_iterations=2)
    harness = _Harness()

    events = await asyncio.wait_for(_collect(harness, ctx), 2)

    assert harness.stirrups.rounds == 2
    assert any(ev.kind == "clarification_needed" for ev in events)


@pytest.mark.asyncio
async def test_wait_times_out_without_hanging(monkeypatch):
    """无人作答：TTL 到点落回原拦截路径，run 继续但不重复挂起。

    超时同时要收干净：后端待答状态必须回收（否则用户迟到的答案会写进一张
    已无人等待的题，被后面的无关调用消费），前端要收到 expired 的
    clarification_done（否则向导关闭看起来和「答案已生效」一模一样）。
    """
    monkeypatch.setattr(clarification_mod, "_CLARIFICATION_TTL_SEC", 0.3)
    ctx = _ctx(interactive=True, max_iterations=2)
    harness = _Harness()

    started = time.monotonic()
    events = await asyncio.wait_for(_collect(harness, ctx), 3)
    elapsed = time.monotonic() - started

    assert elapsed < 2, "澄清等待必须受 TTL 约束"
    assert harness.stirrups.rounds == 2
    assert harness.saddle.executed == [], "超时未答的命令不得执行"
    done_events = [ev for ev in events if ev.kind == "clarification_done"]
    assert len(done_events) == 1
    assert done_events[0].payload.get("expired") is True
    assert done_events[0].payload.get("tool") == "shell_exec"
    assert ctx.run_id not in _CLARIFICATION_CONTEXT, "超时须回收待答状态"
    assert ctx.run_id not in _CLARIFICATION_DONE
    _cleanup(ctx.run_id)


@pytest.mark.asyncio
async def test_retry_after_answer_asks_and_waits_again():
    """答完的第一条命令若被模型重发，仍要重新询问并重新停住。"""
    ctx = _ctx(interactive=True, max_iterations=3)
    harness = _Harness()
    harness.stirrups = _ScriptedStirrups(tool_rounds=2)
    task = asyncio.create_task(_collect(harness, ctx))
    try:
        assert await _wait_until(lambda: ctx.run_id in _CLARIFICATION_CONTEXT)
        record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}})

        assert await _wait_until(lambda: harness.stirrups.rounds == 2)
        assert await _wait_until(lambda: ctx.run_id in _CLARIFICATION_CONTEXT)
        await asyncio.sleep(0.3)
        assert harness.stirrups.rounds == 2, "第二次澄清未答时不得进入下一轮"
        assert harness.saddle.executed == ["shell_exec"]

        record_clarification_answers(ctx.run_id, {"0": {"choice": "B"}})
        await asyncio.wait_for(task, 3)
    finally:
        _cleanup(ctx.run_id)

    assert harness.saddle.executed == ["shell_exec", "shell_exec"]
    assert harness.stirrups.rounds == 3


@pytest.mark.asyncio
async def test_permission_card_after_answer_still_blocks():
    """澄清放行后落到权限门：审批等待闸仍须生效。"""
    ctx = _ctx(interactive=True, max_iterations=3)
    reins = _ClarThenApprovalReins()
    harness = _Harness(reins=reins)
    task = asyncio.create_task(_collect(harness, ctx))
    try:
        assert await _wait_until(lambda: ctx.run_id in _CLARIFICATION_CONTEXT)
        record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}})
        assert await _wait_until(lambda: reins.cards == 1)
        await asyncio.sleep(0.3)

        assert not task.done(), "审批未裁决时 run 不得结束"
        assert harness.saddle.executed == []
        assert harness.stirrups.rounds == 1

        assert approval_wait.resolve(ctx.run_id, APPROVAL_KEY, approval_wait.ALLOWED)
        await asyncio.wait_for(task, 3)
    finally:
        _cleanup(ctx.run_id)

    assert reins.cards == 2
    assert harness.saddle.executed == ["shell_exec"]
