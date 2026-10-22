"""审批卡弹出后 run 必须真的停住 —— 用户没点，AI 不许继续往下跑。

用户报的现象：提示词写「清理临时日志」，模型发出 `rm -rf /tmp/xxx` 被判 dangerous、
界面弹出授权卡片；**与此同时** run 已经拿着「工具调用被拦截」继续跑下一轮 —— 用户
还在犹豫要不要点，AI 已经把后面的事做完了。审批卡在物理上存在，在时序上不起作用。

本文件钉五件事：
  1. 卡片发出后 Driver 阻塞：命令不执行、run 不结束；
  2. 用户点「允许」→ 本次调用**自动执行**，不再指望模型重发同一行命令；
  3. 用户点「拒绝」→ 走「曾被用户拒绝」拦截，同样不执行；
  4. 授权端点算出的键与 Driver 登记的键是同一把（键一漂移，点了允许 run 就挂死）；
  5. 非交互式 run（定时任务）不阻塞 —— 没人可点卡片，等下去就是挂死。

运行: python -m pytest backend/tests/test_approval_blocking.py -v
"""
import asyncio
import time

import pytest
import routes.agent as agent_mod
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
from agent.harness.reins import approval_wait
from agent.harness.reins.core import AWAIT_APPROVAL_KEY
from agent.harness.reins.gate import PermissionGate

RUN_ID = "run-1"
CMD = "rm -rf /tmp/build-cache"
KEY = "shell_exec:deadbeefdeadbeef"


class _NoopTelemetry:
    async def emit(self, ev):
        return None


class _StubBridle:
    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text}


class _StubSaddle:
    """只数「真的执行了几次」，不跑真实工具。"""

    def __init__(self):
        self.calls = 0

    async def execute(self, tc, ctx, auto_rollback=True):
        self.calls += 1
        return ToolResult(success=True, output="已清理 3 个文件", duration_ms=1), None


class _StubHarness:
    def __init__(self, saddle, reins):
        self.bridle = _StubBridle()
        self.saddle = saddle
        self.reins = reins
        self.cost_tracker = None


class _ApprovalReins:
    """模拟 gate + 守卫：第一次弹卡，之后按用户裁决重判。

    真实实现里重判由 gate/守卫自己完成（授权状态是权威），这里只需要
    「批了就放行、拒了就给出拒绝原因」这一对结论。
    """

    def __init__(self):
        self.verdict = None

    async def check(self, point, ctx):
        if point is not ReinPoint.PRE_TOOL_CALL:
            return ReinDecision()
        if self.verdict is None:
            return ReinDecision(
                intercepted=True,
                intercept_reason="需审批命令: 递归删除",
                events=[TraceEvent(
                    kind="permission_needed",
                    source="test",
                    run_id=ctx.run_id,
                    payload={"tool": "shell_exec", "command": CMD},
                )],
                approval_key=KEY,
            )
        if self.verdict == "allowed":
            return ReinDecision()
        return ReinDecision(
            intercepted=True,
            intercept_reason=f"命令曾被用户拒绝: {CMD}",
        )


class _AlwaysCardReins:
    """永远弹卡的拦截器：用来验证「同一签名只等一次」的兜底。"""

    async def check(self, point, ctx):
        if point is not ReinPoint.PRE_TOOL_CALL:
            return ReinDecision()
        return ReinDecision(
            intercepted=True,
            intercept_reason="需审批命令: 递归删除",
            approval_key=KEY,
        )


class _DenyDriftReins:
    """键漂移：用户点了拒绝，但拒绝标记没落在本次调用上，重判会放行。"""

    def __init__(self):
        self.cards = 0

    async def check(self, point, ctx):
        if point is not ReinPoint.PRE_TOOL_CALL:
            return ReinDecision()
        self.cards += 1
        if self.cards == 1:
            return ReinDecision(
                intercepted=True,
                intercept_reason="需审批命令: 递归删除",
                events=[TraceEvent(
                    kind="permission_needed",
                    source="test",
                    run_id=ctx.run_id,
                    payload={"tool": "shell_exec", "command": CMD},
                )],
                approval_key=KEY,
            )
        return ReinDecision()


