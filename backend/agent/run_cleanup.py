"""终态运行的安全点清理：有未收口副作用时保留对账现场。"""
from __future__ import annotations

import logging

from db import operation_ledger, run_safe_points

logger = logging.getLogger(__name__)
_UNRESOLVED = {"prepared", "executing", "awaiting_delivery", "unknown"}


def cleanup_terminal_safe_point(run_id: str, status: str) -> bool:
    """无未收口副作用时清理；审批/人工状态始终保留。"""
    if status in ("human_help", "waiting_confirmation", "running", "init"):
        return False
    try:
        if any(row["status"] in _UNRESOLVED for row in operation_ledger.list_operations(run_id)):
            return False
        run_safe_points.delete_safe_point(run_id)
    except Exception:
        logger.warning("终态安全点清理失败，保留恢复现场 run=%s", run_id, exc_info=True)
        return False
    return True
