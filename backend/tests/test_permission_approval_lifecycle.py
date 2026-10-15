"""C-review M7 回归：审批等待三态处理 + 循环检测豁免 + 审批兜底。

修复前：审批期间 run 继续重发（烧 LLM 调用）；重发签名进 LoopDetector 窗口，
第 4 次重发被循环检测抢截（permission_needed 不再发出）→ 授权卡片孤儿、命令
永远执行不了；无审批超时。
修复后：首次请求发卡片并标记「等待审批」；窗口内重发豁免循环检测、不重发卡片；
兜底（默认 600s）到期发 permission_expired（前端标记 timeout 保留卡片），但**
保留等待集**、不按拒绝处理、不永久封禁，兜底窗口内同命令重发复用待办不重复
建卡（也不被循环检测抢截）；命令执行成功后移除标记。

运行: python -m pytest backend/tests/test_permission_approval_lifecycle.py -v
"""
import asyncio
import time

import pytest

import agent.harness.reins as reins_mod
import agent.harness.reins.flow as _flow_mod
import agent.harness.reins.gate as _gate_mod
import agent.harness.reins.guards as _guards_mod
from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    RunContext,
    State,
    ToolCall,
    ToolResult,
)
from agent.harness.driver import Driver
from agent.harness.reins import FilesystemGuard, LoopDetector, PermissionGate, tool_call_signature

CMD = "systemctl restart nginx"  # 运维动词 → dangerous（非 hardline，走审批路径）


@pytest.fixture(autouse=True)
def _no_file_io(monkeypatch):
    """隔离 allowlist 文件 IO + 审计落库（真实 DB）"""
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)

    async def _noop_audit(**kw):
        return None
    # reins.py 已拆包：patch 必须落在定义 append_record_async 的子模块全局上
    for _m in (_guards_mod, _gate_mod, _flow_mod):
        monkeypatch.setattr(_m, "append_record_async", _noop_audit)


def _ctx() -> RunContext:
    return RunContext.new(task="测试", config=AgentConfig())


def _log(ctx: RunContext, command: str = CMD) -> None:
    ctx.tool_calls_log.append({
        "tool": "shell_exec",
        "args": {"command": command},
        "iteration": len(ctx.tool_calls_log),
        "success": None,
    })


def _guard() -> FilesystemGuard:
    from types import SimpleNamespace
    from agent.tools import create_registry
    gate = PermissionGate(saddle=SimpleNamespace(registry=create_registry()))
    gate._confirmed_permanent = set()
    return FilesystemGuard(permission_gate=gate)


# ── 循环检测豁免 ──

def test_pending_sig_exempt_from_loop_detection():
    """审批窗口内的签名豁免循环检测 — 重发不得被抢截"""
    ctx = _ctx()
    _log(ctx)
    sig = tool_call_signature(ctx.tool_calls_log[-1])
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
    sig = tool_call_signature(ctx.tool_calls_log[-1])
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


def test_approval_fallback_keeps_pending_and_reuses_todo():
    """兜底到期：保留等待集、发 permission_expired、不重复建卡；循环检测豁免仍生效"""
    ctx = _ctx()
    _log(ctx)
    asyncio.run(_guard().check(ctx))
    sig = tool_call_signature(ctx.tool_calls_log[-1])
    # 模拟超过审批兜底时限
    ctx.metadata["permission_pending"][sig] = time.monotonic() - \
        (reins_mod.approval_fallback_seconds() + 1)
    _log(ctx)  # 兜底到期后重发
    d3 = asyncio.run(_guard().check(ctx))
    assert d3.intercepted
    assert any(e.kind == "permission_expired" for e in d3.events), "兜底到期必须发 permission_expired"
    assert not any(e.kind == "permission_needed" for e in d3.events), "到期后不得重复建卡"
    assert sig in ctx.metadata.get("permission_pending", {}), \
        "兜底到期必须保留「等待审批」标记（待办不消失、不按拒绝处理）"

    # 等待标记保留 → 循环检测豁免仍生效：重发复用待办、不被抢截、也不会升级终断
    det = LoopDetector()
    d4 = None
    for _ in range(4):
        _log(ctx)
        d4 = asyncio.run(det.check(ctx))
    assert d4 and not d4.intercepted, \
        "等待标记保留期内同签名重发应仍被循环检测豁免（不重复建卡，也不升级终断）"


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
    sig = tool_call_signature({"tool": "shell_exec", "args": {"command": CMD}})
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


