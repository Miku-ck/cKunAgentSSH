# -*- coding: utf-8 -*-
"""反思 safety 三态评分回归：被正确拦截不应反向惩罚安全合规行为。

修复前：只要 warnings 含"拦截/权限"就记 0.3，"危险命令被拦 → 代理换安全
路径完成任务"这一理想行为与"反复撞墙"同分。
修复后：无拦截 1.0 / 拦截后恢复 0.9 / 反复拦截后恢复 0.7 / 拦截后未恢复 0.3。

运行: python -m pytest backend/tests/test_reflection_safety_scoring.py -v
"""
from agent.reflection import MetaReflection, RunTrace


def _trace(tool_calls, final_state="completed", warnings=None):
    return RunTrace(
        run_id="r1", task="清理日志", mode="ssh",
        final_state=final_state, iterations=len(tool_calls) + 1,
        tool_calls=tool_calls, warnings=warnings or [],
    )


BLOCKED = {"tool": "shell_exec", "args": {"command": "rm -rf /"},
           "success": False, "result": "[系统拦截] 危险命令被阻断"}
SAFE_OK = {"tool": "shell_exec", "args": {"command": "du -sh /var/log"},
           "success": True, "result": "1.2G /var/log"}


def test_no_block_full_score():
    assert MetaReflection()._score_safety(_trace([SAFE_OK])) == 1.0


def test_block_then_recover_not_penalized():
    """被正确拦截后换安全路径 → 期望行为，得分应接近满分"""
    score = MetaReflection()._score_safety(_trace([BLOCKED, SAFE_OK]))
    assert score == 0.9


def test_block_recovered_by_completion_even_without_later_success():
    """末次拦截后无工具调用，但 run 正常完成 → 视为已适应"""
    score = MetaReflection()._score_safety(
        _trace([BLOCKED], final_state="completed"))
    assert score == 0.9


def test_repeated_block_then_recover_mid_score():
    score = MetaReflection()._score_safety(
        _trace([BLOCKED, BLOCKED, SAFE_OK]))
    assert score == 0.7


def test_block_never_recovered_low_score():
    score = MetaReflection()._score_safety(
        _trace([BLOCKED], final_state="failed"))
    assert score == 0.3


def test_warnings_only_external_source():
    """无 tool_calls 细节、仅外部注入 warnings 时按终态分流"""
    t_done = _trace([], final_state="completed",
                    warnings=["工具 shell_exec 被拦截"])
    t_failed = _trace([], final_state="failed",
                      warnings=["工具 shell_exec 被拦截"])
    assert MetaReflection()._score_safety(t_done) == 0.9
    assert MetaReflection()._score_safety(t_failed) == 0.3


def test_reflect_end_to_end_recovery_higher_than_stuck():
    r = MetaReflection()
    recovered = r.reflect(_trace([BLOCKED, SAFE_OK]))
    stuck = r.reflect(_trace([BLOCKED], final_state="failed"))
    assert recovered.safety > stuck.safety
    assert any("符合预期" in i for i in recovered.insights)
    assert any("未见恢复" in i for i in stuck.insights)
