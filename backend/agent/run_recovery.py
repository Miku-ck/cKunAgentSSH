"""启动对账：只放行证据完整且没有未知副作用的运行。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.harness.contracts import TERMINAL_RUN_STATES, State
from agent.harness.safe_point import validate_message_pairs


@dataclass(frozen=True)
class RecoveryDecision:
    kind: str
    reason: str
    run_id: str = ""
    safe_point: dict[str, Any] | None = None


def pending_tool_call_ids(messages: list[dict[str, Any]]) -> list[str]:
    pending: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if call.get("id"):
                    pending.add(str(call["id"]))
        elif message.get("role") == "tool":
            pending.discard(str(message.get("tool_call_id") or ""))
    return sorted(pending)


def complete_terminal_tool_calls(
    messages: list[dict[str, Any]], operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """用账本里的真实结果补齐崩溃前未落盘的 tool 消息，不重新执行。"""
    by_call = {str(op.get("tool_call_id") or ""): op for op in operations}
    completed = list(messages)
    for call_id in pending_tool_call_ids(completed):
        operation = by_call.get(call_id)
        if operation is None or operation.get("status") not in (
            "succeeded", "failed", "not_executed",
        ):
            raise ValueError(f"工具调用 {call_id} 存在未知副作用，不能补齐结果")
        status = operation["status"]
        if status == "succeeded":
            content = str(operation.get("output") or "")
        elif status == "failed":
            content = str(operation.get("error") or "工具执行失败")
        else:
            content = str(operation.get("error") or "工具未执行")
        completed.append({"role": "tool", "tool_call_id": call_id, "content": content})
    validate_message_pairs(completed)
    return completed


def classify_recovery(
    run: dict[str, Any],
    safe_point: dict[str, Any] | None,
    operations: list[dict[str, Any]],
) -> RecoveryDecision:
    run_id = str(run.get("id") or "")
    status = str(run.get("status") or "")
    if status in TERMINAL_RUN_STATES or status == "waiting_confirmation":
        return RecoveryDecision("ignore", "运行已终态或正在等待用户", run_id, safe_point)
    if safe_point is None:
        return RecoveryDecision("fail", "没有完整运行安全点，无法证明断点", run_id)
    payload = safe_point.get("payload") or {}
    if payload.get("schema_version") != 1:
        return RecoveryDecision("fail", "安全点版本不受支持", run_id, safe_point)
    for field in ("run_id", "conversation_id", "task"):
        expected = run_id if field == "run_id" else run.get(field)
        if str(payload.get(field) or "") != str(expected or ""):
            return RecoveryDecision("fail", f"安全点与运行记录的 {field} 不一致", run_id, safe_point)
    try:
        state = State(str(payload.get("state") or ""))
    except ValueError:
        return RecoveryDecision("fail", "安全点运行状态无效", run_id, safe_point)
    if state in TERMINAL_RUN_STATES:
        return RecoveryDecision("fail", "安全点已记录终态，不应自动复活", run_id, safe_point)
    if payload.get("ssh_session_id") or payload.get("visible_terminal"):
        return RecoveryDecision("human", "断点依赖旧 SSH 会话或可见终端，需重新建立后再处理", run_id, safe_point)
    phase = str(safe_point.get("phase") or payload.get("phase") or "")
    if phase in ("waiting_approval", "awaiting_visible_result"):
        return RecoveryDecision("human", f"断点停在{phase}，必须由用户处理", run_id, safe_point)
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return RecoveryDecision("fail", "安全点缺少完整消息历史", run_id, safe_point)
    try:
        validate_message_pairs(messages)
    except ValueError as exc:
        return RecoveryDecision("fail", str(exc), run_id, safe_point)
    unresolved = [
        op for op in operations
        if op.get("status") in ("prepared", "executing", "unknown", "awaiting_delivery")
    ]
    if unresolved:
        states = ", ".join(sorted({str(op.get("status")) for op in unresolved}))
        return RecoveryDecision("reconcile", f"存在未闭合副作用（{states}），禁止自动重放", run_id, safe_point)
    by_call = {str(op.get("tool_call_id") or ""): op for op in operations}
    for call_id in pending_tool_call_ids(messages):
        if call_id not in by_call:
            return RecoveryDecision("reconcile", f"工具调用 {call_id} 没有副作用账本记录", run_id, safe_point)
    iteration = int(payload.get("iteration") or 0)
    if phase == "iteration":
        iteration -= 1
    max_iterations = int((payload.get("config") or {}).get("max_iterations") or 0)
    if iteration + 1 >= max_iterations:
        return RecoveryDecision("fail", "迭代预算已耗尽", run_id, safe_point)
    return RecoveryDecision("auto", "安全点完整且所有副作用均有确定结果", run_id, safe_point)
