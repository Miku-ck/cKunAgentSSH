"""Agent 运行前置解析回归：thinking 三态、输出预算的推理预留、占位自愈的终态集合。

三条都对应线上可见故障：

1. `enable_thinking` 原为 `bool = False`，「客户端没表态」与「用户显式关闭」无法区分
   —— 演化一旦学到「开启 thinking」，用户就再也关不掉（还连带多扣 8192 推理预算）。
2. 模型行的 `reasoning_tokens` 默认 8192，过去无条件传给 `safe_max_tokens` 扣除，
   而 thinking 关闭时 `stirrups.call_llm` 根本不使用该预算 —— 白扣把小上下文模型的
   输出削到 256 token，回答与工具调用 JSON 静默截断。
3. 同会话占位的陈旧判定只认 completed/failed，而 `database.update_agent_run`
   会写入四个终态 —— 以 human_help / max_reached 结束的泄漏占位永不自愈，
   该会话之后每次运行都报「已有运行中的 Agent 任务」。
"""
import asyncio

from routes.agent import (
    AgentRunRequest,
    _in_flight_runs,
    _resolve_evolution_config,
    _resolve_thinking,
)
from safe_tokens import safe_max_tokens


def _req(**kw):
    base = {"task": "把服务重启一下", "conversation_id": "c"}
    base.update(kw)
    return AgentRunRequest(**base)


def test_enable_thinking_field_is_tristate():
    """默认必须是 None（未表态），显式 False 不得被归一掉。"""
    assert _req().enable_thinking is None
    assert _req(enable_thinking=False).enable_thinking is False


def test_explicit_off_wins_over_evolved_on():
    evolved = {"enable_thinking": True}
    assert _resolve_thinking(_req(enable_thinking=False), evolved) is False, \
        "用户显式关闭时演化值不得覆盖"
    assert _resolve_thinking(_req(enable_thinking=True), {"enable_thinking": False}) is True
    assert _resolve_thinking(_req(), evolved) is True, "未表态时演化生效"
    assert _resolve_thinking(_req(), None) is False


def test_reasoning_budget_only_deducted_when_thinking_on():
    """锁定量级：thinking 关闭时输出预算不再被推理预留吃掉。

    上下文 8192、prompt 极短：不扣推理 → 4096（上下文一半的上限）；
    扣 8192 推理 → 落到 256 的地板值。旧调用点对每个 run 都取后者。
    """
    without = safe_max_tokens(8192, "hi", None, None)
    with_reasoning = safe_max_tokens(8192, "hi", None, 8192)
    assert without == 4096
    assert with_reasoning == 256
    # 调用点约定：reasoning 实参跟着 thinking 走
    assert (_resolve_thinking(_req(enable_thinking=False), {"enable_thinking": True}) is False)
    assert safe_max_tokens(8192, "hi", None,
                           8192 if _resolve_thinking(_req(), None) else None) == without


def test_evolution_config_falls_back_without_evolved_row():
    """演化配置缺失/加载失败（_load_evolved_config 返回 None）时回到用户与模型默认值。"""
    evo = _resolve_evolution_config(_req(), {"temperature": 0.3}, 4096, None, False)
    # 契约新增 top_p：模型行没这一列时按 0.9 反显（与 chat_stream / AgentConfig 同默认）
    assert evo == {
        "thinking": False, "max_iter": 100, "temperature": 0.3,
        "top_p": 0.9, "max_tokens": 4096,
    }
    evo2 = _resolve_evolution_config(
        _req(), {"temperature": 0.3, "top_p": 0.4}, 4096,
        {"max_iterations": 7, "temperature": 0.1, "max_tokens": 2048}, True,
    )
    # 演化不改 top_p（agent_strategies 没有这一项）：仍取模型行的 0.4
    assert evo2 == {
        "thinking": True, "max_iter": 7, "temperature": 0.1,
        "top_p": 0.4, "max_tokens": 2048,
    }


def test_stale_takeover_covers_every_terminal_status(monkeypatch):
    """陈旧占位自愈的终态集合与 update_agent_run 对齐（漏 human_help/max_reached = 会话锁死）。"""
    from routes import agent as ra

    terminal = ("completed", "failed", "human_help", "max_reached")

    async def main():
        for status in terminal:
            monkeypatch.setattr(
                ra.db, "get_agent_run",
                lambda rid, s=status: {"id": rid, "status": s},
            )
            _in_flight_runs["cx"] = "old"
            assert await ra._acquire_run_slot("cx", "new") is None, f"{status} 应视为陈旧"
            assert _in_flight_runs["cx"] == "new"
        # 仍在跑的占位必须保持冲突
        monkeypatch.setattr(
            ra.db, "get_agent_run", lambda rid: {"id": rid, "status": "running"}
        )
        _in_flight_runs["cx"] = "old"
        assert await ra._acquire_run_slot("cx", "new") == "old"

    try:
        asyncio.run(main())
    finally:
        _in_flight_runs.pop("cx", None)
