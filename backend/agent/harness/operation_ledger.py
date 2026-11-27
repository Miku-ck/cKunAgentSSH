"""Harness 与 SQLite 副作用账本之间的异步适配器。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from db import operation_ledger as store

# 明确证明无外部突变的工具不落账；未知工具按可能写副作用处理。
READONLY_OPERATION_TOOLS = frozenset({
    "file_read", "file_glob", "file_grep", "read_image", "log_read",
    "sys_overview", "process_inspect", "network_probe", "job_list", "job_output",
    "web_search", "web_fetch", "memory_search", "load_skill", "runbook_search",
    "fleet_sessions", "fleet_overview",
})


def operation_id_for(call, ctx) -> str:
    identity = {
        "run_id": ctx.run_id,
        "tool_call_id": call.id,
        "iteration": ctx.iteration,
        "tool_name": call.name,
        "arguments": call.args,
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "op_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def ledger_for_run(*, enabled: bool | None, db_path: str) -> OperationLedger | None:
    if enabled is False:
        return None
    if enabled is True or db_path:
        return OperationLedger()
    return None


class OperationLedger:
    """同步 SQLite 存储在 asyncio 线程中的薄适配层。"""

    async def prepare(self, *, call, ctx, idempotent: bool, reversible: bool) -> dict[str, Any]:
        return await asyncio.to_thread(
            store.create_operation,
            operation_id=operation_id_for(call, ctx),
            run_id=ctx.run_id,
            conversation_id=ctx.conversation_id,
            tool_call_id=call.id,
            iteration=ctx.iteration,
            tool_name=call.name,
            arguments=call.args,
            idempotent=idempotent,
            reversible=reversible,
        )

    async def close_delivery(
        self,
        operation_id: str,
        *,
        to_status: str,
        output: str = "",
        error: str = "",
    ) -> bool:
        operation = await asyncio.to_thread(store.get_operation, operation_id)
        if operation is None:
            return False
        if operation["status"] == to_status:
            return True
        if operation["status"] != "awaiting_delivery":
            return False
        return await self.transition(
            operation, to_status=to_status, output=output, error=error,
        )

    async def transition(
        self,
        operation: dict[str, Any],
        *,
        to_status: str,
        output: str = "",
        error: str = "",
        duration_ms: int = 0,
        note: str = "",
    ) -> bool:
        return await asyncio.to_thread(
            store.transition_operation,
            operation["operation_id"],
            from_status=operation["status"],
            from_version=operation["version"],
            to_status=to_status,
            output=output,
            error=error,
            duration_ms=duration_ms,
            note=note,
        )
