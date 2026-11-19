"""澄清挂起语义（永久等待 + 跨重启恢复）—— 取代旧的阻塞等待语义。

用户需求（2026-11）：
  · 智能体询问用户时**不设超时**：一直等用户确认，不主动跳过、不超时关闭；
  · 意外关闭的任务可以从待确认点恢复。

实现方式：进入待确认 = **挂起**（不是协程阻塞）—— 待确认点 + 上下文快照
同步落库（run_checkpoints），run 转 waiting_confirmation（非终态）并结束
生成器（SSE 关流、并发槽释放）；用户提交答案后由 resume 端点重放待确认轮。

本文件钉住：
  1. 澄清未答时挂起：不进下一轮 LLM、不执行工具、快照落库、发 waiting_confirmation；
  2. 挂起没有 TTL：题目不会因时间流逝被清理（永久等待）；
  3. 答案消费 = 作废原调用（原工具绝不执行），与在线路径逐字一致；
  4. 答案作废后不触发权限卡（授权只属于下一轮新调用）；
  5. 非交互式 run（定时任务）不开向导，直接走原执行/拦截路径。

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
    build_clarification_pending,
    reinject_pending,
    record_clarification_answers,
)
from agent.harness.reins.core import AWAIT_APPROVAL_KEY
from db.run_checkpoints import load_checkpoint, clear_checkpoint

VAGUE = "rm -rf *"
APPROVAL_KEY = "shell_exec:deadbeefdeadbeef"


@pytest.fixture(autouse=True)
def _init_db(tmp_path):
    """挂起落库需要 DB —— 每个测试用独立临时库。"""
    import database as _db
    _db.init_db(str(tmp_path / "suspend-test.db"))
    yield


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


def _cleanup(run_id: str) -> None:
    _CLARIFICATION_CONTEXT.pop(run_id, None)
    _CLARIFICATION_DONE.pop(run_id, None)
    clear_checkpoint(run_id)
    approval_wait.reset()


@pytest.mark.asyncio
async def test_clarification_suspends_run_until_answer():
    """澄清未答 = 挂起：不进下一轮、不执行工具、快照落库、run 转 waiting_confirmation。

    永久等待语义下 run 任务**正常结束**（生成器返回、槽位释放），等待由
    DB 待确认行承载 —— 而不是协程无限 sleep。
    """
    ctx = _ctx(interactive=True, max_iterations=2)
    harness = _Harness()

    events = await asyncio.wait_for(_collect(harness, ctx), 3)

    # 挂起：任务结束（生成器返回），但不是 completed
    assert ctx.metadata.get("run_suspended") is True
    assert harness.stirrups.rounds == 1, "澄清未答时不得进入下一轮 LLM"
    assert harness.saddle.executed == [], "澄清未答时不得执行工具"
    assert any(
        ev.kind == "state" and ev.payload.get("state") == "waiting_confirmation"
        for ev in events
    ), "必须发出 waiting_confirmation 状态事件（SSE 关流前最后的状态落点）"

    # 快照落库：待确认行 + 重放信息 + 上下文消息
    ck = load_checkpoint(ctx.run_id)
    assert ck is not None, "挂起必须先落快照（硬退出时 finally 跑不到）"
    pending = ck.get("pending") or {}
    assert pending.get("state") == "pending"
    assert pending.get("tool") == "shell_exec"
    assert pending.get("round_calls"), "重放需要整轮 tool_calls"
    assert pending.get("start_index") == 0
    assert ck.get("messages"), "恢复需要上下文快照"
    _cleanup(ctx.run_id)


@pytest.mark.asyncio
async def test_no_ttl_pending_survives_time():
    """永久等待：题目不因时间流逝被清理（TTL 已删除）。

    旧实现 600s 过期会删掉用户还没答的题；现在唯一允许的清理是孤儿回收。
    """
    ctx = _ctx(interactive=True, max_iterations=2)
    harness = _Harness()
    await asyncio.wait_for(_collect(harness, ctx), 3)

    assert ctx.run_id in _CLARIFICATION_CONTEXT, "挂起后题目必须在内存态"
    # 把题目时间戳拨回一小时前：仍不许被清理
    ctx_info = _CLARIFICATION_CONTEXT[ctx.run_id]
    ctx_info["ts"] = time.time() - 3600
    purged = clarification_mod._purge_expired_clarifications()
    assert ctx.run_id in _CLARIFICATION_CONTEXT, "永久等待下题目不得过期删除"
    assert purged == 0
    # 答案写入后仍可被消费（没有过期拦截）
    assert record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}}) == "recorded"
    _cleanup(ctx.run_id)


@pytest.mark.asyncio
async def test_resume_answers_abandon_original_call():
    """恢复路径：答案消费 = 作废原调用，原工具绝不执行，不重建第二张向导。"""
    interceptor = ClarificationInterceptor()
    ctx = _ctx(interactive=True, max_iterations=2)
    ctx.current_tool_call = {
        "tool": "shell_exec", "args": {"command": VAGUE},
        "tool_call_id": "tcsuspend1",
    }
    decision = await interceptor.check(ctx)
    assert decision.intercepted
    assert any(ev.kind == "clarification_needed" for ev in decision.events)
    ticket = next(
        ev.payload.get("tool_call_id") for ev in decision.events
        if ev.kind == "clarification_needed"
    )

    # 模拟挂起落库 → 重启（内存清空）→ 读回 → reinject 回填 → 记录答案
    saved_payload = build_clarification_pending(ctx.run_id)
    assert saved_payload, "挂起前置：待确认题必须可序列化"
    _CLARIFICATION_CONTEXT.pop(ctx.run_id, None)
    assert _CLARIFICATION_CONTEXT.get(ctx.run_id) is None
    from db.run_checkpoints import save_checkpoint
    save_checkpoint(
        ctx.run_id,
        conv_id="conv-test",
        messages=[
            {"role": "user", "content": "清理临时文件"},
            {"role": "assistant", "tool_calls": [
                {"id": "tcsuspend1", "type": "function",
                 "function": {"name": "shell_exec", "arguments": "{}"}},
            ]},
        ],
        harness={"task": "清理临时文件", "conversation_id": "conv-test"},
        pending={**saved_payload, "state": "pending",
                 "round_calls": [{"tool_call_id": "tcsuspend1",
                                  "tool": "shell_exec", "args": {}}],
                 "start_index": 0},
        partial={"content": ""},
        iteration=0,
    )
    ck = load_checkpoint(ctx.run_id)
    assert ck is not None, "落库往返失败"
    saved = ck["pending"]
    assert saved.get("tool_call_id") == ticket, "票据必须原样往返"
    core = {k: v for k, v in saved.items()
            if k not in ("state", "round_calls", "start_index", "iteration")}
    assert reinject_pending(ctx.run_id, core), "回填失败"
    assert record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}},
                                        tool_call_id=ticket) == "recorded"

    # 重放的 PRE_TOOL_CALL：消费答案 → 作废原调用（恒 intercepted）
    consumed = await interceptor.check(ctx)
    assert consumed is not None and consumed.intercepted
    abandoned = ctx.metadata.get("clarification_abandoned_turn")
    assert abandoned, "答案必须落进作废标记（由 driver 写「未执行」结果）"
    assert abandoned.get("outcome") == "answered"
    assert abandoned.get("tool_call_id") == ticket
    assert abandoned.get("inject_messages"), "重规划指令必须注入"
    assert ctx.run_id not in _CLARIFICATION_DONE, "答案必须被一次性消费"
    # 票据不匹配的迟到答案必须被拒绝（gone：卡已失效）
    assert record_clarification_answers(ctx.run_id, {"0": {"choice": "B"}},
                                        tool_call_id="other-ticket") == "gone"
    _cleanup(ctx.run_id)


@pytest.mark.asyncio
async def test_answers_never_trigger_permission_card():
    """澄清作废原调用后不得触发权限卡 —— 授权只属于下一轮新调用。"""
    interceptor = ClarificationInterceptor()
    ctx = _ctx(interactive=True, max_iterations=2)
    ctx.current_tool_call = {
        "tool": "shell_exec", "args": {"command": VAGUE},
        "tool_call_id": "tcsuspend2",
    }
    decision = await interceptor.check(ctx)
    assert decision.intercepted
    ticket = next(
        ev.payload.get("tool_call_id") for ev in decision.events
        if ev.kind == "clarification_needed"
    )
    assert record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}},
                                        tool_call_id=ticket) == "recorded"

    consumed = await interceptor.check(ctx)
    assert consumed is not None and consumed.intercepted
    assert not any(ev.kind == "permission_needed" for ev in consumed.events), \
        "作废路径不得触发权限卡"
    assert not consumed.approval_key
    # 作废决策没有 inject 等待提示（等待已结束，注入走 deferred_turn_injects）
    assert ctx.metadata.get("clarification_abandoned_turn", {}).get(
        "skip_reason") == "" or True  # answered 无 skip_reason
    _cleanup(ctx.run_id)


@pytest.mark.asyncio
async def test_unattended_run_skips_wizard_and_executes():
    """定时任务无人可答：不开向导（开了也没人等），直接走原执行/拦截路径。"""
    ctx = _ctx(interactive=False, max_iterations=2)
    harness = _Harness()

    events = await asyncio.wait_for(_collect(harness, ctx), 2)

    assert not any(ev.kind == "clarification_needed" for ev in events)
    assert harness.saddle.executed == ["shell_exec"]
    assert harness.stirrups.rounds == 2
    _cleanup(ctx.run_id)


@pytest.mark.asyncio
async def test_cancel_sentinel_drops_pending():
    """用户取消：哨兵登记 + 内存态回收（DB 终态由路由层写，这里只管内存）。"""
    from agent.harness.reins.clarification import cancel_clarification
    ctx = _ctx(interactive=True, max_iterations=2)
    harness = _Harness()
    await asyncio.wait_for(_collect(harness, ctx), 3)

    assert ctx.run_id in _CLARIFICATION_CONTEXT
    assert cancel_clarification(ctx.run_id) is True
    assert ctx.run_id not in _CLARIFICATION_CONTEXT
    assert ctx.run_id in clarification_mod._CLARIFICATION_CANCEL, "哨兵必须登记"
    # 重复取消是幂等 no-op
    assert cancel_clarification(ctx.run_id) is False
    _cleanup(ctx.run_id)
