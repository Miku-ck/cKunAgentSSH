"""完整 Harness 安全点序列化与异步持久化。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from db import run_safe_points as store

logger = logging.getLogger(__name__)

_RUNTIME_METADATA = {
    "saddle", "governance", "cost_tracker", "emit_event", "operation_ledger", "safe_point",
    "run_started_mono",
}
_SECRET_KEY = re.compile(
    r"(api[_-]?key|password|secret|credential|(?:^|[_-])token(?:$|[_-]))",
    re.IGNORECASE,
)


def validate_message_pairs(messages: list[dict[str, Any]]) -> None:
    """拒绝孤儿 tool 消息和交错的 assistant.tool_calls。"""
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            calls = message.get("tool_calls") or []
            if pending:
                raise ValueError("上一组 tool_calls 尚未完成便出现新的 assistant.tool_calls")
            pending = {str(call["id"]) for call in calls if call.get("id")}
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in pending:
                raise ValueError(f"孤儿 tool 消息: {call_id}")
            pending.remove(call_id)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if _SECRET_KEY.search(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return _redact(asdict(value))
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"安全点不能序列化 {type(value).__name__}")


def _metadata_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in _RUNTIME_METADATA or key.startswith("_"):
            continue
        try:
            safe_value = "[redacted]" if _SECRET_KEY.search(key) else _redact(value)
            result[key] = json.loads(json.dumps(safe_value, default=_json_default, ensure_ascii=False))
        except (TypeError, ValueError):
            logger.debug("安全点跳过不可序列化 metadata=%s", key)
    return result


def snapshot_context(ctx, phase: str, **extra: Any) -> dict[str, Any]:
    undo_stack = []
    for action in ctx.undo_stack:
        if is_dataclass(action):
            undo_stack.append({"kind": type(action).__name__, "fields": _redact(asdict(action))})
        else:
            undo_stack.append({"kind": type(action).__name__, "fields": {}})
    metadata = _metadata_snapshot(ctx.metadata)
    tracker = ctx.metadata.get("cost_tracker")
    if tracker is not None:
        try:
            metadata["cost_snapshot"] = tracker.snapshot(iterations=ctx.iteration).to_dict()
        except Exception as exc:
            logger.warning("安全点读取成本快照失败 run=%s: %s", ctx.run_id, exc)
    started_mono = ctx.metadata.get("run_started_mono")
    elapsed = max(0.0, time.monotonic() - float(started_mono)) if started_mono is not None else 0.0
    payload = {
        "schema_version": 1,
        "run_id": ctx.run_id,
        "run_started_unix_s": time.time() - elapsed,
        "task": ctx.task,
        "conversation_id": ctx.conversation_id,
        "ssh_session_id": ctx.ssh_session_id,
        "environment": ctx.environment,
        "visible_terminal": ctx.visible_terminal,
        "system_prompt": ctx.system_prompt,
        "history": ctx.history,
        "memories": ctx.memories,
        "config": _redact(asdict(ctx.config)),
        "llm_model": ctx.llm_model,
        "skill_ids": ctx.skill_ids,
        "auto_skill_match": ctx.auto_skill_match,
        "state": ctx.state.value,
        "iteration": ctx.iteration,
        "messages": ctx.messages,
        "tool_calls_log": ctx.tool_calls_log,
        "state_history": ctx.state_history,
        "span_stack": list(ctx.span_stack),
        "undo_stack": undo_stack,
        "current_tool_call": ctx.current_tool_call,
        "warnings": ctx.warnings,
        "errors": ctx.errors,
        "metadata": metadata,
        "phase": phase,
        **extra,
    }
    validate_message_pairs(payload["messages"])
    return payload


def safe_point_for_run(*, enabled: bool | None, db_path: str) -> SafePoint | None:
    if enabled is False:
        return None
    if enabled is True or db_path:
        return SafePoint()
    return None


class SafePoint:
    """每个 run 一个写者锁和本地版本，避免并行只读工具互相覆盖。"""

    def __init__(self, version: int = 0) -> None:
        self.version = version
        self._lock = asyncio.Lock()

    async def save(self, ctx, phase: str, **extra: Any) -> dict[str, Any]:
        payload = snapshot_context(ctx, phase, **extra)
        async with self._lock:
            saved = await asyncio.to_thread(
                store.save_safe_point,
                ctx.run_id,
                expected_version=self.version,
                phase=phase,
                payload=payload,
            )
        if saved is None:
            raise RuntimeError(f"安全点版本冲突 (run={ctx.run_id}, version={self.version})")
        self.version = saved["version"]
        return saved

    @classmethod
    async def load(cls, run_id: str) -> tuple[dict[str, Any], "SafePoint"] | None:
        row = await asyncio.to_thread(store.load_safe_point, run_id)
        if row is None:
            return None
        validate_message_pairs(row["payload"].get("messages") or [])
        return row["payload"], cls(version=row["version"])
