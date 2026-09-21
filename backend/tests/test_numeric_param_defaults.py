"""数值型配置/字段的"假值吞默认值"回归测试。

同一根因（`x or default` 把 0 当成缺失）的两处历史缺陷，本轮修复：
- schedule_engine：`model_cfg.get("temperature") or 0.7` —— 设置页允许
  temperature=0（ge=0，确定性输出），交互式运行按 0 走、定时任务按 0.7 走。
- memory._row_to_entry：`float(row.get("importance", 0.5) or 0.5)` —— importance
  0.0 是合法的"低重要性"取值，被抬回 0.5 后记忆重要性读数失真。
"""
from agent.memory import _row_to_entry
from agent.schedule_engine import _DEFAULT_TEMPERATURE, _num_param


def _row(**over):
    row = {
        "id": "m1", "type": "semantic", "mode": "ssh", "content": "c",
        "source_conv_id": "", "agent_run_id": "", "embedding": None,
        "importance": 0.5, "last_used_at": "2026-09-20 00:00:00",
        "use_count": 0, "created_at": "2026-09-20 00:00:00",
    }
    row.update(over)
    return row


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


# ── 记忆 importance ────────────────────────────────────────────────
def test_row_to_entry_keeps_zero_importance():
    assert _row_to_entry(_row(importance=0.0)).importance == 0.0


def test_row_to_entry_null_importance_defaults():
    assert _row_to_entry(_row(importance=None)).importance == 0.5


def test_row_to_entry_missing_importance_column():
    # 老库无 importance 列：dict 里根本没这个键，也不能 KeyError 或抬成非默认值
    row = _row()
    del row["importance"]
    assert _row_to_entry(row).importance == 0.5


def test_row_to_entry_keeps_real_importance():
    assert _row_to_entry(_row(importance=0.9)).importance == 0.9
