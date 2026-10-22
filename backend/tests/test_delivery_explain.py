"""交付确认卡解释层（tool_deliver explain）回归。

可见终端模式下交付卡原先只显示命令原文 —— 用户面对一行裸命令无从判断
「这条命令会做什么」（截图一反馈）。现在 Driver 在发 tool_deliver 事件前
跑一次分类，把中文后果说明（与审批卡同款解释层，纯展示、不参与放行判定）
塞进 payload；「未知命令」族再由 maybe_explain_via_llm 带 tool_id 异步补发
permission_explain 回填（截图二 (null) 连刷事故的边界修复在本文件末尾）。

运行: python -m pytest backend/tests/test_delivery_explain.py -v
"""
import asyncio
from types import SimpleNamespace

import agent.harness.driver as driver_mod
import agent.pending_delivery as pd_mod
from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    RunContext,
    State,
    ToolCall,
    ToolResult,
    TraceEvent,
)
from agent.harness.driver import Driver
from agent.harness.safety import classify_command
from agent.harness.telemetry import SSEAdapter
from llm_adapter import ToolCall as RawToolCall
from agent.tools.builtin.shell_exec import ShellExecTool

# rm 非递归删敏感路径 → "delete of sensitive system path or home"（后果表命中，needs_llm=False）
DANGEROUS_CMD = "rm /etc/nginx/nginx.conf"
SAFE_CMD = "ls -la"
# 不在只读白名单 → "unknown command, default approval (…)" 族（needs_llm=True）
UNKNOWN_CMD = "mycohortool --run"


class _CapTelemetry:
    def __init__(self):
        self.events = []

    async def emit(self, ev):
        self.events.append(ev)


class _StubBridle:
    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text}


class _StubReins:
    async def check(self, point, ctx):
        from agent.harness.contracts import ReinDecision
        return ReinDecision()


class _DeliverSaddle:
    def __init__(self, command):
        self.command = command

    async def execute(self, tc, ctx, auto_rollback=True):
        return ToolResult(
            success=True, output="", duration_ms=1,
            pending_delivery={
                "tool_id": "td-1", "command": self.command,
                "session_id": "pty-1", "requires_confirm": False,
            },
        ), None


class _Reg:
    rejected = False
    success = True
    output = "done"
    exit_code = 0


async def _ok_wait(tool_id):
    return None


def _drive(monkeypatch, command):
    """驱动一次可见终端交付，返回 (yielded_events, telemetry, llm_spy_calls)"""
    llm_calls = []

    async def _spy(ctx, tool, cmd, display_cmd, env, target_tool_id=None):
        llm_calls.append({"tool": tool, "command": cmd, "tool_id": target_tool_id})

    monkeypatch.setattr(driver_mod, "maybe_explain_via_llm", _spy)
    monkeypatch.setattr(pd_mod.PendingDeliveries, "wait", _ok_wait)
    monkeypatch.setattr(pd_mod.PendingDeliveries, "timeout_of", lambda tool_id: 5)
    monkeypatch.setattr(pd_mod.PendingDeliveries, "get", lambda tool_id: _Reg())
    monkeypatch.setattr(pd_mod.PendingDeliveries, "remove", lambda tool_id: None)

    async def main():
        ctx = RunContext.new(task="测试", config=AgentConfig())
        ctx.tool_calls_log.append({
            "tool": "shell_exec", "args": {"command": command},
            "iteration": 0, "success": None,
        })
        await ctx.transition(State.ACTING, event="need_tool")
        harness = SimpleNamespace(
            bridle=_StubBridle(), saddle=_DeliverSaddle(command),
            reins=_StubReins(), cost_tracker=None,
        )
        tel = _CapTelemetry()
        llm_out = LLMOutput(tool_calls=[ToolCall(
            id="tc1", name="shell_exec", args={"command": command})])
        events = []
        async for ev in Driver()._execute_tool_calls(llm_out, ctx, harness, tel, 0):
            events.append(ev)
        return events, tel

    events, tel = asyncio.run(main())
    return events, tel, llm_calls


def _deliver_event(events):
    return next(e for e in events if e.kind == "tool_deliver")


def test_dangerous_delivery_carries_static_explain(monkeypatch):
    """非 safe 且后果表命中的命令：payload 带中文后果说明（source=static）"""
    events, tel, llm_calls = _drive(monkeypatch, DANGEROUS_CMD)
    ev = _deliver_event(events)
    assert ev.payload["explain_source"] == "static"
    assert "删除" in ev.payload["explain"], "后果说明必须是后果表的中文文案"
    # needs_llm=False 的族不触发 LLM 兜底
    assert llm_calls == []
    # telemetry.emit 与 yield 的是同一事件，payload 一致
    assert tel.events and tel.events[-1] is ev or any(
        e.kind == "tool_deliver" for e in tel.events)


