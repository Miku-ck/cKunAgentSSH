"""goal_plan 状态机 — 守卫「非法跃迁进不来、验收措辞不漂移」。

与前端 `src/utils/goal-plan-core.test.ts` 成对手工同源：一侧放宽另一侧
不放宽即为跨端漂移。goal 达成只由用户裁决产生，模型侧不存在入口。
"""
from agent.harness.goal_plan_fsm import (
    LEGAL_STEP_TRANSITIONS,
    STEP_STATUSES,
    all_criteria_met,
    build_goal_self_check_prompt,
    can_transition,
    compute_progress,
    merge_criteria,
)


def test_legal_transitions_pass():
    assert can_transition("pending", "in_progress")[0]
    assert can_transition("pending", "skipped")[0]
    assert can_transition("in_progress", "completed", evidence="回显 done")[0]
    assert can_transition("in_progress", "blocked", blocked_reason="权限被拒")[0]
    assert can_transition("blocked", "in_progress")[0]


def test_illegal_transitions_rejected():
    assert not can_transition("completed", "in_progress")[0]
    assert not can_transition("skipped", "pending")[0]
    assert not can_transition("pending", "completed", evidence="x")[0]
    assert not can_transition("blocked", "skipped")[0]
    # 未知状态 fail-closed
    assert not can_transition("nope", "pending")[0]
    assert not can_transition("pending", "nope")[0]


def test_completed_requires_evidence_and_blocked_requires_reason():
    ok, reason = can_transition("in_progress", "completed")
    assert not ok and "evidence" in reason
    ok, reason = can_transition("in_progress", "completed", evidence="   ")
    assert not ok
    ok, reason = can_transition("in_progress", "blocked")
    assert not ok and "blocked_reason" in reason


def test_matrix_is_total_over_statuses():
    """矩阵覆盖全部已知状态（漏键会导致 can_transition 恒拒）。"""
    assert set(LEGAL_STEP_TRANSITIONS) == set(STEP_STATUSES)


def test_compute_progress_counts_and_ignores_unknown():
    steps = [
        {"status": "completed"},
        {"status": "in_progress"},
        {"status": "blocked"},
        {"status": "pending"},
        {"status": "weird"},
    ]
    assert compute_progress(steps) == {
        "total": 5, "pending": 1, "in_progress": 1,
        "completed": 1, "blocked": 1, "skipped": 0,
    }
    assert compute_progress([]) == {
        "total": 0, "pending": 0, "in_progress": 0,
        "completed": 0, "blocked": 0, "skipped": 0,
    }


def test_merge_criteria_and_all_met():
    criteria = [
        {"id": "c1", "text": "服务重启", "met": False, "evidence": ""},
        {"id": "c2", "text": "日志无报错", "met": False, "evidence": ""},
    ]
    merged = merge_criteria(criteria, {"c1": {"met": True, "evidence": "systemctl 输出"}})
    assert merged[0]["met"] is True and merged[0]["evidence"] == "systemctl 输出"
    assert merged[1]["met"] is False
    # 入参不被修改
    assert criteria[0]["met"] is False
    assert not all_criteria_met(merged)
    assert all_criteria_met(merge_criteria(merged, {"c2": {"met": True}}))
    # 空 criteria 不算达成
    assert not all_criteria_met([])


def test_self_check_prompt_wording_is_pinned():
    """前缀与两处结论措辞是被守卫钉住的取值（M3 迁移时保持不变）。"""
    prompt = build_goal_self_check_prompt("服务恢复", [
        {"id": "c1", "text": "进程存活", "met": True, "evidence": ""},
    ])
    assert prompt.startswith("[目标验收]")
    assert "已**全部达成**" in prompt
    assert "仍有未达成" in prompt
    assert "进程存活" in prompt


def test_self_check_prompt_empty_statement_returns_empty():
    assert build_goal_self_check_prompt("", []) == ""
    assert build_goal_self_check_prompt("   ", [{"id": "c", "text": "x", "met": False}]) == ""
