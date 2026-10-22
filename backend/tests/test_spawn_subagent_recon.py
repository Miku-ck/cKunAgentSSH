"""侦察子 Agent — 工具面白名单、治理链强制接入、循环上限与诚实上报。

背景：spawn_subagent 原先是一次无工具纯推理调用。升级后子 Agent 在自己的
上下文里执行只读观测工具（contracts.READONLY_TOOLS）做取证、只回结论。
本文件钉七件事：
  1. 治理通道（governance）缺失 → 拒绝启动，绝不降级成免审路径；
  2. 工具面双重封死：LLM 只拿到白名单 schema；白名单外调用即使拦截器放行
     也在本地被拒；
  3. 侦察循环正常收敛：观测 → 结论，子 saddle 收到的 ctx 继承父 run 的
     ssh_session_id/environment、visible_terminal=False、metadata["saddle"] 注入；
  4. PRE 拦截的成员不执行、拦截原因回给子模型、其余成员照常、消息按序配对；
  5. 每次工具调用都触发 POST_TOOL_CALL（审计落链，与主 run 同一条链）；
  6. 子 LLM 每轮 token 计入主 Agent CostTracker；
  7. 迭代上限走收尾总结（明确标注未验证项）；空结论/空正文按失败上抛，
     绝不包装成「分析结果」。

运行: python -m pytest backend/tests/test_spawn_subagent_recon.py -v
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent.harness.contracts import LLMOutput, ReinDecision, ReinPoint, ToolCall, ToolResult
from agent.tools import ToolContext
from agent.tools.builtin.spawn_subagent import SpawnSubagentTool

RUN_ID = "sub-run-1"


# ─────────────────────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────────────────────

class _ScriptedLLM:
    """按脚本依次返回 LLMOutput；记录每次调用收到的 tools 与 messages。"""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []          # (tools_names, last_message_role)

    async def call(self, messages, tools, model, temperature, max_tokens,
                   enable_thinking, **kwargs):
        self.calls.append((
            [s["function"]["name"] for s in (tools or [])],
            messages[-1]["role"] if messages else "",
        ))
        if not self.scripted:
            raise RuntimeError("脚本耗尽")
        return self.scripted.pop(0)


class _RecordingReins:
    """记录每个检查点；可按工具名拦截 PRE。"""

    def __init__(self, intercept_names=()):
        self.seen = []                    # (point.value, tool, ctx_id)
        self.intercept_names = set(intercept_names)

    async def check(self, point, ctx):
        entry = ctx.active_tool_call
        tool = str(entry.get("tool") or "") if entry else ""
        self.seen.append((point.value, tool, id(ctx)))
        if point is ReinPoint.PRE_TOOL_CALL and tool in self.intercept_names:
            return ReinDecision(intercepted=True, intercept_reason="侦察白名单外内容")
        return ReinDecision()


class _RecordingSaddle:
    """记录每次执行的 ctx 快照；可控结果。"""

    def __init__(self, fail_names=()):
        self.executed = []                # (tool, ssh_session_id, environment, visible_terminal)
        self.fail_names = set(fail_names)
        self._schemas = [
            {"type": "function", "function": {"name": n}}
            for n in ("sys_overview", "log_read", "file_read", "shell_exec")
        ]

    def schemas(self):
        return list(self._schemas)

    async def execute(self, tc, ctx, auto_rollback=True):
        self.executed.append((
            tc.name,
            ctx.ssh_session_id,
            ctx.environment,
            getattr(ctx, "visible_terminal", None),
        ))
        if tc.name in self.fail_names:
            return ToolResult(success=False, error="模拟失败", output="stderr 片段"), None
        return ToolResult(success=True, output=f"out-{tc.name}"), None


class _TrackingCostTracker:
    def __init__(self):
        self.calls = []

    def record_llm_call(self, p, c):
        self.calls.append((p, c))


def _governance(reins, saddle):
    return SimpleNamespace(reins=reins, saddle=saddle)


def _tool_ctx(gov=None, cost_tracker=None):
    return ToolContext(
        ssh_session_id="ssh-parent",
        conversation_id="conv-1",
        agent_run_id="parent-run",
        cost_tracker=cost_tracker,
        visible_terminal=True,          # 父 run 开着可见终端 —— 子 Agent 必须强制关掉
        environment="prod",             # 继承父环境（侦察同机 + prod 审批强化口径）
        governance=gov,
    )


@pytest.fixture(autouse=True)
def _fake_model_config(monkeypatch):
    """子 Agent 从 DB 读模型配置 —— 测试里替换为固定值，不碰真实 DB。"""
    import database as db
    monkeypatch.setattr(db, "get_default_model", lambda mode: {"id": "m1"})
    monkeypatch.setattr(
        db, "get_model_config",
        lambda cid, mask_key=True: {
            "id": cid, "base_url": "http://llm.test/v1",
            "api_key": "k", "model_id": "test-model",
        })


def _run(tool, ctx, task="查一下负载和可疑进程",
         system_prompt="你是运维专家", extra=""):
    return asyncio.run(tool.execute(ctx, task, system_prompt, extra))


# ─────────────────────────────────────────────────────────────────────────────
# 1. 治理通道缺失 → 拒绝启动
# ─────────────────────────────────────────────────────────────────────────────

def test_治理通道缺失拒绝启动():
    tool = SpawnSubagentTool()
    # governance=None（脱离 harness 的老调用方）
    res = _run(tool, _tool_ctx(gov=None))
    assert res.success is False
    assert "治理通道未注入" in res.error
    # governance 缺 saddle（半装配）
    res2 = _run(tool, _tool_ctx(gov=SimpleNamespace(reins=object(), saddle=None)))
    assert res2.success is False
    assert "治理通道未注入" in res2.error


# ─────────────────────────────────────────────────────────────────────────────
# 2. 工具面双重封死
# ─────────────────────────────────────────────────────────────────────────────

def test_LLM只拿到白名单内schema():
    saddle = _RecordingSaddle()   # schemas 含 shell_exec（白名单外）
    reins = _RecordingReins()
    llm = _ScriptedLLM([LLMOutput(content="结论：一切正常")])
    tool = SpawnSubagentTool()
    monkey_llm = llm

    # 用 monkeypatch 把 StreamLLMClient 换成脚本替身
    import agent.harness.llm_client as llm_mod
    orig = llm_mod.StreamLLMClient

    class _FakeClient:
        def __init__(self, **kw):
            pass
        async def call(self, **kw):
            return await monkey_llm.call(
                messages=kw["messages"], tools=kw["tools"], model=kw["model"],
                temperature=kw["temperature"], max_tokens=kw["max_tokens"],
                enable_thinking=kw["enable_thinking"])

    # 模块内是函数内延迟导入 —— 直接替换 llm_client 模块属性
    llm_mod.StreamLLMClient = _FakeClient
    try:
        res = _run(tool, _tool_ctx(_governance(reins, saddle)))
    finally:
        llm_mod.StreamLLMClient = orig

    assert res.success is True
    assert llm.calls, "子 LLM 应至少被调用一次"
    tools_seen = llm.calls[0][0]
    assert "shell_exec" not in tools_seen, "白名单外工具不得出现在子 Agent 的 schema 面"
    assert set(tools_seen) <= {"sys_overview", "log_read", "file_read"}


def test_白名单外调用即使拦截器放行也被本地拒绝():
    reins = _RecordingReins()     # 不拦截任何工具（模拟拦截器链被裁剪）
    saddle = _RecordingSaddle()
    llm = _ScriptedLLM([
        LLMOutput(tool_calls=[ToolCall(id="t1", name="shell_exec", args={"command": "id"})]),
        LLMOutput(content="结论：白名单外调用已被拒绝"),
    ])
    tool = SpawnSubagentTool()

    import agent.harness.llm_client as llm_mod
    orig = llm_mod.StreamLLMClient

    class _FakeClient:
        def __init__(self, **kw):
            pass
        async def call(self, **kw):
            return await llm.call(
                messages=kw["messages"], tools=kw["tools"], model=kw["model"],
                temperature=kw["temperature"], max_tokens=kw["max_tokens"],
                enable_thinking=kw["enable_thinking"])

    llm_mod.StreamLLMClient = _FakeClient
    try:
        res = _run(tool, _tool_ctx(_governance(reins, saddle)))
    finally:
        llm_mod.StreamLLMClient = orig

    assert res.success is True
    assert saddle.executed == [], "白名单外工具绝不能到达 Saddle 执行层"
    # 拒绝原因要回给子模型（tool 消息）；PRE 检查仍先被触发（治理链记账一致）
    assert any(p == "pre_tool_call" and t == "shell_exec" for p, t, _ in reins.seen)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 侦察循环收敛 + 子上下文继承/隔离
# ─────────────────────────────────────────────────────────────────────────────

def test_侦察循环取结论且子上下文正确():
    reins = _RecordingReins()
    saddle = _RecordingSaddle()
    llm = _ScriptedLLM([
        LLMOutput(tool_calls=[
            ToolCall(id="t1", name="sys_overview", args={}),
            ToolCall(id="t2", name="log_read", args={"path": "/var/log/syslog"}),
        ], prompt_tokens=100, completion_tokens=50),
        LLMOutput(content="结论：负载正常；syslog 无异常条目。", prompt_tokens=200, completion_tokens=80),
    ])
    tool = SpawnSubagentTool()

    import agent.harness.llm_client as llm_mod
    orig = llm_mod.StreamLLMClient

    class _FakeClient:
        def __init__(self, **kw):
            pass
        async def call(self, **kw):
            return await llm.call(
                messages=kw["messages"], tools=kw["tools"], model=kw["model"],
                temperature=kw["temperature"], max_tokens=kw["max_tokens"],
                enable_thinking=kw["enable_thinking"])

    llm_mod.StreamLLMClient = _FakeClient
    try:
        res = _run(tool, _tool_ctx(_governance(reins, saddle)))
    finally:
        llm_mod.StreamLLMClient = orig

    assert res.success is True
    assert "负载正常" in res.output
    # 子 saddle 收到的 ctx：继承父 ssh 会话与环境、可见终端强制关闭
    assert all(e[1] == "ssh-parent" for e in saddle.executed)
    assert all(e[2] == "prod" for e in saddle.executed)
    assert all(e[3] is False for e in saddle.executed), \
        "子 Agent 必须强制 visible_terminal=False（父 run 开着也不继承）"
    # 每个工具调用都过了 PRE + POST（审计链）
    pre_tools = [t for p, t, _ in reins.seen if p == "pre_tool_call"]
    post_tools = [t for p, t, _ in reins.seen if p == "post_tool_call"]
    assert pre_tools == ["sys_overview", "log_read"]
    assert post_tools == ["sys_overview", "log_read"], "POST（审计链）必须逐调用触发"


# ─────────────────────────────────────────────────────────────────────────────
# 4. PRE 拦截 + 消息顺序
# ─────────────────────────────────────────────────────────────────────────────

def test_PRE拦截成员不执行且其余成员照常():
    reins = _RecordingReins(intercept_names={"log_read"})
    saddle = _RecordingSaddle()
    llm = _ScriptedLLM([
        LLMOutput(tool_calls=[
            ToolCall(id="t1", name="sys_overview", args={}),
            ToolCall(id="t2", name="log_read", args={"path": "/var/log/syslog"}),
        ]),
        LLMOutput(content="结论：概览正常，日志读取被拦截未能验证。"),
    ])
    tool = SpawnSubagentTool()

    import agent.harness.llm_client as llm_mod
    orig = llm_mod.StreamLLMClient

    class _FakeClient:
        def __init__(self, **kw):
            pass
        async def call(self, **kw):
            return await llm.call(
                messages=kw["messages"], tools=kw["tools"], model=kw["model"],
                temperature=kw["temperature"], max_tokens=kw["max_tokens"],
                enable_thinking=kw["enable_thinking"])

    llm_mod.StreamLLMClient = _FakeClient
    try:
        res = _run(tool, _tool_ctx(_governance(reins, saddle)))
    finally:
        llm_mod.StreamLLMClient = orig

    assert res.success is True
    executed = [e[0] for e in saddle.executed]
    assert executed == ["sys_overview"], f"被拦截成员不得执行: {executed}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. token 计入主 CostTracker
# ─────────────────────────────────────────────────────────────────────────────

def test_token计入主AgentCostTracker():
    reins = _RecordingReins()
    saddle = _RecordingSaddle()
    tracker = _TrackingCostTracker()
    llm = _ScriptedLLM([
        LLMOutput(content="结论：正常", prompt_tokens=111, completion_tokens=22),
    ])
    tool = SpawnSubagentTool()

    import agent.harness.llm_client as llm_mod
    orig = llm_mod.StreamLLMClient

    class _FakeClient:
        def __init__(self, **kw):
            pass
        async def call(self, **kw):
            return await llm.call(
                messages=kw["messages"], tools=kw["tools"], model=kw["model"],
                temperature=kw["temperature"], max_tokens=kw["max_tokens"],
                enable_thinking=kw["enable_thinking"])

    llm_mod.StreamLLMClient = _FakeClient
    try:
        res = _run(tool, _tool_ctx(_governance(reins, saddle), cost_tracker=tracker))
    finally:
        llm_mod.StreamLLMClient = orig

    assert res.success is True
    assert tracker.calls == [(111, 22)], f"token 应计入主 Tracker: {tracker.calls}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. 迭代上限 → 收尾总结；空结论按失败上抛
# ─────────────────────────────────────────────────────────────────────────────

def test_迭代上限走收尾总结():
    reins = _RecordingReins()
    saddle = _RecordingSaddle()
    # 前 6 轮全部要求观测（耗尽迭代），第 7 次（总结调用，tools=[]）给结论
    llm = _ScriptedLLM([
        LLMOutput(tool_calls=[ToolCall(id=f"t{i}", name="sys_overview", args={})])
        for i in range(6)
    ] + [LLMOutput(content="收尾结论：基于 6 次概览，负载平稳；进程细节未验证。")])
    tool = SpawnSubagentTool()

    import agent.harness.llm_client as llm_mod
    orig = llm_mod.StreamLLMClient

    class _FakeClient:
        def __init__(self, **kw):
            pass
        async def call(self, **kw):
            return await llm.call(
                messages=kw["messages"], tools=kw["tools"], model=kw["model"],
                temperature=kw["temperature"], max_tokens=kw["max_tokens"],
                enable_thinking=kw["enable_thinking"])

    llm_mod.StreamLLMClient = _FakeClient
    try:
        res = _run(tool, _tool_ctx(_governance(reins, saddle)))
    finally:
        llm_mod.StreamLLMClient = orig

    assert res.success is True
    assert "收尾结论" in res.output
    # 总结调用不带工具（第 7 次）
    assert llm.calls[6][0] == [], "收尾总结必须是纯推理调用"
    assert len(saddle.executed) == 6


def test_空结论按失败上抛():
    reins = _RecordingReins()
    saddle = _RecordingSaddle()
    llm = _ScriptedLLM([LLMOutput(content="   ")])   # 无工具、无正文
    tool = SpawnSubagentTool()

    import agent.harness.llm_client as llm_mod
    orig = llm_mod.StreamLLMClient

    class _FakeClient:
        def __init__(self, **kw):
            pass
        async def call(self, **kw):
            return await llm.call(
                messages=kw["messages"], tools=kw["tools"], model=kw["model"],
                temperature=kw["temperature"], max_tokens=kw["max_tokens"],
                enable_thinking=kw["enable_thinking"])

    llm_mod.StreamLLMClient = _FakeClient
    try:
        res = _run(tool, _tool_ctx(_governance(reins, saddle)))
    finally:
        llm_mod.StreamLLMClient = orig

    assert res.success is False
    assert "空内容" in res.error
