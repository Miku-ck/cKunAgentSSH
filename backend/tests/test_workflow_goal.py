"""goal 验收锚 — 自检提示 helper 的单测（backend/agent/workflow.py）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.harness.driver import _goal_acknowledged
from agent.workflow import goal_self_check_prompt


def test_goal_self_check_prompt_empty():
    """无 goal 内容 → 返回空串，调用方据此跳过自检（保持原收尾行为）。"""
    assert goal_self_check_prompt("") == ""
    assert goal_self_check_prompt("   \n ") == ""


def test_goal_self_check_prompt_basic():
    """有 goal → 返回含成功标准原文与已达成/未达成分支的提示。"""
    prompt = goal_self_check_prompt("将 CPU 使用率降至 30% 以下")
    assert "将 CPU 使用率降至 30% 以下" in prompt
    assert "已**全部达成**" in prompt
    assert "仍有未达成" in prompt
    assert prompt.startswith("[目标验收]")


def test_goal_self_check_prompt_trims():
    """只 trim 首尾空白，不改写 goal 原文。"""
    prompt = goal_self_check_prompt("  修复登录失败   ")
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