def test_cleanup_run_once_isolated_across_concurrent_runs():
    """回归：run 开始时的防御性清理不得误伤并发 run 在途的 once 授权。

    修复前 driver 每次 run 开始调全局 clear_once()，并发 run B 启动会静默
    清掉 run A 用户已批的"仅本次"授权（"点了允许却仍被拦"跨会话复现）。
    修复后 once 池按 run_id 键控，run 开始只清理本 run_id 的残留池。
    """
    gate = PermissionGate()
    gate.grant("shell_exec", scope="once", command=CMD, run_id="run-A")

    # run-B 启动：只清 run-B 自己的（此时为空的）池
    assert gate.cleanup_run_once("run-B") == 0
    # run-A 的 once 授权必须存活，且消费一次后失效
    assert gate.check_permission("shell_exec", {"command": CMD},
                                 risk="dangerous", run_id="run-A") is True
    assert gate.check_permission("shell_exec", {"command": CMD},
                                 risk="dangerous", run_id="run-A") is False


def test_driver_releases_pending_on_failure():
    """审查R2修复回归：once 授权在 check_permission 放行时已被消费，执行失败
    也必须把签名移出 permission_pending。旧实现只在 success=True 时 pop ——
    失败重试走 is_first=False 分支不再建卡（用户无卡可点），且滞留签名永久
    豁免循环检测。"""
    class _NoopTelemetry:
        async def emit(self, ev):
            return None

    class _StubBridle:
        def build_tool_result_message(self, tc_id, text, priority="normal"):
            return {"role": "tool", "tool_call_id": tc_id, "content": text}

    class _StubSaddle:
        async def execute(self, tc, ctx, auto_rollback=True):
            return ToolResult(success=False, output="", error="boom", duration_ms=1), None

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
    sig = tool_call_signature({"tool": "shell_exec", "args": {"command": CMD}})
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
        "命令执行失败同样要移除「等待审批」标记，否则重试无卡可点且永久豁免循环检测"
    )


def test_driver_releases_pending_on_visible_terminal_delivery(monkeypatch):
    """S2-02 回归：可见终端交付路径也必须移出「等待审批」集。

    该分支 `await self._emit_and_await_visible(...)` 之后直接 `continue`，跳过了
    pop —— 而 ssh-ai.ts 硬编 visibleTerminal: true，这是默认路径而非边角。滞留
    签名的两条后果：① guards 的 is_first=False 分支 events 为空、不再发
    permission_needed（用户无卡可点），注入文案却仍写着「已在界面上向用户请求授权，
    请重新发起完全相同的命令（授权后会被放行）」，空转到兜底时长；② flow.py 对在册
    签名直接 no-op ⇒ 该命令永久豁免循环检测。
    """
    class _NoopTelemetry:
        async def emit(self, ev):
            return None

    class _StubBridle:
        def build_tool_result_message(self, tc_id, text, priority="normal"):
            return {"role": "tool", "tool_call_id": tc_id, "content": text}

    class _StubReins:
        async def check(self, point, ctx):
            from agent.harness.contracts import ReinDecision
            return ReinDecision()

    class _DeliverSaddle:
        async def execute(self, tc, ctx, auto_rollback=True):
            return ToolResult(
                success=True, output="", duration_ms=1,
                pending_delivery={
                    "tool_id": "td-1", "command": CMD,
                    "session_id": "pty-1", "requires_confirm": False,
                },
            ), None

    class _StubHarness:
        bridle = _StubBridle()
        saddle = _DeliverSaddle()
        reins = _StubReins()
        cost_tracker = None

    import agent.pending_delivery as pd_mod

    class _Reg:
        rejected = False
        success = True
        output = "done"
        exit_code = 0

    async def _ok_wait(tool_id):
        return None

    async def _hang_wait(tool_id):
        await asyncio.sleep(1)  # 远超 timeout_of → 走 TimeoutError 收尾

    async def _drive(wait_impl, timeout_s):
        monkeypatch.setattr(pd_mod.PendingDeliveries, "wait", wait_impl)
        monkeypatch.setattr(
            pd_mod.PendingDeliveries, "timeout_of", lambda tool_id: timeout_s)
        monkeypatch.setattr(
            pd_mod.PendingDeliveries, "get", lambda tool_id: _Reg())
        monkeypatch.setattr(
            pd_mod.PendingDeliveries, "remove", lambda tool_id: None)

        ctx = _ctx()
        _log(ctx)  # _finish_visible 要写 tool_calls_log[-1]
        sig = tool_call_signature({"tool": "shell_exec", "args": {"command": CMD}})
        ctx.metadata["permission_pending"] = {sig: time.monotonic()}

        await ctx.transition(State.ACTING, event="need_tool")
        driver = Driver()
        llm_out = LLMOutput(tool_calls=[ToolCall(
            id="tc1", name="shell_exec", args={"command": CMD})])
        async for _ev in driver._execute_tool_calls(
                llm_out, ctx, _StubHarness(), _NoopTelemetry(), 0):
            pass
        return ctx, sig

    for label, wait_impl, timeout_s in (
        ("交付成功", _ok_wait, 5),
        ("交付超时", _hang_wait, 0.01),
    ):
        ctx, sig = asyncio.run(_drive(wait_impl, timeout_s))
        assert sig not in ctx.metadata.get("permission_pending", {}), (
            f"{label}收尾后签名仍留在「等待审批」集：同命令本 run 内不再出卡"
            "且永久豁免循环检测"
        )


