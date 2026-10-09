# -*- coding: utf-8 -*-
"""S1-06：TraceEvent 的 SSE 投影不许读生产者从未写的键。

`SSEAdapter.to_sse_dict` 是**白名单**投影：只把列出的键搬到 Wire 上。于是漂移有两种，
都不报错也不告警：
- 投影读一个生产者从不写的键 ⇒ 那个字段恒为空（`budget_extended.reason` 就是这样）；
- 生产者写了一个说明性的键而投影没搬 ⇒ 中间断掉（`done.context_summary` 曾经就是这样，
  agent_runs.context_summary 因此恒为 NULL）。

本测试把「投影读的键 ⊆ 生产者写的键」钉成源码守卫：改任一侧都会被抓到，
不必等某个 UI 某天来读它才发现字段一直是空的。
"""
import re
from pathlib import Path

from agent.harness.contracts import TraceEvent
from agent.harness.telemetry import SSEAdapter

BACKEND = Path(__file__).resolve().parents[1]
TELEMETRY = (BACKEND / "agent/harness/telemetry.py").read_text(encoding="utf-8")
FLOW = (BACKEND / "agent/harness/reins/flow.py").read_text(encoding="utf-8")
LIFECYCLE = (BACKEND / "agent/harness/reins/lifecycle.py").read_text(encoding="utf-8")


def _braced_block(src: str, open_idx: int) -> str:
    """从某个 '{' 起按配对深度切出整个块（dict 里还会嵌套 dict）。"""
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    raise AssertionError("花括号未配对")


def _keys_from_block(block: str) -> set[str]:
    return set(re.findall(r'"(\w+)":', block))


def _projected_keys(kind: str) -> set[str]:
    """投影侧从 payload 读走的键。分支边界取下一个 `elif kind ==`。"""
    start = TELEMETRY.index(f'kind == "{kind}"')
    nxt = TELEMETRY.find("elif kind ==", start + 1)
    body = TELEMETRY[start:nxt if nxt > 0 else len(TELEMETRY)]
    return set(re.findall(r'payload\.get\("(\w+)"', body))


def _budget_extended_payload_keys() -> set[str]:
    at = FLOW.index('kind="budget_extended"')
    brace = FLOW.index("payload={", at) + len("payload=")
    return _keys_from_block(_braced_block(FLOW, brace))


def _reflection_request_payload_keys() -> set[str]:
    # ReflectionRein 先把 payload 拼进一个局部变量，再 `payload=payload` 交给事件
    at = LIFECYCLE.index('kind="reflection_request"')
    start = LIFECYCLE.rindex("payload = {", 0, at) + len("payload = ")
    return _keys_from_block(_braced_block(LIFECYCLE, start))


def test_budget_extended_projects_only_real_keys():
    produced = _budget_extended_payload_keys()
    read = _projected_keys("budget_extended")
    assert produced and read, "两侧任一空集会让这条守卫变成恒真"
    assert read <= produced, f"投影读了生产者不写的键: {sorted(read - produced)}"


def test_reflection_request_projects_only_real_keys():
    produced = _reflection_request_payload_keys()
    read = _projected_keys("reflection_request")
    assert produced and read, "两侧任一空集会让这条守卫变成恒真"
    assert read <= produced, f"投影读了生产者不写的键: {sorted(read - produced)}"


def test_phantom_reason_is_gone():
    """这条曾恒为空串：生产者从来没写 reason，投影却读它。"""
    assert "reason" not in _projected_keys("budget_extended")


def test_budget_extended_carries_the_why_fields_that_do_exist():
    """加预算的「为什么」是 added/files_edited/cap —— 现在真的搬到 Wire 上了。"""
    payload = {
        "initial": 30, "extended_to": 45, "added": 15,
        "files_edited": 3, "files_read": 7, "total_tool_calls": 12, "cap": 60,
    }
    out = SSEAdapter.to_sse_dict(
        TraceEvent(kind="budget_extended", source="reins.dynamic_budget",
                   run_id="run-1", iteration=4, payload=payload)
    )
    for k, v in payload.items():
        assert out[k] == v
    assert "reason" not in out


def test_reflection_request_projection_shape():
    out = SSEAdapter.to_sse_dict(
        TraceEvent(kind="reflection_request", source="reflection_rein",
                   run_id="run-2", iteration=6,
                   payload={
                       "run_id": "run-2", "mode": "ssh", "final_state": "completed",
                       "iterations": 6, "max_iterations": 30, "tool_calls_count": 3,
                       "unique_tools": ["shell_exec"], "failed_tools": [],
                       "has_warnings": True, "has_cost": False,
                   })
    )
    assert out["final_state"] == "completed"
    assert out["tool_calls_count"] == 3
    assert out["has_warnings"] is True