@pytest.fixture(autouse=True)
def _clean_waiters():
    approval_wait.reset()
    yield
    approval_wait.reset()


async def _noop_async(*_a, **_k):
    """审计写在纯等待语义的用例里不相关（必须是 async：被测端点会 await 它）"""


def _ctx(await_approval: bool = True) -> RunContext:
    ctx = RunContext.new(task="清理临时日志", config=AgentConfig())
    ctx.run_id = RUN_ID
    if await_approval:
        ctx.metadata[AWAIT_APPROVAL_KEY] = True
    return ctx


def _llm_out() -> LLMOutput:
    return LLMOutput(tool_calls=[
        ToolCall(id="tc-1", name="shell_exec", args={"command": CMD}),
    ])


def _tool_msgs(ctx: RunContext) -> list[dict]:
    return [m for m in ctx.messages if m.get("role") == "tool"]


async def _wait_registered(n: int = 1, timeout: float = 3.0) -> None:
    """等 Driver 真的走到等待点（不要用固定 sleep 赌调度）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(approval_wait.pending_keys()) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Driver 没有在审批点登记等待项")


async def _wait(run_id: str = RUN_ID, key: str = KEY,
                tool: str = "shell_exec", timeout: float = 30) -> str:
    """登记并等待（生产侧是 register → 发卡 → wait_registered，测试无卡片环节）。"""
    waiter = approval_wait.register(run_id, key, tool)
    assert waiter is not None
    return await approval_wait.wait_registered(waiter, timeout)


def _run_tool_calls(ctx, harness):
    """起一个 task 跑 _execute_tool_calls，返回 (task, 已发出的事件列表)。"""

    async def consume():
        events = []
        async for ev in Driver()._execute_tool_calls(
                _llm_out(), ctx, harness, _NoopTelemetry(), 0):
            events.append(ev)
        return events

    return asyncio.create_task(consume())


# ── 1. 卡片弹出后真的停住 ──────────────────────────────────────────────────

def test_卡片发出后命令不执行且_run_停在审批点():
    ctx = _ctx()
    saddle, reins = _StubSaddle(), _ApprovalReins()
    harness = _StubHarness(saddle, reins)

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        task = _run_tool_calls(ctx, harness)
        await _wait_registered()

        assert approval_wait.pending_keys() == [(RUN_ID, KEY)]
        assert saddle.calls == 0, "卡片弹出后、用户点击前，命令绝不能被执行"
        assert not task.done(), "run 必须停在审批点，而不是继续往下跑"

        # 用户点「允许」：本次调用应当直接执行，不依赖模型重发同一行命令
        reins.verdict = "allowed"
        assert approval_wait.resolve(RUN_ID, KEY, approval_wait.ALLOWED)
        await asyncio.wait_for(task, 3)

        assert saddle.calls == 1, "批准后本次调用应自动执行且只执行一次"
        msgs = _tool_msgs(ctx)
        assert msgs and "工具调用被拦截" not in msgs[-1]["content"]
        assert "已清理" in msgs[-1]["content"]
        assert approval_wait.pending_keys() == []

    asyncio.run(main())


# ── 2. 卡片必须先发出、再停住 ────────────────────────────────────────────

def test_卡片事件在阻塞之前发出():
    """反序即死锁：等一个用户看不见的卡片，只能等到兜底超时（默认 600s）。

    permission_needed 挂在 ReinDecision.events 上，要由 driver yield 出去才算
    「用户看得见」。所以顺序只能是「登记 → 发卡 → 等待」。
    """
    ctx = _ctx()
    saddle, reins = _StubSaddle(), _ApprovalReins()
    harness = _StubHarness(saddle, reins)

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        seen: list[TraceEvent] = []

        async def consume():
            async for ev in Driver()._execute_tool_calls(
                    _llm_out(), ctx, harness, _NoopTelemetry(), 0):
                if ev.kind == "permission_needed":
                    # 只能在这里断言：事件刚交到消费者手里、生成器还停在 yield 上。
                    # 等外部轮询再查就恒真了 —— 消费者会立即恢复生成器，登记早已完成。
                    assert approval_wait.pending_keys() == [(RUN_ID, KEY)], \
                        "先发卡后登记：卡片可见那一瞬点「允许」会唤醒落空"
                seen.append(ev)

        task = asyncio.create_task(consume())
        deadline = time.monotonic() + 3
        while not any(e.kind == "permission_needed" for e in seen):
            assert time.monotonic() < deadline, "卡片事件没有在阻塞之前发出"
            await asyncio.sleep(0.01)

        assert not task.done(), "run 应停在审批点"

        reins.verdict = "allowed"
        approval_wait.resolve(RUN_ID, KEY, approval_wait.ALLOWED)
        await asyncio.wait_for(task, 3)
        assert saddle.calls == 1

    asyncio.run(main())


# ── 3. 并行只读段：同一套闸不能漏（两个调用点）──────────────────────────

def test_只读并行段里也停住():
    """连续只读调用走并行段，PRE 检查在 A 阶段串行完成 —— 闸必须在 A 阶段。

    否则审批卡还没点，B 阶段已经 gather 把两个只读命令都跑了。
    """
    ctx = _ctx()
    saddle, reins = _StubSaddle(), _ApprovalReins()
    harness = _StubHarness(saddle, reins)
    llm_out = LLMOutput(tool_calls=[
        ToolCall(id="tc-1", name="file_read", args={"path": "/tmp/a.log"}),
        ToolCall(id="tc-2", name="file_read", args={"path": "/tmp/b.log"}),
    ])

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        seen: list[TraceEvent] = []

        async def consume():
            async for ev in Driver()._execute_tool_calls(
                    llm_out, ctx, harness, _NoopTelemetry(), 0):
                seen.append(ev)

        task = asyncio.create_task(consume())
        deadline = time.monotonic() + 3
        while not any(e.kind == "permission_needed" for e in seen):
            assert time.monotonic() < deadline, "只读段里卡片没有在阻塞之前发出"
            await asyncio.sleep(0.01)

        assert not task.done()
        assert saddle.calls == 0, "卡片未点击前，并行段一个也不得执行"

        reins.verdict = "allowed"
        approval_wait.resolve(RUN_ID, KEY, approval_wait.ALLOWED)
        await asyncio.wait_for(task, 3)
        assert saddle.calls == 2, "批了之后段内两个只读调用都应执行"

    asyncio.run(main())


# ── 4. 拒绝同样不执行 ─────────────────────────────────────────────────────

def test_拒绝后不执行并回灌拒绝原因():
    ctx = _ctx()
    saddle, reins = _StubSaddle(), _ApprovalReins()
    harness = _StubHarness(saddle, reins)

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        task = _run_tool_calls(ctx, harness)
        await _wait_registered()

        reins.verdict = "denied"
        approval_wait.resolve(RUN_ID, KEY, approval_wait.DENIED)
        await asyncio.wait_for(task, 3)

        assert saddle.calls == 0, "用户点了拒绝，命令绝不能被执行"
        msgs = _tool_msgs(ctx)
        assert msgs and "曾被用户拒绝" in msgs[-1]["content"]
        assert ctx.tool_calls_log[-1]["success"] is False

    asyncio.run(main())


# ── 5. 非交互式 run 不阻塞（定时任务没人在卡片上点） ─────────────────────

def test_非交互式_run_不阻塞():
    ctx = _ctx(await_approval=False)
    saddle, reins = _StubSaddle(), _ApprovalReins()
    harness = _StubHarness(saddle, reins)

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        # 没人会唤醒它：若实现漏了开关，这里会一直挂到兜底超时
        await asyncio.wait_for(_run_tool_calls(ctx, harness), 2)
        assert saddle.calls == 0
        assert approval_wait.pending_keys() == []
        assert "工具调用被拦截" in _tool_msgs(ctx)[-1]["content"]

    asyncio.run(main())


# ── 6. 同一签名只等一次：重判仍拦时不能永久挂起 ──────────

def test_拒绝兜底唤醒到不匹配的键时仍不放行():
    """deny 的兜底唤醒（键对不上 → 按工具名）落在本次调用上时，重判会放行。

    场景：用户点的是 A 命令的拒绝，但 A 已超时，driver 正等在 B 命令（同工具）上；
    醒来重判 B 时拒绝标记盖不到它，没有任何人会看见 B 在跑。这里真的走
    resolve_for_call 的同工具兜底分支（传入一把对不上的键）。
    """
    ctx = _ctx()
    saddle = _StubSaddle()
    harness = _StubHarness(saddle, _DenyDriftReins())

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        task = _run_tool_calls(ctx, harness)
        await _wait_registered()
        assert approval_wait.resolve_for_call(
            RUN_ID, "shell_exec", "shell_exec:another-flow", approval_wait.DENIED)
        await asyncio.wait_for(task, 3)

        assert saddle.calls == 0, "用户点了拒绝，哪怕拒绝标记没覆盖到本次调用也不能执行"
        assert "拒绝" in _tool_msgs(ctx)[-1]["content"]

    asyncio.run(main())


# ── 7. 同一签名只等一次：重判仍拦时不能永久挂起 ──────────

def test_兜底超时后不执行也不重判(monkeypatch):
    """超时不能拿一个没人点过的判决去执行 —— 必须落回原拦截路径。"""
    monkeypatch.setattr(
        "agent.harness.driver.approval_fallback_seconds", lambda: 0.05)
    ctx = _ctx()
    saddle, reins = _StubSaddle(), _ApprovalReins()
    harness = _StubHarness(saddle, reins)

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        # 没人点卡：等兜底超时后必须自己走出来
        await asyncio.wait_for(_run_tool_calls(ctx, harness), 3)

        assert saddle.calls == 0, "兜底超时后绝不能拿一个没人点过的判决去执行"
        assert approval_wait.pending_keys() == []
        assert "工具调用被拦截" in _tool_msgs(ctx)[-1]["content"]

    asyncio.run(main())


def test_批了却仍被拦时不再二次等待():
    ctx = _ctx()
    saddle = _StubSaddle()
    harness = _StubHarness(saddle, _AlwaysCardReins())

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        task = _run_tool_calls(ctx, harness)
        await _wait_registered()
        approval_wait.resolve(RUN_ID, KEY, approval_wait.ALLOWED)
        await asyncio.wait_for(task, 3)

        assert saddle.calls == 0
        assert approval_wait.pending_keys() == [], "不得为同一签名再登记一次等待"
        assert "授权范围未覆盖本次调用" in _tool_msgs(ctx)[-1]["content"]

    asyncio.run(main())


# ── 8. 等待表本身 ──────────────────────────────

def test_发卡途中被关闭不留等待项():
    """客户端断连 → 生成器在 yield 处被关闭。登记与等待之间的窗口必须回收。

    内层生成器的 finally 由 asyncgen 终结器在下一轮事件循环调度（实测 aclose()
    返回时尚未执行），所以这里短暂轮询。不回收的话，resolve_for_call 的同工具
    兜底会先命中这个死项并返回 True，真正的唤醒被吞掉。
    """
    ctx = _ctx()
    saddle, reins = _StubSaddle(), _ApprovalReins()
    harness = _StubHarness(saddle, reins)

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        gen = Driver()._execute_tool_calls(
            _llm_out(), ctx, harness, _NoopTelemetry(), 0)
        ev = await gen.__anext__()
        assert ev.kind == "permission_needed"
        assert approval_wait.pending_keys() == [(RUN_ID, KEY)]

        await gen.aclose()  # 模拟客户端断连 / 上层提前 break

        deadline = time.monotonic() + 1
        while approval_wait.pending_keys():
            assert time.monotonic() < deadline, "断连后登记项必须被回收"
            await asyncio.sleep(0.01)
        assert saddle.calls == 0

    asyncio.run(main())


def test_发卡时_telemetry_抛错不留等待项():
    """emit 抛错会从 try 里冒出去 —— 异常路径上也要把登记项收干净。"""

    class _BoomTelemetry:
        async def emit(self, ev):
            if ev.kind == "permission_needed":
                raise RuntimeError("emit boom")

    ctx = _ctx()
    saddle, reins = _StubSaddle(), _ApprovalReins()
    harness = _StubHarness(saddle, reins)

    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        with pytest.raises(RuntimeError, match="emit boom"):
            async for _ in Driver()._execute_tool_calls(
                    _llm_out(), ctx, harness, _BoomTelemetry(), 0):
                pass
        assert approval_wait.pending_keys() == [], "异常路径上登记项必须被回收"
        assert saddle.calls == 0

    asyncio.run(main())


def test_兜底超时后摘除等待项():
    async def main():
        assert await _wait(timeout=0.05) \
            == approval_wait.TIMEOUT
        assert approval_wait.pending_keys() == []

    asyncio.run(main())


def test_run_被取消时摘除等待项():
    async def main():
        task = asyncio.create_task(
            _wait())
        await _wait_registered()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert approval_wait.pending_keys() == []

    asyncio.run(main())


def test_release_run_叫醒等待项():
    async def main():
        task = asyncio.create_task(
            _wait())
        await _wait_registered()
        assert approval_wait.abort_run(RUN_ID) == 1
        assert await task == approval_wait.ABORTED

    asyncio.run(main())


def test_键对不上时按同工具兜底唤醒():
    """键漂移的兜底：用户点了允许、卡片也消失，run 却一直挂着 —— 比多唤醒糟得多。"""
    async def main():
        task = asyncio.create_task(
            _wait())
        await _wait_registered()
        assert approval_wait.resolve_for_call(
            RUN_ID, "shell_exec", "shell_exec:another-flow", approval_wait.ALLOWED)
        assert await task == approval_wait.ALLOWED

    asyncio.run(main())


# ── 9. Driver 与授权端点必须算出同一把键 ──────────────────────────────────

@pytest.fixture
def gate(tmp_path, monkeypatch):
    path = str(tmp_path / "allowlist.txt")
    monkeypatch.setattr("agent.harness.reins.gate.ALLOWLIST_PATH", path)
    g = PermissionGate()
    g.allowlist_ttl_hours = 0
    return g


def test_授权端点算出的键与_driver_登记的是同一把(gate):
    """两边都走 _grant_key。键一旦漂移，用户点了允许、卡片消失，run 却挂着。"""
    driver_key = gate.approval_key_for_call("shell_exec", {"command": CMD}, CMD)
    # 端点侧算键时要带 scope：once 必须配 run_id，否则直接拒收 ——
    # 这里只验证「同一把键」，取 always 不影响哈希口径
    assert driver_key == gate.grant_key_for("shell_exec", CMD, scope="always")
    # 非命令工具（无 bound_command）退化成裸工具名，两端同口径
    assert gate.approval_key_for_call("service_manage", {"action": "stop"}) \
        == gate.grant_key_for("service_manage", None, scope="always") == "service_manage"


def test_grant_端点唤醒等待中的_run(gate, monkeypatch):
    monkeypatch.setattr(
        "agent.harness.reins._get_global_permission_gate_sync", lambda *a, **k: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    key = gate.approval_key_for_call("shell_exec", {"command": CMD}, CMD)

    async def main():
        waiter = asyncio.create_task(
            _wait(key=key))
        await _wait_registered()
        await agent_mod.grant_tool_permission(agent_mod.PermissionGrantRequest(
            tool="shell_exec", scope="session", command=CMD,
            conversation_id="conv-1", run_id=RUN_ID))
        assert await asyncio.wait_for(waiter, 2) == approval_wait.ALLOWED
        # 醒来重判要真的过：session 授权已生效
        assert gate.check_permission(
            "shell_exec", {"command": CMD},
            conversation_id="conv-1", run_id=RUN_ID) is True

    asyncio.run(main())


def test_deny_端点唤醒等待中的_run(gate, monkeypatch):
    monkeypatch.setattr(
        "agent.harness.reins._get_global_permission_gate_sync", lambda *a, **k: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    key = gate.approval_key_for_call("shell_exec", {"command": CMD}, CMD)

    async def main():
        waiter = asyncio.create_task(
            _wait(key=key))
        await _wait_registered()
        await agent_mod.deny_command(agent_mod.DenyCommandRequest(
            tool="shell_exec", command=CMD, run_id=RUN_ID))
        assert await asyncio.wait_for(waiter, 2) == approval_wait.DENIED
        # 拒绝标记先落、再唤醒：醒来重判必须看到「曾被用户拒绝」
        assert gate.is_denied("shell_exec", CMD, RUN_ID) is True

    asyncio.run(main())


def test_release_run_回收审批等待项(gate, monkeypatch):
    monkeypatch.setattr(
        "agent.harness.reins._get_global_permission_gate_sync", lambda *a, **k: gate)

    async def main():
        waiter = asyncio.create_task(
            _wait())
        await _wait_registered()
        agent_mod._release_run_interaction_state(RUN_ID)
        assert await asyncio.wait_for(waiter, 2) == approval_wait.ABORTED

    asyncio.run(main())


# ── 10. 端到端：真守卫 + 真 gate + 真 Driver ───────────────────────────

def test_真守卫发的卡能被真_gate_的授权唤醒(gate, monkeypatch):
    """键漂移是静默崩溃：卡片消失、run 挂到兜底超时，日志里只有「已授权」。

    这里不用 stub 拦截器，直接上 FilesystemGuard + PermissionGate + Driver，
    验三件事：卡片真发出来了、Driver 登记的键就是守卫算的那把、
    gate.grant 返回的键与它相等 —— 然后命令真的执行。
    """
    from types import SimpleNamespace

    import agent.harness.reins.guards as guards_mod
    from agent.harness.reins import FilesystemGuard
    from agent.tools import create_registry

    monkeypatch.setattr(guards_mod, "append_record_async", _noop_async)
    monkeypatch.setattr(guards_mod, "maybe_explain_via_llm", _noop_async)
    gate.saddle = SimpleNamespace(registry=create_registry())
    guard = FilesystemGuard(permission_gate=gate)

    class _GuardReins:
        async def check(self, point, ctx):
            if point is not ReinPoint.PRE_TOOL_CALL:
                return ReinDecision()
            return await guard.check(ctx)

    saddle = _StubSaddle()
    harness = _StubHarness(saddle, _GuardReins())

    async def main():
        ctx = _ctx()
        await ctx.transition(State.ACTING, event="need_tool")
        task = _run_tool_calls(ctx, harness)
        await _wait_registered()

        expected = gate.approval_key_for_call("shell_exec", {"command": CMD}, CMD)
        assert approval_wait.pending_keys() == [(RUN_ID, expected)], \
            "Driver 登记的键必须是守卫算出的那一把"
        assert saddle.calls == 0, "卡片未点击前不得执行"

        granted = gate.grant("shell_exec", scope="always", command=CMD)
        assert granted == expected, "授权端点算出的键必须与 Driver 登记的是同一把"
        approval_wait.resolve_for_call(
            RUN_ID, "shell_exec", granted, approval_wait.ALLOWED)
        await asyncio.wait_for(task, 3)

        assert saddle.calls == 1, "批准后本次调用应直接执行"
        assert "已清理" in _tool_msgs(ctx)[-1]["content"]

    asyncio.run(main())