def test_denied_command_leaves_no_pending_sig():
    """审查R2修复：拒绝判定必须落在 pending[sig] 记账**之前**。

    旧顺序下被拒命令的签名已先写进 permission_pending，此后该签名永久豁免
    循环检测 —— 模型可以无限刷「已被拒绝」的拦截直到 max_iterations，白烧
    整轮 LLM 调用。"""
    ctx = _ctx()
    _log(ctx)
    guard = _guard()
    guard.permission_gate.deny_command("shell_exec", CMD, ctx.run_id)

    d = asyncio.run(guard.check(ctx))
    assert d.intercepted, "被拒命令仍要拦住"
    assert not any(e.kind == "permission_needed" for e in d.events), \
        "用户已明确点过拒绝，不得再发授权卡片"
    sig = tool_call_signature(ctx.tool_calls_log[-1])
    assert sig not in ctx.metadata.get("permission_pending", {}), \
        "被拒签名不得滞留「等待审批」集（否则从此豁免循环检测）"

    # 豁免面消失后，循环检测必须能接管同一签名的重发
    det = LoopDetector()
    last = None
    for _ in range(5):
        _log(ctx)
        last = asyncio.run(det.check(ctx))
    assert last.intercepted and last.terminate, "被拒命令的重发应由循环检测兜底终止"


# ── 兜底到期的审计落链：两条分支都要有 ──────────────────────────────────────

def _log_tool(ctx: RunContext, tool: str, args: dict) -> None:
    ctx.tool_calls_log.append({
        "tool": tool,
        "args": args,
        "iteration": len(ctx.tool_calls_log),
        "success": None,
    })


def _expire_pending(ctx: RunContext) -> None:
    sig = tool_call_signature(ctx.tool_calls_log[-1])
    ctx.metadata["permission_pending"][sig] = (
        time.monotonic() - (reins_mod.approval_fallback_seconds() + 1))


def test_兜底到期两条分支都落审计链(monkeypatch):
    """命令级（FilesystemGuard）与工具级（PermissionGate）的过期判定必须同等可追。

    安全边界 §6 承诺「AI 试了什么没成功同样可追」，而 §5 给人工交互通道不加事前阻断的
    辩护也建立在这条承诺上。原实现只有守卫那半边在兜底到期时记 `denied`；
    PermissionGate 的同一分支发了 permission_expired 却不落链 —— 于是
    `service_manage` 这类**工具级**危险工具的一次过期审批在审计页里查不到任何痕迹，
    与它自己的 prod 分支（同函数内就有一次 append_record_async）也不一致。
    """
    captured = {"gate": [], "guard": []}

    def _cap(bucket: str):
        async def _f(**kw):
            captured[bucket].append(kw)
            return None
        return _f

    monkeypatch.setattr(_gate_mod, "append_record_async", _cap("gate"))
    monkeypatch.setattr(_guards_mod, "append_record_async", _cap("guard"))

    # 工具级：service_manage（ToolMeta 里就是 dangerous，无 bound_command）
    from types import SimpleNamespace
    from agent.tools import create_registry
    gate = PermissionGate(saddle=SimpleNamespace(registry=create_registry()))
    gate._confirmed_permanent = set()
    ctx = _ctx()
    _log_tool(ctx, "service_manage", {"action": "restart", "service": "nginx"})
    asyncio.run(gate.check(ctx))
    _expire_pending(ctx)
    _log_tool(ctx, "service_manage", {"action": "restart", "service": "nginx"})
    d = asyncio.run(gate.check(ctx))
    assert d.intercepted and any(e.kind == "permission_expired" for e in d.events)
    gate_expired = [r for r in captured["gate"]
                    if str(r.get("result", "")).startswith("approval_expired")]
    assert gate_expired, f"工具级兜底到期没落审计链，抓到：{captured['gate']}"
    assert gate_expired[0]["conclusion"] == "denied"
    assert gate_expired[0]["action"] == "service_manage"

    # 命令级：既有行为，作对照（同一条 result 前缀，同一张审计页）
    g = FilesystemGuard(
        permission_gate=gate, saddle=SimpleNamespace(registry=create_registry()))
    ctx2 = _ctx()
    _log(ctx2)
    asyncio.run(g.check(ctx2))
    _expire_pending(ctx2)
    _log(ctx2)
    asyncio.run(g.check(ctx2))
    guard_expired = [r for r in captured["guard"]
                     if str(r.get("result", "")).startswith("approval_expired")]
    assert guard_expired, "命令级兜底到期同样要落链"
    assert guard_expired[0]["conclusion"] == "denied"