def test_safe_delivery_explain_is_readonly_copy(monkeypatch):
    """safe 命令（reason=None）用只读白名单确定文案，不落「无法预知」的误导 fallback"""
    events, _tel, llm_calls = _drive(monkeypatch, SAFE_CMD)
    ev = _deliver_event(events)
    assert ev.payload["explain_source"] == "static"
    assert "只读" in ev.payload["explain"]
    assert "无法" not in ev.payload["explain"]
    assert llm_calls == []


def test_unknown_command_delivery_triggers_llm_with_tool_id(monkeypatch):
    """未知命令族：静态文案照常下发，LLM 兜底带 target_tool_id 供前端精确回填"""
    events, _tel, llm_calls = _drive(monkeypatch, UNKNOWN_CMD)
    ev = _deliver_event(events)
    assert ev.payload["explain"], "卡片先带静态文案弹出，解释随后异步补"
    assert len(llm_calls) == 1
    assert llm_calls[0]["tool_id"] == ev.payload["tool_id"]
    assert llm_calls[0]["command"] == UNKNOWN_CMD


def test_tool_deliver_projection_passes_explain():
    """SSE 投影透传 explain / explain_source，前端交付卡才读得到"""
    ev = TraceEvent(
        kind="tool_deliver", source="saddle", run_id="r1", iteration=0,
        payload={"tool_id": "t1", "command": "ls", "session_id": "s1",
                 "requires_confirm": True,
                 "explain": "这是一条只读命令。", "explain_source": "static"},
    )
    d = SSEAdapter.to_sse_dict(ev)
    assert d["explain"] == "这是一条只读命令。"
    assert d["explain_source"] == "static"
    assert d["tool_id"] == "t1"


def test_permission_explain_projection_carries_tool_id_only_when_present():
    """交付回填的 permission_explain 带 tool_id；审批卡回填（无 tool_id）不出现该键"""
    delivery_ev = TraceEvent(
        kind="permission_explain", source="harness.command_explain",
        run_id="r1", iteration=0,
        payload={"tool": "shell_exec", "command": "x", "normalized_command": "x",
                 "explanation": "说明", "tool_id": "td-9"},
    )
    d = SSEAdapter.to_sse_dict(delivery_ev)
    assert d["tool_id"] == "td-9"
    assert d["explanation"] == "说明"

    approval_ev = TraceEvent(
        kind="permission_explain", source="harness.command_explain",
        run_id="r1", iteration=0,
        payload={"tool": "shell_exec", "command": "x", "normalized_command": "x",
                 "explanation": "说明"},
    )
    d2 = SSEAdapter.to_sse_dict(approval_ev)
    assert "tool_id" not in d2


def test_parsed_args_non_dict_json_returns_empty():
    """arguments 为合法 JSON 但非对象（"null"/数字/字符串）按缺失处理 ——
    原实现 json.loads 成功返回非 dict，下游 args.get 直接 AttributeError；
    模型发出 arguments="null" 时曾产出 unknown command (null) 四连刷拒绝"""
    assert RawToolCall(id="c", name="shell_exec", arguments="null").parsed_args() == {}
    assert RawToolCall(id="c", name="shell_exec", arguments="3").parsed_args() == {}
    assert RawToolCall(id="c", name="shell_exec", arguments='"str"').parsed_args() == {}
    # 正常对象不受影响
    assert RawToolCall(id="c", name="shell_exec",
                       arguments='{"command":"ls"}').parsed_args() == {"command": "ls"}


def test_shell_exec_rejects_blank_command():
    """边界校验：command 缺失/None/空白 → 可读失败，不进分类链（classify(None) 会 TypeError）"""
    async def main():
        tool = ShellExecTool()
        for bad in (None, "", "   "):
            r = await tool.execute(SimpleNamespace(), bad)
            assert not r.success
            assert "命令参数缺失" in (r.error or "")
    asyncio.run(main())


def test_readonly_find_stays_safe():
    """钉住：无 -delete/-exec 的 find 不判危险（图二误伤排查结论 —— 检测无 bug，
    文案已人话化；此测试防后续漂移把只读 find 误升级）"""
    assert classify_command(
        'find /home/admin-pl -maxdepth 3 -name "*.log" -o -name "*.log.*"') == ("safe", None)
    assert classify_command(
        'find /home/admin-pl/.trae-cn-server -name "*.log" -mtime +7') == ("safe", None)
    # 对照：带 -delete 仍按危险处理
    level, reason = classify_command(
        'find /home/admin-pl/.trae-cn-server -name "*.log.*" -delete')
    assert level == "dangerous" and "-delete/-exec" in reason
