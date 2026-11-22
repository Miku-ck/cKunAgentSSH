"""goal_plan_progress 的 SSE 投影 — 与 test_trace_projection_keys.py 同风格。

钉三件事：
1. `_KIND_MAP` 收录 `goal_plan_progress`（清单必须是全集，跨层守卫才有意义）；
2. `to_sse_dict` 键严格取生产者实写键（mode/goal/plan/step/progress），
   刻意不带完整 steps —— `steps` 键已被澄清向导占用，snapshot 事件是
   「前端拉 REST 全量」的信号；
3. done 事件的 `workflow_goal` 键随 done 投影上行（生产者写了、投影必须搬）。
"""
from agent.harness.contracts import TraceEvent
from agent.harness.telemetry import SSEAdapter


def _ev(kind: str, payload: dict, run_id: str = "r1") -> TraceEvent:
    return TraceEvent(kind=kind, source="test", run_id=run_id, payload=payload)


def test_kind_map_contains_goal_plan_progress():
    from agent.harness.telemetry import SSEAdapter as _S
    assert _S._KIND_MAP.get("goal_plan_progress") == "goal_plan_progress"


def test_delta_projection_only_emits_present_keys():
    """delta 模式：只带 step/progress —— goal/plan 缺席，前端字段级归并。"""
    out = SSEAdapter.to_sse_dict(_ev("goal_plan_progress", {
        "mode": "delta",
        "step": {"step_id": "s1", "status": "completed", "evidence": "ok"},
        "progress": {"total": 3, "completed": 1},
    }))
    assert out["type"] == "goal_plan_progress"
    assert out["run_id"] == "r1"
    assert out["mode"] == "delta"
    assert out["step"] == {"step_id": "s1", "status": "completed", "evidence": "ok"}
    assert out["progress"] == {"total": 3, "completed": 1}
    # 缺省键保持缺席（前端 applyDelta 按 in 判定，不该被 None 污染）
    assert "goal" not in out and "plan" not in out
    # 契约红线：这条线上没有完整 steps —— 键已被澄清向导占用
    assert "steps" not in out


def test_snapshot_projection_carries_goal_and_plan():
    out = SSEAdapter.to_sse_dict(_ev("goal_plan_progress", {
        "mode": "snapshot",
        "goal": {"goal_id": "g1", "statement": "服务可用"},
        "plan": {"plan_id": "p1", "version": 1, "status": "awaiting_approval"},
        "progress": {"total": 2, "completed": 0},
    }))
    assert out["mode"] == "snapshot"
    assert out["goal"] == {"goal_id": "g1", "statement": "服务可用"}
    assert out["plan"]["status"] == "awaiting_approval"
    assert "step" not in out  # snapshot 不带单步


def test_done_event_projects_workflow_goal():
    """done 投影搬运 workflow_goal（结构化演进后的键形状）。"""
    wf = {"goal_id": "g1", "version": 2, "criteria_met": {"met": 1, "total": 2},
          "self_checked": True, "acknowledged": True}
    out = SSEAdapter.to_sse_dict(_ev("done", {"workflow_goal": wf, "content": "done"}))
    assert out["type"] == "done"
    assert out["workflow_goal"] == wf
