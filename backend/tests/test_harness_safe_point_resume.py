"""完整安全点恢复 Harness 上下文，不重置预算、消息或累计成本。"""
from __future__ import annotations

import time

import pytest

from agent.cost_tracker import CostTracker
from agent.harness import Harness, HarnessConfig
from agent.harness.contracts import AgentConfig, RunContext, State
from agent.harness.safe_point import SafePoint, snapshot_context
from agent.tools import ToolRegistry


class _Client:
    pass


class _Driver:
    def __init__(self):
        self.ctx = None

    async def drive(self, ctx, harness):
        self.ctx = ctx
        if False:
            yield None


@pytest.mark.asyncio
async def test_harness_restores_full_safe_point_without_resetting_budget(monkeypatch):
    source = RunContext(
        run_id="run-restore", task="long task", conversation_id="conv-restore",
        system_prompt="system", history=[{"role": "user", "content": "old"}],
        memories=["memory"], config=AgentConfig(
            max_iterations=9, max_cost_usd=1.25, max_total_tokens=12345,
            operation_ledger_enabled=True, safe_point_enabled=True,
        ),
        llm_model="model-a", ssh_session_id="session-a", environment="prod",
        skill_ids=["skill-a"], auto_skill_match=False,
    )
    source.messages = [
        {"role": "user", "content": "long task"},
        {"role": "assistant", "content": "partial"},
    ]
    source.iteration = 4
    source.state = State.OBSERVING
    source.state_history = [{"from": "thinking", "event": "x", "to": "observing"}]
    source.tool_calls_log = [{"tool": "file_read", "success": True}]
    source.warnings = ["warning"]
    source.errors = ["error"]
    source.metadata.update({
        "permission_mode": "plan", "target_host": "host-a",
        "environment_source": "connection", "cost_snapshot": {
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "cost_usd": 0.2, "tool_call_count": 2, "llm_call_count": 3,
            "duration_ms": 8000, "iterations": 4, "tool_duration_ms": 20,
        },
    })
    payload = snapshot_context(source, "after_tool")
    writer = SafePoint(version=7)
    monkeypatch.setattr(SafePoint, "load", lambda _: _async_value((payload, writer)))

    tracker = CostTracker(model_id="model-a")
    harness = Harness(HarnessConfig(model="model-a"))
    driver = _Driver()
    harness.driver = driver
    harness.assemble(_Client(), ToolRegistry(), tracker)

    async for _ in harness.run(
        task="ignored", conversation_id="ignored", run_id="run-restore",
        resume_safe_point=payload,
    ):
        pass

    ctx = driver.ctx
    assert ctx.messages[:-1] == source.messages
    assert ctx.messages[-1]["role"] == "user"
    assert "重新规划" in ctx.messages[-1]["content"]
    assert "不要重复" in ctx.messages[-1]["content"]
    assert ctx.iteration == 4
    assert ctx.state == State.OBSERVING
    assert ctx.state_history == source.state_history
    assert ctx.tool_calls_log == source.tool_calls_log
    assert ctx.config.max_iterations == 9
    assert ctx.config.max_cost_usd == 1.25
    assert ctx.config.max_total_tokens == 12345
    assert ctx.metadata["permission_mode"] == "plan"
    assert ctx.metadata["target_host"] == "host-a"
    assert ctx.metadata["safe_point"] is writer
    assert ctx.span_stack == []
    assert tracker.prompt_tokens == 100
    assert tracker.completion_tokens == 50
    assert tracker.tool_call_count == 2
    assert tracker.llm_call_count == 3


@pytest.mark.asyncio
async def test_harness_restores_wall_clock_origin_across_processes(monkeypatch):
    source = RunContext(
        run_id="run-wall", task="task", conversation_id="conv",
        system_prompt="", history=[], memories=[],
        config=AgentConfig(max_iterations=5, max_wall_clock_s=60),
    )
    source.iteration = 2
    payload = snapshot_context(source, "iteration")
    payload["run_started_unix_s"] = time.time() - 20
    monkeypatch.setattr(SafePoint, "load", lambda _: _async_value((payload, SafePoint(version=3))))
    harness = Harness(HarnessConfig(model="model-a"))
    driver = _Driver()
    harness.driver = driver
    harness.assemble(_Client(), ToolRegistry(), CostTracker(model_id="model-a"))
    async for _ in harness.run(task="task", conversation_id="conv", run_id="run-wall", resume_safe_point=payload):
        pass
    elapsed = time.monotonic() - driver.ctx.metadata["run_started_mono"]
    assert 19 <= elapsed <= 22


async def _async_value(value):
    return value
