"""澄清向导的跨层契约测试：reins 生产者 → SSE 事件 → 前端可读字段。

历史缺陷（本轮修复，防回归）：生产者 payload 用 step/total/steps 三个键，
SSEAdapter 却按 current_step/total_steps/question/options 取值 —— 全部落到默认值，
前端拿到空题目 + 空选项，整个澄清向导只剩一个自定义输入框（2-3 步的真实题元
从来没显示过）。单侧测试各自"通过"，因为没人跨过这条边界，故本文件两端都用真件。
"""
from agent.harness.contracts import TraceEvent
from agent.harness.reins.clarification import (
    _build_clarification_needed,
    _CLARIFIABLE_TOOLS,
    ClarificationInterceptor,
)
from agent.harness.telemetry import SSEAdapter

RUN = "run-clarify-1"


def _shell_event():
    """用真实注册表构造 rm -rf * 的澄清事件（多步）。"""
    detect_vague, build_steps = _CLARIFIABLE_TOOLS["shell_exec"]
    args = {"command": "rm -rf /var/log/*"}
    reason = detect_vague(args)
    assert reason, "该命令应被判为意图不明"
    steps = build_steps(args)
    assert len(steps) >= 2, "shell_exec 澄清应是多步问卷"
    return _build_clarification_needed(
        RUN, "shell_exec", args, steps, 0, len(steps), "tc-1", reason=reason
    ), steps


def test_clarification_event_reaches_frontend_with_question_and_options():
    ev, steps = _shell_event()
    sse = SSEAdapter.to_sse_dict(ev)
    assert sse["type"] == "clarification_needed"
    assert sse["run_id"] == RUN
    assert sse["reason"]
    # 前端读的就是这几个字段：任一为空都等于向导不可用
    assert sse["question"] == steps[0].question
    assert [o["id"] for o in sse["options"]] == ["A", "B", "C"]
    assert sse["current_step"] == 0
    assert sse["total_steps"] == len(steps)


def test_clarification_steps_are_forwarded_for_local_advance():
    """全量 steps 必须透传：前端本地推进 localStep 时按索引取下一题。"""
    ev, steps = _shell_event()
    sse = SSEAdapter.to_sse_dict(ev)
    forwarded = sse["steps"]
    assert len(forwarded) == len(steps)
    for i, s in enumerate(steps):
        assert forwarded[i]["question"] == s.question
        assert forwarded[i]["allow_custom"] == s.allow_custom
        assert [o["id"] for o in forwarded[i]["options"]] == [o.id for o in s.options]
    # 后续步与首步不同 —— 否则"逐步推进"根本没意义（旧 bug 会把首步重复 N 遍）
    assert forwarded[1]["question"] != forwarded[0]["question"]


def test_reshow_of_pending_step_keeps_index():
    """LLM 重试同一工具时后端重发当前步事件：步号/题目都要跟着走。"""
    ev, steps = _shell_event()
    ev2 = _build_clarification_needed(
        RUN, "shell_exec", {"command": "rm -rf /var/log/*"}, steps, 1, len(steps), "tc-1"
    )
    sse = SSEAdapter.to_sse_dict(ev2)
    assert sse["current_step"] == 1
    assert sse["question"] == steps[1].question
    assert sse["total_steps"] == len(steps)
    assert isinstance(ev, TraceEvent)


def test_interceptor_is_registered_before_permission_gate():
    """澄清(7) 必须早于授权(8)：先问清意图，再决定要不要弹授权卡。"""
    assert ClarificationInterceptor.point.value == "pre_tool_call"
    assert ClarificationInterceptor.priority < 8
