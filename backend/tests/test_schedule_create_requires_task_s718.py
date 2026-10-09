# -*- coding: utf-8 -*-
"""S7-18 回归 — schedule_op create 不许把漏参换成内置默认巡检词。

空 task_prompt 入库后，无头那次由 schedule_engine 兜一句「对当前环境执行一次例行
检查」（历史行与 UI 留空时的既有语义，本测试还顺手钉住它，改那句要连带改这里）。
工具这侧原先照单全收：模型漏传 task/command 时排下的班，到点跑的是谁也没要求过的
检查 —— 用户要的和他拿到的不是同一件事，而且没有任何一处报错。
"""
import asyncio

import pytest

import agent.tools.builtin.ops_schedule as osch


class _NoWrite(Exception):
    """闸门之后第一步就是解析目标主机 ⇒ 抛这个即证明「没走到写库」"""


def _boom(*_a, **_k):
    raise _NoWrite


def _create(monkeypatch, **kw):
    monkeypatch.setattr(osch, "_resolve_target_host", _boom)
    args = {"mode": "interval", "interval_minutes": 60, "cron": None,
            "task": None, "command": None, "name": None}
    args.update(kw)
    try:
        return asyncio.run(osch.ScheduleOpTool()._create(**args))
    except _NoWrite:
        return None


@pytest.mark.parametrize("task", [None, "", "   ", "\n\t"])
def test_blank_task_without_command_is_rejected(monkeypatch, task):
    res = _create(monkeypatch, task=task)
    assert res is not None, "空 task 不该走到写库那一步"
    assert res.success is False
    assert "非空的 task" in res.error


def test_blank_task_with_whitespace_command_still_rejected(monkeypatch):
    """command 只有一串空格时也等于没给 —— 两侧都 strip 后才能判空。"""
    res = _create(monkeypatch, task="  ", command="   ")
    assert res is not None
    assert res.success is False
    assert "非空的 task" in res.error


def test_command_alone_passes_the_gate(monkeypatch):
    """只给 command 是合法形态（拼进 task_prompt 由未来那次执行），不该被拦。"""
    assert _create(monkeypatch, command="df -h") is None


def test_task_alone_passes_the_gate(monkeypatch):
    assert _create(monkeypatch, task="每天巡检磁盘水位") is None


def test_engine_default_prompt_is_still_the_fallback_for_existing_rows():
    """UI 留空与历史行的既有语义：兜底词还在，只是不再由工具入口静默代填。"""
    import inspect

    from agent import schedule_engine
    assert "对当前环境执行一次例行检查" in inspect.getsource(schedule_engine)
