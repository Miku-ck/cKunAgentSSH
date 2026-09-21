"""演化链路的三处读数错误（round 13）。

1. evolution.py 把反思记录里 0~1 的**成本评分**当成美元读：
   `cfg_data.get("cost_usd") or cfg_data.get("cost", 0.0)` —— 零成本运行（本地/免费
   模型）的 cost_usd 恰为 0.0，于是回落到 _score_cost 给的 1.0，被读成"单次花了 $1"，
   恰好让最省钱的运行最频繁触发"成本高"规则，把迭代上限和温度一路削到下限。
2. routes/agent.py 无条件用演化值接管 max_iterations/temperature/max_tokens：
   写侧的 Field(ge=1, le=500) 管不住 agent_strategies 里的历史行，读侧不复核；
   并且用户关掉"动态步数预算"时演化仍接管 —— 设置开关是死的。
3. spawn_subagent 的失败以字符串 return，被 execute 包成 ToolResult(True)，
   主 Agent 把"[子 Agent LLM 调用超时]"当成子 Agent 的分析结果读进报告。
4. pending_delivery.clear_run 只 pop 不 set 事件，挂在 wait() 上的 Driver 只能等
   自己的 300s 超时才退出。
"""
import asyncio

import pytest

from agent.evolution import EvolutionEngine
from agent.pending_delivery import PendingDeliveries
from routes.agent import AgentRunRequest, _resolve_evolution_config


def _reflection(cost_usd, cost_score, completion=1.0, efficiency=0.5, robustness=0.8):
    return {
        "score": 0.8,
        "config": {
            "efficiency": efficiency,
            "robustness": robustness,
            "completion": completion,
            "cost": cost_score,
            "cost_usd": cost_usd,
            "iterations": 10,
        },
    }


def test_free_runs_are_not_billed_at_their_cost_score():
    cfg = EvolutionEngine().evolve(
        mode="ssh",
        recent_reflections=[_reflection(0.0, 1.0) for _ in range(5)],
    )
    assert cfg.avg_cost_usd == 0.0
    assert not [a for a in cfg.adjustments if "成本高" in a]


def test_legacy_reflection_without_cost_usd_counts_as_no_cost():
    row = _reflection(0.3, 0.6)
    del row["config"]["cost_usd"]
    cfg = EvolutionEngine().evolve(mode="ssh", recent_reflections=[row])
    assert cfg.avg_cost_usd == 0.0


def test_real_cost_still_triggers_the_cost_rule():
    cfg = EvolutionEngine().evolve(
        mode="ssh",
        recent_reflections=[_reflection(2.0, 0.2) for _ in range(5)],
    )
    assert cfg.avg_cost_usd > 0.5
    assert [a for a in cfg.adjustments if "成本高" in a]


def _req(**kw):
    base = dict(task="t", conversation_id="c")
    base.update(kw)
    return AgentRunRequest(**base)


_MODEL_CFG = {"temperature": 0.55}


def test_out_of_range_evolved_values_are_rejected_not_clamped():
    # 演化自己写 5-300；区间外说明数据坏了 → 保留用户/模型值，而不是夹成 1 轮
    out = _resolve_evolution_config(
        _req(), _MODEL_CFG, 4096,
        {"max_iterations": 0, "temperature": 9.9, "max_tokens": 9_999_999},
        thinking=False,
    )
    assert out["max_iter"] == 100
    assert out["temperature"] == 0.55
    assert out["max_tokens"] == 4096


def test_dynamic_budget_switch_governs_iteration_takeover():
    evolved = {"max_iterations": 20}
    on = _resolve_evolution_config(_req(), _MODEL_CFG, 4096, evolved, thinking=False)
    assert on["max_iter"] == 20
    off = _resolve_evolution_config(
        _req(enable_dynamic_budget=False), _MODEL_CFG, 4096, evolved, thinking=False
    )
    assert off["max_iter"] == 100


def test_evolved_temperature_still_applies_when_budget_off():
    off = _resolve_evolution_config(
        _req(enable_dynamic_budget=False), _MODEL_CFG, 4096,
        {"temperature": 0.3}, thinking=False,
    )
    assert off["temperature"] == 0.3


def test_subagent_model_lookup_failure_raises_instead_of_success_text(monkeypatch):
    from agent.tools.builtin.spawn_subagent import SpawnSubagentTool

    tool = SpawnSubagentTool()
    monkeypatch.setattr(tool, "_get_model_config", lambda ctx: None)
    with pytest.raises(RuntimeError):
        asyncio.run(
            tool._run_subagent(
                task="t", system_prompt="p", extra_context="", tool_ctx=object()
            )
        )


def test_clear_run_wakes_a_parked_waiter():
    d = PendingDeliveries.create(
        command="ls", session_id="s-1", requires_confirm=False, run_id="run-clear-13"
    )
    try:
        async def scenario():
            waiter = asyncio.create_task(PendingDeliveries.wait(d.tool_id))
            await asyncio.sleep(0)
            PendingDeliveries.clear_run("run-clear-13")
            # 原实现不 set 事件：这里会一直挂到 wait_for 超时
            await asyncio.wait_for(waiter, timeout=1)
        asyncio.run(scenario())
        assert d.event.is_set()
        assert PendingDeliveries.get(d.tool_id) is None
    finally:
        PendingDeliveries.remove(d.tool_id)
