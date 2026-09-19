# -*- coding: utf-8 -*-
"""澄清向导（Clarification Wizard）回归测试。

覆盖：
  - _shell_detect_vague：明确命令返回 None，模糊命令返回原因
  - _step_to_dict / _build_clarification_needed：事件结构正确
  - _format_answers：格式化用户答案文本
  - _get_global_clarification_store：返回模块级 dict 引用
  - ClarificationInterceptor.check 三链路：触发→拦截→放行

运行: python -m pytest backend/tests/test_clarification_wizard.py -v
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

import agent.harness.reins as reins_mod
from agent.harness.contracts import AgentConfig, RunContext, ReinPoint, TraceEvent
from agent.harness.reins import (
    ClarificationInterceptor,
    ClarificationOption,
    ClarificationStep,
    _CLARIFICATION_CONTEXT,
    _CLARIFICATION_DONE,
    _build_clarification_needed,
    _format_answers,
    _get_global_clarification_store,
    _shell_build_steps,
    _shell_detect_vague,
    _step_to_dict,
)


# ── helpers ──

def _make_ctx(tool: str = "shell_exec", args: dict | None = None) -> RunContext:
    """构造一个最小化的 RunContext 用于拦截器测试"""
    ctx = RunContext.new(task="test task", mode="ssh", config=AgentConfig())
    ctx.tool_calls_log = [{
        "tool": tool,
        "args": args or {"command": "rm -rf *"},
        "iteration": 0,
        "success": None,
    }]
    return ctx


# ── _shell_detect_vague ──

class TestShellDetectVague:
    """_shell_detect_vague 不应误报明确命令，应识别真正的破坏性命令"""

    def test_clean_file_returns_none(self):
        # 明确单文件删除 → 不触发
        assert _shell_detect_vague({"command": "rm file.txt"}) is None

    def test_wildcard_rm_triggers(self):
        reason = _shell_detect_vague({"command": "rm -rf node_modules/*"})
        assert reason is not None
        assert "rm" in reason

    def test_recursive_rm_triggers(self):
        reason = _shell_detect_vague({"command": "rm -rf /tmp/build"})
        assert reason is not None

    def test_find_xargs_triggers(self):
        reason = _shell_detect_vague({"command": "find . -name '*.log' | xargs rm"})
        assert reason is not None

    def test_ls_returns_none(self):
        # 非破坏性命令不触发
        assert _shell_detect_vague({"command": "ls -la"}) is None

    def test_none_args_returns_none(self):
        assert _shell_detect_vague(None) is None
        assert _shell_detect_vague({}) is None

    def test_chmod_wildcard_triggers(self):
        reason = _shell_detect_vague({"command": "chmod -R 777 *"})
        assert reason is not None


# ── _shell_build_steps ──

class TestShellBuildSteps:
    def test_builds_three_steps(self):
        steps = _shell_build_steps({"command": "rm -rf *"})
        assert len(steps) == 3
        for s in steps:
            assert isinstance(s, ClarificationStep)
            assert s.options  # 非空
            assert s.allow_custom is True

    def test_first_step_asks_scope(self):
        steps = _shell_build_steps({"command": "rm -rf /tmp/*"})
        assert "范围" in steps[0].question or "作用" in steps[0].question

    def test_steps_have_three_options_each(self):
        steps = _shell_build_steps({"command": "rm -rf *"})
        for s in steps:
            assert len(s.options) == 3
            ids = [o.id for o in s.options]
            assert ids == ["A", "B", "C"]


# ── _step_to_dict ──

class TestStepToDict:
    def test_converts_correctly(self):
        step = ClarificationStep(
            question="test question?",
            options=[ClarificationOption(id="A", label="opt A")],
            allow_custom=True,
        )
        d = _step_to_dict(step)
        assert d["question"] == "test question?"
        assert d["options"] == [{"id": "A", "label": "opt A"}]
        assert d["allow_custom"] is True


# ── _build_clarification_needed ──

class TestBuildClarificationNeeded:
    def test_builds_trace_event(self):
        steps = _shell_build_steps({"command": "rm -rf *"})
        ev = _build_clarification_needed(
            run_id="r1", tool="shell_exec", args={"command": "rm -rf *"},
            steps=steps, step=0, total=3, tool_call_id="tc1",
        )
        assert isinstance(ev, TraceEvent)
        assert ev.kind == "clarification_needed"
        assert ev.payload["tool"] == "shell_exec"
        assert ev.payload["total"] == 3
        assert len(ev.payload["steps"]) == 3


# ── _format_answers ──

class TestFormatAnswers:
    def test_empty_answers(self):
        result = _format_answers({})
        assert "跳过" in result

    def test_formatted_steps(self):
        answers = {
            "0": {"choice": "A"},
            "1": {"choice": "custom", "custom_text": "blah"},
        }
        result = _format_answers(answers)
        assert "Q1" in result
        assert "Q2" in result
        assert "blah" in result


# ── _get_global_clarification_store ──

class TestGlobalStore:
    def test_returns_dict_with_done_and_context(self):
        store = _get_global_clarification_store()
        assert "done" in store
        assert "context" in store
        assert isinstance(store["done"], dict)
        assert isinstance(store["context"], dict)

    def test_is_module_level_reference(self):
        """多次调用应拿到同一对象引用 — 确保 routes 写入后拦截器读到"""
        s1 = _get_global_clarification_store()
        s2 = _get_global_clarification_store()
        assert s1["done"] is s2["done"]
        assert s1["context"] is s2["context"]


# ── ClarificationInterceptor 三链路 ──

class TestClarificationInterceptor:
    @pytest.fixture(autouse=True)
    def _clear_store(self):
        """每个测试前清理 pending 存储，避免跨测试干扰"""
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()
        yield
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()

    @pytest.mark.asyncio
    async def test_vague_command_triggers_clarification(self):
        """链路 1：模糊命令 → 拦截 + clarification_needed 事件"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf /tmp/*"})

        decision = await interceptor.check(ctx)

        assert decision.intercepted is True
        assert decision.intercept_reason == "clarification_needed"
        assert len(decision.events) == 1
        ev = decision.events[0]
        assert ev.kind == "clarification_needed"

        # 验证 payload 含 reason + args（前端展示命令上下文）
        assert ev.payload.get("reason"), "reason 不能为空"
        assert "args" in ev.payload
        assert ev.payload["args"].get("command") == "rm -rf /tmp/*"

        # 验证 pending context 已存储（含 reason）
        assert ctx.run_id in _CLARIFICATION_CONTEXT
        assert _CLARIFICATION_CONTEXT[ctx.run_id].get("reason")

    @pytest.mark.asyncio
    async def test_clear_command_not_triggered(self):
        """明确命令 → 不拦截"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "ls -la"})

        decision = await interceptor.check(ctx)

        assert decision.intercepted is False
        assert not decision.events

    @pytest.mark.asyncio
    async def test_unregistered_tool_not_triggered(self):
        """不在注册表中的工具不触发"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(tool="file_read", args={"path": "/etc/passwd"})

        decision = await interceptor.check(ctx)

        assert decision.intercepted is False

    @pytest.mark.asyncio
    async def test_pending_re_emits_same_step_and_intercepts(self):
        """链路 2：pending 未完成时重发当前步 + 拦截"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf *"})

        # 第一次触发
        d1 = await interceptor.check(ctx)
        assert d1.intercepted

        # 第二次检查（模拟 LLM 重试同一工具）
        # 给 ctx 再加一条相同工具调用（模拟 LLM 重试）
        ctx.tool_calls_log.append({
            "tool": "shell_exec",
            "args": {"command": "rm -rf *"},
            "iteration": 1,
            "success": None,
        })
        d2 = await interceptor.check(ctx)

        assert d2.intercepted is True
        assert d2.events[0].kind == "clarification_needed"
        assert any("等待用户回答" in (m.get("content", "") or "") for m in d2.inject_messages)

    @pytest.mark.asyncio
    async def test_done_answers_inject_context_and_release(self):
        """链路 3：答案就绪后 → 注入上下文 + clarification_done + 放行"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf *"})

        # 第 1 步：拦截触发
        d1 = await interceptor.check(ctx)
        assert d1.intercepted

        # 模拟前端提交答案 → routes 写入 _CLARIFICATION_DONE
        _CLARIFICATION_DONE[ctx.run_id] = {"0": {"choice": "A"}, "1": {"choice": "B"}}
        ctx_info = _CLARIFICATION_CONTEXT.get(ctx.run_id)
        if ctx_info:
            ctx_info["_done"] = True

        # 第 2 步：框架再次调用 check
        decision = await interceptor.check(ctx)

        assert decision.intercepted is False
        assert len(decision.events) == 1
        assert decision.events[0].kind == "clarification_done"

        # inject_messages 包含用户答案摘要
        assert any("[用户澄清结果]" in (m.get("content", "") or "") for m in decision.inject_messages)
