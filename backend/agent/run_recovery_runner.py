"""启动对账与保守恢复执行器。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import database as db
from agent.run_cleanup import cleanup_terminal_safe_point
from agent.run_recovery import classify_recovery, complete_terminal_tool_calls
from db import operation_ledger, run_safe_points

logger = logging.getLogger(__name__)
_recovery_tasks: set[asyncio.Task] = set()


def _classify(run_id: str):
    run = db.get_agent_run(run_id)
    if run is None:
        return None
    return classify_recovery(
        run,
        run_safe_points.load_safe_point(run_id),
        operation_ledger.list_operations(run_id),
    )


async def reconcile_stale_runs() -> int:
    """先于旧收尸逻辑运行；返回有过处置的滞留运行数。"""
    handled = 0
    rows = await asyncio.to_thread(db.get_agent_runs, status="running", limit=1000)
    init_rows = await asyncio.to_thread(db.get_agent_runs, status="init", limit=1000)
    for run in [*rows, *init_rows]:
        run_id = str(run.get("id") or "")
        decision = await asyncio.to_thread(_classify, run_id)
        if decision is None or decision.kind == "ignore":
            continue
        handled += 1
        if decision.kind == "auto":
            task = asyncio.create_task(execute_recovery(run_id))
            _recovery_tasks.add(task)
            task.add_done_callback(_recovery_tasks.discard)
            continue
        status = "human_help" if decision.kind in ("human", "reconcile") else "failed"
        await asyncio.to_thread(db.update_agent_run, run_id=run_id, status=status)
        logger.warning("启动对账 run=%s kind=%s: %s", run_id, decision.kind, decision.reason)
    return handled


async def execute_recovery(run_id: str) -> None:
    """无交互续跑：完整治理链、原预算、现有并发槽；不等待审批。"""
    from agent.cost_tracker import CostTracker
    from agent.harness import Harness, HarnessConfig
    from agent.harness.llm_client import StreamLLMClient
    from agent.tools import create_registry
    from routes.agent import (
        _acquire_global_run_slot, _acquire_run_slot,
        _release_global_run_slot_sync, _release_run_slot_sync,
    )
    from routes.terminal import _get_model_config

    decision = await asyncio.to_thread(_classify, run_id)
    if decision is None or decision.kind != "auto" or decision.safe_point is None:
        return
    safe_row = decision.safe_point
    payload = dict(safe_row["payload"])
    if safe_row["phase"] == "iteration":
        payload["iteration"] = int(payload.get("iteration") or 0) - 1
    run = await asyncio.to_thread(db.get_agent_run, run_id)
    if run is None:
        return
    conversation_id = str(payload.get("conversation_id") or "")
    if payload.get("ssh_session_id") or payload.get("visible_terminal"):
        await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="human_help")
        return
    model_cfg = await asyncio.to_thread(_get_model_config, str(run.get("model_used") or ""))
    if not model_cfg:
        await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="human_help")
        return
    if not await _acquire_global_run_slot():
        await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="human_help")
        return
    conflict = await _acquire_run_slot(conversation_id, run_id)
    if conflict:
        _release_global_run_slot_sync()
        await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="human_help")
        return
    try:
        operations = await asyncio.to_thread(operation_ledger.list_operations, run_id)
        payload["messages"] = complete_terminal_tool_calls(payload.get("messages") or [], operations)
        payload["phase"] = "recovered_tool_results"
        saved = await asyncio.to_thread(
            run_safe_points.save_safe_point, run_id,
            expected_version=int(safe_row["version"]), phase="recovered_tool_results",
            payload=payload,
        )
        if saved is None:
            raise RuntimeError("恢复前安全点版本发生变化")
        model_name = model_cfg.get("model_id") or model_cfg.get("id") or ""
        harness = Harness(HarnessConfig(
            base_url=model_cfg["base_url"], api_key=model_cfg["api_key"],
            model=model_name, max_context_tokens=model_cfg.get("max_tokens") or 16384,
        ))
        registry = create_registry()
        metadata = payload.get("metadata") or {}
        allowed_tools = _expert_tool_names(run)
        original = metadata.get("available_tool_names")
        if isinstance(original, list):
            original_names = {str(name) for name in original}
            allowed_tools = original_names if allowed_tools is None else allowed_tools & original_names
        if allowed_tools is not None:
            for name in list(registry.names()):
                if name not in allowed_tools:
                    registry.unregister(name)
        harness.assemble(
            llm_client=StreamLLMClient(
                base_url=model_cfg["base_url"], api_key=model_cfg["api_key"],
            ),
            tool_registry=registry,
            cost_tracker=CostTracker(model_id=model_name),
        )
        await asyncio.to_thread(
            db.update_agent_run, run_id=run_id, status="running",
            iteration=int(payload.get("iteration") or 0),
        )
        await asyncio.to_thread(db.update_conversation_status, conversation_id, "in_progress")
        final_state = ""
        final_iteration = int(payload.get("iteration") or 0)
        content = ""
        error = ""
        cost: dict[str, Any] = {}
        async for event in harness.run(
            task=str(payload.get("task") or ""),
            conversation_id=conversation_id,
            run_id=run_id,
            resume_safe_point=payload,
            permission_mode=str(metadata.get("permission_mode") or "normal"),
            await_approval=False,
        ):
            if event.kind == "done":
                done = event.payload or {}
                final_state = str(done.get("state") or "")
                final_iteration += max(0, int(done.get("iterations") or 0))
                content = str(done.get("content") or "")
                cost = dict(done.get("cost") or {})
            elif event.kind == "error":
                error = str((event.payload or {}).get("message") or error)
        status = final_state if final_state in (
            "completed", "failed", "human_help", "max_reached", "cancelled",
        ) else "failed"
        await asyncio.to_thread(
            db.update_agent_run, run_id=run_id, status=status,
            iteration=final_iteration,
            total_tokens=int(cost.get("total_tokens") or 0),
            prompt_tokens=int(cost.get("prompt_tokens") or 0),
            completion_tokens=int(cost.get("completion_tokens") or 0),
            cost_usd=float(cost.get("cost_usd") or 0),
            tool_call_count=int(cost.get("tool_call_count") or 0),
            context_summary=str(cost.get("context_summary") or "") or None,
        )
        if content or error:
            await asyncio.to_thread(
                db.add_message, conversation_id=conversation_id, role="assistant",
                content=content or error, run_id=run_id,
                agent_trace={"status": status, "currentState": "recovered", "errorMessage": error or None},
            )
        current = await asyncio.to_thread(db.get_conversation, conversation_id)
        if current and current.get("status") == "in_progress":
            await asyncio.to_thread(db.update_conversation_status, conversation_id, "completed")
        await asyncio.to_thread(cleanup_terminal_safe_point, run_id, status)
    except Exception:
        logger.exception("自动恢复运行失败 run=%s", run_id)
        await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="human_help")
        await asyncio.to_thread(db.update_conversation_status, conversation_id, "completed")
    finally:
        _release_run_slot_sync(conversation_id, run_id)
        _release_global_run_slot_sync()


def _expert_tool_names(run: dict) -> set[str] | None:
    raw = run.get("expert_snapshot_json")
    if not raw:
        return None
    try:
        snapshot = json.loads(raw)
        names = snapshot.get("tool_names")
        return set(names) if isinstance(names, list) else None
    except (TypeError, ValueError):
        return set()
