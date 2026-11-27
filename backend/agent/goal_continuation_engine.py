"""跨会话 Goal 持续推进：只执行用户已批准计划，永不打裁决 Goal。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import database as db
from agent.headless_run import run_headless_once
from agent.run_cleanup import cleanup_terminal_safe_point
from db import goal_continuation, goal_plan_store
from db._util import _new_id
from routes._goal_plan_setup import load_goal_plan_state

logger = logging.getLogger(__name__)


def _fingerprint(snapshot: dict[str, Any]) -> str:
    goal = snapshot.get("goal") or {}
    plan = snapshot.get("plan") or {}
    steps = snapshot.get("steps") or []
    return json.dumps({
        "goal_version": goal.get("version"),
        "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("version"),
        "steps": [{key: step.get(key) for key in (
            "step_id", "status", "evidence", "blocked_reason", "updated_at",
        )} for step in steps],
    }, ensure_ascii=False, sort_keys=True)


def _has_open_steps(snapshot: dict[str, Any]) -> bool:
    return any(step.get("status") in ("pending", "in_progress", "blocked")
               for step in snapshot.get("steps") or [])


class GoalContinuationEngine:
    """短轮询 + SQLite 租约；无进展或权限阻塞时暂停等用户处理。"""

    def __init__(self, poll_seconds: int = 60) -> None:
        self.poll_seconds = max(5, poll_seconds)
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._running_conversations: set[str] = set()

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Goal 持续推进 tick 失败", exc_info=True)
            await asyncio.sleep(self.poll_seconds)

    async def tick_once(self) -> int:
        now = int(time.time())
        candidates = await asyncio.to_thread(goal_continuation.list_due, now, 10)
        scheduled = 0
        for candidate in candidates:
            cid = str(candidate["conversation_id"])
            if cid in self._running_conversations:
                continue
            self._running_conversations.add(cid)
            task = asyncio.create_task(self._execute(candidate))
            self._tasks.add(task)
            task.add_done_callback(self._task_finished)
            task.add_done_callback(lambda _task, key=cid: self._running_conversations.discard(key))
            scheduled += 1
        return scheduled

    def _task_finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Goal 持续推进任务异常", exc_info=task.exception())

    async def _execute(self, candidate: dict[str, Any]) -> None:
        from routes.agent import (
            _acquire_global_run_slot, _acquire_run_slot,
            _release_global_run_slot_sync, _release_run_slot_sync,
        )

        goal_id = str(candidate["goal_id"])
        cid = str(candidate["conversation_id"])
        now = int(time.time())
        claimed = await asyncio.to_thread(
            goal_continuation.claim, goal_id, now=now, interval=self.poll_seconds,
        )
        if claimed is None:
            return
        run_id = ""
        global_held = False
        run_held = False
        try:
            snapshot = await asyncio.to_thread(goal_plan_store.get_snapshot, cid)
            goal = snapshot.get("goal") or {}
            plan = snapshot.get("plan") or {}
            if (goal.get("status") != "active" or plan.get("plan_id") != candidate["plan_id"]
                    or plan.get("status") not in ("approved", "active") or not _has_open_steps(snapshot)):
                await asyncio.to_thread(
                    goal_continuation.finish, goal_id,
                    expected_attempt=claimed["attempts"], fingerprint="", progressed=False,
                    next_run_at=now + self.poll_seconds, stop_reason="plan_changed",
                )
                return
            before = _fingerprint(snapshot)
            if not await _acquire_global_run_slot():
                await asyncio.to_thread(
                    goal_continuation.finish, goal_id,
                    expected_attempt=claimed["attempts"], fingerprint=before, progressed=False,
                    next_run_at=now + self.poll_seconds,
                    resumed_by_user=bool(claimed.get("resumed_by_user")),
                )
                return
            global_held = True
            run_id = f"goal-{_new_id()}"
            conflict = await _acquire_run_slot(cid, run_id)
            if conflict not in (None, False):
                await asyncio.to_thread(
                    goal_continuation.finish, goal_id,
                    expected_attempt=claimed["attempts"], fingerprint=before, progressed=False,
                    next_run_at=now + self.poll_seconds,
                    resumed_by_user=bool(claimed.get("resumed_by_user")),
                )
                return
            run_held = True
            await asyncio.to_thread(
                db.create_agent_run, run_id, cid, str(goal.get("statement") or "继续推进目标"),
                max_iterations=8, mode="goal",
            )
            await asyncio.to_thread(db.update_conversation_status, cid, "in_progress")
            state = load_goal_plan_state(cid)
            if not state.get("injection") or state.get("force_plan"):
                raise RuntimeError("Goal 计划上下文不可用或尚未批准，拒绝无人值守执行")
            prompt = (state["injection"] + "\n\n[跨会话持续推进]\n"
                      "接续当前已批准计划，只推进确有把握的步骤；完成后附证据。"
                      "目标是否达成由用户裁决，禁止自行宣告 achieved。")
            await asyncio.to_thread(
                db.add_message, cid, "user", "[系统] 自动接续已批准目标计划。", run_id=run_id,
            )
            history = await asyncio.to_thread(db.get_recent_messages, cid, 20)
            result = await run_headless_once(
                task=str(goal.get("statement") or "继续推进当前目标计划"),
                run_id=run_id, conversation_id=cid, max_iterations=8,
                system_prompt=prompt, history=history, log_tag=f"goal:{goal_id}",
            )
            status = result.state if result.state in (
                "completed", "failed", "human_help", "max_reached", "cancelled",
            ) else "failed"
            if result.permission_requested:
                status = "human_help"
            cost = getattr(result, "cost", None) or {}
            iterations = max(0, int(getattr(result, "iterations", 0) or 0))
            await asyncio.to_thread(
                db.update_agent_run, run_id, status,
                iteration=iterations,
                total_tokens=int(cost.get("total_tokens") or 0),
                prompt_tokens=int(cost.get("prompt_tokens") or 0),
                completion_tokens=int(cost.get("completion_tokens") or 0),
                cost_usd=float(cost.get("cost_usd") or 0.0),
                tool_call_count=int(cost.get("tool_call_count") or 0),
                duration_ms=int(cost.get("duration_ms") or 0),
            )
            if result.content or result.error:
                await asyncio.to_thread(
                    db.add_message, cid, "assistant", result.content or result.error,
                    run_id=run_id, agent_trace={"status": status, "currentState": "goal_continuation"},
                )
            await asyncio.to_thread(db.update_conversation_status, cid, "completed")
            await asyncio.to_thread(cleanup_terminal_safe_point, run_id, status)
            after = await asyncio.to_thread(goal_plan_store.get_snapshot, cid)
            progressed = _fingerprint(after) != before
            stop_reason = "permission" if result.permission_requested else (
                "human_help" if status == "human_help" else
                "steps_complete" if not _has_open_steps(after) else ""
            )
            await asyncio.to_thread(
                goal_continuation.finish, goal_id,
                expected_attempt=claimed["attempts"], fingerprint=_fingerprint(after),
                progressed=progressed, next_run_at=int(time.time()) + self.poll_seconds,
                stop_reason=stop_reason,
            )
        except asyncio.CancelledError:
            if run_id:
                await asyncio.to_thread(db.update_agent_run, run_id, "cancelled")
                await asyncio.to_thread(db.update_conversation_status, cid, "completed")
                await asyncio.to_thread(cleanup_terminal_safe_point, run_id, "cancelled")
                await asyncio.to_thread(
                    goal_continuation.finish, goal_id,
                    expected_attempt=claimed["attempts"], fingerprint="", progressed=False,
                    next_run_at=int(time.time()) + self.poll_seconds,
                )
            raise
        except Exception:
            logger.exception("Goal 自动推进失败 goal=%s", goal_id)
            if run_id:
                await asyncio.to_thread(db.update_agent_run, run_id, "human_help")
                await asyncio.to_thread(db.update_conversation_status, cid, "completed")
                await asyncio.to_thread(cleanup_terminal_safe_point, run_id, "human_help")
            await asyncio.to_thread(
                goal_continuation.finish, goal_id,
                expected_attempt=claimed["attempts"], fingerprint="", progressed=False,
                next_run_at=int(time.time()) + self.poll_seconds,
                stop_reason="human_help",
            )
        finally:
            if run_held:
                _release_run_slot_sync(cid, run_id)
            if global_held:
                _release_global_run_slot_sync()


_engine: GoalContinuationEngine | None = None


def get_goal_continuation_engine() -> GoalContinuationEngine:
    global _engine
    if _engine is None:
        _engine = GoalContinuationEngine()
    return _engine
