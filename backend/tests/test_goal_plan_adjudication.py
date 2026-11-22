"""goal 验收锚 — 自检提示与达成粗判的单测（换宿主：agent/workflow.py 已退役，
自检提示现由 agent/harness/goal_plan_fsm.resolve_self_check_from_metadata 产出）。

三组取值断言原样保留（措辞是跨端契约的一部分，前端/注入文案依赖）：
- 前缀 ``[目标验收]``；
- 「已**全部达成**」/「仍有未达成」两个分支措辞；
- 只 trim 首尾空白，不改写 goal 原文。
"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.harness.driver import _goal_acknowledged
from agent.harness.goal_plan_fsm import resolve_self_check_from_metadata


def _prompt(goal_text: str) -> str:
    """与 driver 收尾锚同形：metadata 只带 goal 原文，无 criteria。"""
    return resolve_self_check_from_metadata({"_goal_text": goal_text})


def test_self_check_empty():
    """无 goal 内容 → 返回空串，调用方据此跳过自检（保持原收尾行为）。"""
    assert _prompt("") == ""
    assert _prompt("   \n ") == ""


def test_self_check_basic():
    """有 goal → 返回含成功标准原文与已达成/未达成分支的提示。"""
    prompt = _prompt("将 CPU 使用率降至 30% 以下")
    assert "将 CPU 使用率降至 30% 以下" in prompt
    assert "已**全部达成**" in prompt
    assert "仍有未达成" in prompt
    assert prompt.startswith("[目标验收]")


def test_self_check_trims():
    """只 trim 首尾空白，不改写 goal 原文。"""
    prompt = _prompt("  修复登录失败   ")
    assert "修复登录失败" in prompt
    assert "修复登录失败 " not in prompt  # 原文内部空白保留（仅首尾去）


# ── driver._goal_acknowledged：done 事件目标达成粗判 ──

def test_goal_acknowledged_empty():
    assert _goal_acknowledged("") is None
    assert _goal_acknowledged(None) is None


def test_goal_acknowledged_achieved():
    assert _goal_acknowledged("各项均已校验通过，已全部达成目标。") is True
    assert _goal_acknowledged("修复完成，目标已实现。") is True
    assert _goal_acknowledged("三个成功标准均已满足。") is True


def test_goal_acknowledged_not_achieved():
    assert _goal_acknowledged("虽已尽力，但目标尚未达成。") is False
    assert _goal_acknowledged("仅部分达成，性能指标还差 10%。") is False
    assert _goal_acknowledged("未能达成所有成功标准。") is False


def test_goal_acknowledged_ambiguous():
    """含糊措辞（未命中关键词）→ None，不强判成败。"""
    assert _goal_acknowledged("今天完成了不少工作。") is None
    assert _goal_acknowledged("你好，请问需要什么帮助？") is None


def test_goal_acknowledged_conflict_none():
    """同时含达成与未达成语（如"计划未达成，但目标已实现"）→ 冲突，返回 None 避免误判。"""
    assert _goal_acknowledged("部分计划未达成，不过目标已实现。") is None
