"""数值型配置/字段的"假值吞默认值"回归测试（`x or default` 把 0 当成缺失）。

- schedule_engine：`model_cfg.get("temperature") or 0.7` —— 设置页允许
  temperature=0（ge=0，确定性输出），交互式运行按 0 走、定时任务按 0.7 走。

记忆侧同源缺陷随 v1 `memory._row_to_entry` 一并退场；v2 的 `retriever.search`
直接取 `r["importance"]`，0.0 语义由 test_memory_v2 覆盖。
"""
from agent.schedule_engine import _DEFAULT_TEMPERATURE, _num_param


# ── 模型 temperature ────────────────────────────────────────────────
def test_num_param_keeps_zero():
    assert _num_param(0.0, _DEFAULT_TEMPERATURE) == 0.0
    assert _num_param(0, _DEFAULT_TEMPERATURE) == 0.0


def test_num_param_falls_back_only_on_none():
    assert _num_param(None, _DEFAULT_TEMPERATURE) == _DEFAULT_TEMPERATURE
    assert _num_param(1.2, _DEFAULT_TEMPERATURE) == 1.2


def test_num_param_coerces_numeric_string():
    # 老库里数值列可能被写成 TEXT，读回字符串时不能崩
    assert _num_param("0.3", _DEFAULT_TEMPERATURE) == 0.3
