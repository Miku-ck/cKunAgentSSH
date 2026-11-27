"""Goal 跨会话推进：持久化领取、无进展熔断与用户裁决边界。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import database as db
from db import goal_continuation, goal_plan_store


@pytest.fixture
def goal_db(tmp_path):
    db.init_db(str(tmp_path / "goal-continuation.db"))
    goal_plan_store._reset_memo_for_tests()
    goal_continuation._reset_memo_for_tests()
    yield
    goal_continuation._reset_memo_for_tests()
    goal_plan_store._reset_memo_for_tests()


def _goal(*, approved=True, active=True):
    cid = db.create_conversation("Goal")["id"]
    goal = goal_plan_store.create_goal(cid, "目标", "持续推进直到用户裁决")
    assert goal is not None
    goal_plan_store.revise_goal(goal["goal_id"], 1, {"status": "active" if active else "draft"})
    plan = goal_plan_store.create_plan_with_steps(cid, goal["goal_id"], [
        {"title": "第一步", "detail": "执行并附证据"},
    ])
    assert plan is not None
    if approved:
        goal_plan_store.approve_plan(plan["plan_id"])
    return goal_plan_store.get_goal_by_conversation(cid), plan, cid


def test_due_requires_active_goal_approved_plan_and_no_live_run(goal_db):
    goal, plan, cid = _goal()
    assert [row["goal_id"] for row in goal_continuation.list_due(100, 10)] == [goal["goal_id"]]
    goal_plan_store.approve_plan(plan["plan_id"])
    db.create_agent_run("live", cid, "task")
    assert goal_continuation.list_due(100, 10) == []


def test_claim_is_atomic_and_advances_lease(goal_db):
    goal, _, cid = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    assert claimed is not None and claimed["attempts"] == 1
    assert claimed["next_run_at"] == 160
    assert goal_continuation.claim(goal["goal_id"], now=100, interval=60) is None
    assert goal_continuation.list_due(100, 10) == []
    assert len(goal_continuation.list_due(160, 10)) == 1


def test_no_progress_pauses_without_achieving_goal(goal_db):
    goal, _, cid = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    goal_continuation.finish(
        goal["goal_id"], expected_attempt=claimed["attempts"], fingerprint="same",
        progressed=False, next_run_at=160, max_no_progress=2,
    )
    second = goal_continuation.claim(goal["goal_id"], now=160, interval=60)
    goal_continuation.finish(
        goal["goal_id"], expected_attempt=second["attempts"], fingerprint="same",
        progressed=False, next_run_at=220, max_no_progress=2,
    )
    assert goal_continuation.list_due(999, 10) == []
    state = goal_continuation.get(goal["goal_id"])
    assert state["paused"] == 1
    assert state["stop_reason"] == "no_progress"
    current = goal_plan_store.get_goal_by_conversation(cid)
    assert current["status"] == "active"
    assert current["adjudication"] == "pending"


def test_finish_rejects_stale_attempt(goal_db):
    goal, _, cid = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    assert goal_continuation.finish(
        goal["goal_id"], expected_attempt=99, fingerprint="x", progressed=True,
        next_run_at=160, max_no_progress=5,
    ) is False
    assert goal_continuation.get(goal["goal_id"])["attempts"] == claimed["attempts"]


def test_resume_resets_exhausted_attempt_budget(goal_db):
    goal, _, _ = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    goal_continuation.finish(
        goal["goal_id"], expected_attempt=claimed["attempts"], fingerprint="x",
        progressed=True, next_run_at=160, max_attempts=1,
    )
    assert goal_continuation.get(goal["goal_id"])["stop_reason"] == "attempt_budget"
    assert goal_continuation.resume(goal["goal_id"]) is True
    state = goal_continuation.get(goal["goal_id"])
    assert state["paused"] == 0 and state["attempts"] == 0
    assert goal_continuation.list_due(200, 10)[0]["goal_id"] == goal["goal_id"]


def test_latest_human_help_blocks_crashed_goal_until_explicit_resume(goal_db):
    goal, _, cid = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    db.create_agent_run("goal-crashed", cid, "task", mode="goal")
    db.update_agent_run("goal-crashed", status="human_help")
    assert goal_continuation.list_due(999, 10) == []
    assert goal_continuation.claim(goal["goal_id"], now=999, interval=60) is None

    assert goal_continuation.resume(goal["goal_id"]) is True
    assert goal_continuation.list_due(999, 10)[0]["goal_id"] == goal["goal_id"]
    second = goal_continuation.claim(goal["goal_id"], now=999, interval=60)
    goal_continuation.finish(
        goal["goal_id"], expected_attempt=second["attempts"], fingerprint="x",
        progressed=True, next_run_at=0,
    )
    assert goal_continuation.list_due(999, 10) == []
    assert claimed["attempts"] == second["attempts"] == 1


@pytest.mark.asyncio
async def test_resumed_goal_stays_due_when_global_slot_is_busy(goal_db, monkeypatch):
    from agent import goal_continuation_engine as engine_mod

    goal, _, cid = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    goal_continuation.finish(
        goal["goal_id"], expected_attempt=claimed["attempts"], fingerprint="same",
        progressed=False, next_run_at=160, stop_reason="human_help",
    )
    db.create_agent_run("goal-needs-help", cid, "task", mode="goal")
    db.update_agent_run("goal-needs-help", status="human_help")
    assert goal_continuation.resume(goal["goal_id"]) is True

    async def global_slot():
        return False

    monkeypatch.setattr("routes.agent._acquire_global_run_slot", global_slot)
    engine = engine_mod.GoalContinuationEngine(poll_seconds=60)
    assert await engine.tick_once() == 1
    await asyncio.gather(*list(engine._tasks))

    state = goal_continuation.get(goal["goal_id"])
    assert state["paused"] == 0 and state["stop_reason"] == "user_resumed"
    assert [row["goal_id"] for row in goal_continuation.list_due(state["next_run_at"], 10)] == [goal["goal_id"]]


@pytest.mark.asyncio
async def test_user_approval_resumes_paused_goal_continuation(goal_db):
    from routes.goal_plan import approve_plan
    goal, plan, _ = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    goal_continuation.finish(
        goal["goal_id"], expected_attempt=claimed["attempts"], fingerprint="same",
        progressed=False, next_run_at=160, max_no_progress=1,
    )
    assert goal_continuation.get(goal["goal_id"])["paused"] == 1
    goal_plan_store.supersede_plan(plan["plan_id"])
    replacement = goal_plan_store.create_plan_with_steps(
        goal["conversation_id"], goal["goal_id"], [{"title": "重规划第一步", "detail": "继续"}],
    )
    approved = await approve_plan(replacement["plan_id"])
    assert approved["status"] == "approved"
    state = goal_continuation.get(goal["goal_id"])
    assert state["paused"] == 0 and state["stop_reason"] == "user_resumed"
    assert goal_continuation.list_due(200, 10)[0]["goal_id"] == goal["goal_id"]


@pytest.mark.asyncio
async def test_explicit_resume_endpoint_does_not_adjudicate_goal(goal_db):
    from routes.goal_plan import resume_goal_continuation
    goal, _, cid = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    goal_continuation.finish(
        goal["goal_id"], expected_attempt=claimed["attempts"], fingerprint="same",
        progressed=False, next_run_at=160, stop_reason="permission",
    )
    result = await resume_goal_continuation(goal["goal_id"])
    assert result == {"resumed": True, "goal_id": goal["goal_id"]}
    current = goal_plan_store.get_goal_by_conversation(cid)
    assert current["status"] == "active" and current["adjudication"] == "pending"


@pytest.mark.asyncio
async def test_engine_runs_approved_goal_without_adjudicating_it(goal_db, monkeypatch):
    from agent import goal_continuation_engine as engine_mod
    goal, plan, cid = _goal()
    calls = []

    async def fake_run(task, **kwargs):
        calls.append((task, kwargs))
        steps = goal_plan_store.get_steps(plan["plan_id"])
        goal_plan_store.update_step_status(steps[0]["step_id"], "in_progress", run_id=kwargs["run_id"])
        goal_plan_store.update_step_status(
            steps[0]["step_id"], "completed", evidence="已验证", run_id=kwargs["run_id"],
        )
        return SimpleNamespace(
            content="完成一步", state="completed", error="", permission_requested=False,
            iterations=3, cost={"total_tokens": 42, "prompt_tokens": 30,
                                "completion_tokens": 12, "cost_usd": 0.01, "tool_call_count": 2},
        )

    async def global_slot():
        return True
    async def run_slot(cid, rid):
        return False
    monkeypatch.setattr(engine_mod, "run_headless_once", fake_run)
    monkeypatch.setattr("routes.agent._acquire_global_run_slot", global_slot)
    monkeypatch.setattr("routes.agent._acquire_run_slot", run_slot)
    monkeypatch.setattr("routes.agent._release_global_run_slot_sync", lambda: None)
    monkeypatch.setattr("routes.agent._release_run_slot_sync", lambda *args: None)
    engine = engine_mod.GoalContinuationEngine(poll_seconds=60)
    await engine.tick_once()
    await asyncio.gather(*list(engine._tasks))

    assert len(calls) == 1
    assert calls[0][1]["conversation_id"] == cid
    assert calls[0][1]["history"][-1]["content"] == "[系统] 自动接续已批准目标计划。"
    assert "目标是否达成由用户裁决" in calls[0][1]["system_prompt"]
    run = db.get_agent_run(calls[0][1]["run_id"])
    assert run["status"] == "completed"
    assert run["iteration"] == 3
    assert run["total_tokens"] == 42
    assert run["cost_usd"] == 0.01
    assert goal_plan_store.get_goal_by_conversation(cid)["status"] == "active"
    assert goal_continuation.get(goal["goal_id"])["no_progress"] == 0
    assert db.get_messages(cid)[-1]["content"] == "完成一步"


@pytest.mark.asyncio
async def test_engine_does_not_run_unapproved_plan(goal_db):
    from agent.goal_continuation_engine import GoalContinuationEngine
    _goal(approved=False)
    engine = GoalContinuationEngine(poll_seconds=60)
    assert await engine.tick_once() == 0


@pytest.mark.asyncio
async def test_engine_pauses_on_permission_block(goal_db, monkeypatch):
    from agent import goal_continuation_engine as engine_mod
    goal, _, cid = _goal()
    async def fake_run(task, **kwargs):
        return SimpleNamespace(content="", state="human_help", error="需要授权", permission_requested=True)
    async def global_slot():
        return True
    async def run_slot(cid, rid):
        return False
    monkeypatch.setattr(engine_mod, "run_headless_once", fake_run)
    monkeypatch.setattr("routes.agent._acquire_global_run_slot", global_slot)
    monkeypatch.setattr("routes.agent._acquire_run_slot", run_slot)
    monkeypatch.setattr("routes.agent._release_global_run_slot_sync", lambda: None)
    monkeypatch.setattr("routes.agent._release_run_slot_sync", lambda *args: None)
    engine = engine_mod.GoalContinuationEngine(poll_seconds=60)
    await engine.tick_once()
    await asyncio.gather(*list(engine._tasks))
    state = goal_continuation.get(goal["goal_id"])
    assert state["paused"] == 1 and state["stop_reason"] == "permission"
    assert goal_plan_store.get_goal_by_conversation(cid)["status"] == "active"


@pytest.mark.asyncio
async def test_engine_deduplicates_concurrent_ticks(goal_db, monkeypatch):
    from agent import goal_continuation_engine as engine_mod
    goal, _, _ = _goal()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fake_run(task, **kwargs):
        calls.append(kwargs["run_id"])
        entered.set()
        await release.wait()
        return SimpleNamespace(content="ok", state="completed", error="", permission_requested=False)

    async def global_slot():
        return True
    async def run_slot(cid, rid):
        return False

    monkeypatch.setattr(engine_mod, "run_headless_once", fake_run)
    monkeypatch.setattr("routes.agent._acquire_global_run_slot", global_slot)
    monkeypatch.setattr("routes.agent._acquire_run_slot", run_slot)
    monkeypatch.setattr("routes.agent._release_global_run_slot_sync", lambda: None)
    monkeypatch.setattr("routes.agent._release_run_slot_sync", lambda *args: None)
    engine = engine_mod.GoalContinuationEngine(poll_seconds=60)
    assert await engine.tick_once() == 1
    await entered.wait()
    assert await engine.tick_once() == 0
    release.set()
    await asyncio.gather(*list(engine._tasks))
    assert engine._running_conversations == set()
    assert len(calls) == 1
    assert goal_continuation.get(goal["goal_id"])["attempts"] == 1


@pytest.mark.asyncio
async def test_engine_releases_slots_and_pauses_after_exception(goal_db, monkeypatch):
    from agent import goal_continuation_engine as engine_mod
    goal, _, _ = _goal()
    released = []

    async def fake_run(task, **kwargs):
        raise RuntimeError("模型不可用")
    async def global_slot():
        return True
    async def run_slot(cid, rid):
        return False

    monkeypatch.setattr(engine_mod, "run_headless_once", fake_run)
    monkeypatch.setattr("routes.agent._acquire_global_run_slot", global_slot)
    monkeypatch.setattr("routes.agent._acquire_run_slot", run_slot)
    monkeypatch.setattr("routes.agent._release_global_run_slot_sync", lambda: released.append("global"))
    monkeypatch.setattr("routes.agent._release_run_slot_sync", lambda *args: released.append("run"))
    engine = engine_mod.GoalContinuationEngine(poll_seconds=60)
    await engine.tick_once()
    await asyncio.gather(*list(engine._tasks))
    assert released == ["run", "global"]
    state = goal_continuation.get(goal["goal_id"])
    assert state["paused"] == 1 and state["stop_reason"] == "human_help"
    assert db.get_agent_runs(limit=10)[0]["status"] == "human_help"
    assert engine._running_conversations == set()


@pytest.mark.asyncio
async def test_engine_start_and_stop_cancel_active_goal_run(goal_db, monkeypatch):
    from agent import goal_continuation_engine as engine_mod
    _goal()
    entered = asyncio.Event()

    async def fake_run(task, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    async def global_slot():
        return True
    async def run_slot(cid, rid):
        return False

    monkeypatch.setattr(engine_mod, "run_headless_once", fake_run)
    monkeypatch.setattr("routes.agent._acquire_global_run_slot", global_slot)
    monkeypatch.setattr("routes.agent._acquire_run_slot", run_slot)
    monkeypatch.setattr("routes.agent._release_global_run_slot_sync", lambda: None)
    monkeypatch.setattr("routes.agent._release_run_slot_sync", lambda *args: None)
    engine = engine_mod.GoalContinuationEngine(poll_seconds=60)
    engine.ensure_started()
    await entered.wait()
    await engine.stop()
    assert engine._task is not None and engine._task.cancelled()
    assert not engine._tasks
    assert not engine._running_conversations
    run = db.get_agent_runs(limit=10)[0]
    assert run["status"] == "cancelled"
    assert db.get_conversation(run["conversation_id"])["status"] == "completed"


def test_attempt_budget_pauses_goal(goal_db):
    goal, _, cid = _goal()
    claimed = goal_continuation.claim(goal["goal_id"], now=100, interval=60)
    goal_continuation.finish(
        goal["goal_id"], expected_attempt=claimed["attempts"], fingerprint="x",
        progressed=True, next_run_at=160, max_no_progress=5, max_attempts=1,
    )
    assert goal_continuation.get(goal["goal_id"])["stop_reason"] == "attempt_budget"
