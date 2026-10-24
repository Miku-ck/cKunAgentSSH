"""S7-11 回归 — PATCH 不许绕开 POST 的入库上限，两条写入口同一组数字。

`ScheduleIn.name` 有 min/max，而 `SchedulePatch.name` 原是裸 `Optional[str]`；
`_drop_nulls` 只丢 `None` ⇒ `{"name": ""}` 与超长名都能从 PATCH 进库。同族漏洞在
`task_prompt` 上更明显：POST/PATCH/`schedule_op create` 三处都没上限，而这条工具
入口根本不经 pydantic —— 一条 500KB 的 task 会每次触发都整份进无头 run 的上下文。
"""
import asyncio

import pytest
from pydantic import ValidationError

import agent.tools.builtin.ops_schedule as osch
import routes.schedules as sch
from routes.agent import AgentRunRequest

_LONG = 101
_HUGE_TASK = 50_001


# ── 模型层：POST 与 PATCH 的约束必须逐字对齐 ────────────────────────────────

def _bounds(model, field):
    """pydantic 2 把长度约束放在 FieldInfo.metadata 的 MinLen/MaxLen 里，不是属性。"""
    lo = hi = None
    for m in model.model_fields[field].metadata:
        lo = getattr(m, "min_length", lo)
        hi = getattr(m, "max_length", hi)
    return lo, hi


def test_patch_name_carries_the_same_bounds_as_post():
    assert _bounds(sch.SchedulePatch, "name") == _bounds(sch.ScheduleIn, "name")


def test_patch_task_prompt_carries_the_same_bounds_as_post():
    assert _bounds(sch.SchedulePatch, "task_prompt") == _bounds(
        sch.ScheduleIn, "task_prompt")


@pytest.mark.parametrize("payload", [
    {"name": ""},
    {"name": "n" * _LONG},
    {"task_prompt": "p" * _HUGE_TASK},
])
def test_patch_rejects_what_post_rejects(payload):
    """原实现里这三条 PATCH 全部合法 —— 换个接口就把 POST 的闸门绕过了。"""
    with pytest.raises(ValidationError):
        sch.SchedulePatch(**payload)


def test_patch_still_accepts_a_plain_partial_edit():
    patch = sch.SchedulePatch(name="新名字", enabled=False)
    dumped = patch.model_dump(exclude_unset=True)
    assert dumped == {"name": "新名字", "enabled": False}


def test_task_prompt_bound_matches_the_run_task_it_feeds():
    """schedules.task_prompt 就是那次无头 run 的 AgentRunRequest.task，口径同源。"""
    assert _bounds(sch.ScheduleIn, "task_prompt")[1] == _bounds(
        AgentRunRequest, "task")[1]


# ── 工具入口：不经 pydantic，自己判同一组数字 ───────────────────────────────

def test_tool_side_numbers_match_the_rest_side():
    assert osch._NAME_MAX_LEN == _bounds(sch.ScheduleIn, "name")[1]
    assert osch._TASK_PROMPT_MAX_LEN == _bounds(sch.ScheduleIn, "task_prompt")[1]


def _create(**kw):
    args = {"mode": "interval", "interval_minutes": 60, "cron": None,
            "task": None, "command": None, "name": None}
    args.update(kw)
    return asyncio.run(osch.ScheduleOpTool()._create(**args))


def test_create_rejects_oversized_task_without_writing(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("超长 task 不该走到写库那一步")

    monkeypatch.setattr(osch, "_resolve_target_host", _boom)
    res = _create(task="T" * _HUGE_TASK)
    assert res.success is False
    assert "过长" in res.error


def test_create_rejects_oversized_name_without_writing(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("超长 name 不该走到写库那一步")

    monkeypatch.setattr(osch, "_resolve_target_host", _boom)
    res = _create(name="N" * _LONG)
    assert res.success is False
    assert "name 过长" in res.error


def test_command_concatenation_is_also_measured(monkeypatch):
    """task 短、command 长时拼起来照样超限 —— 闸门量的是最终入库的那串。"""
    def _boom(*_a, **_k):
        raise AssertionError("不该走到写库那一步")

    monkeypatch.setattr(osch, "_resolve_target_host", _boom)
    res = _create(task="巡检", command="c" * _HUGE_TASK)
    assert res.success is False
    assert "过长" in res.error
