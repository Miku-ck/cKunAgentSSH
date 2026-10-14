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


def _human_help_payload_keys() -> set[str]:
    at = FLOW.index('kind="human_help_needed"')
    brace = FLOW.index("payload={", at) + len("payload=")
    return _keys_from_block(_braced_block(FLOW, brace))


def test_human_help_needed_projects_only_real_keys():
    """投影原先读 tool/args（生产者只写 reason + confidence），UI 因此一直收到空字段。"""
    produced = _human_help_payload_keys()
    read = _projected_keys("human_help_needed")
    assert produced and read
    assert read <= produced, f"投影读了生产者不写的键: {sorted(read - produced)}"
    assert "tool" not in read and "args" not in read


def test_human_help_needed_carries_confidence():
    out = SSEAdapter.to_sse_dict(
        TraceEvent(kind="human_help_needed", source="reins.confidence_monitor",
                   run_id="run-3", iteration=9,
                   payload={"reason": "连续低置信度", "confidence": 0.31})
    )
    assert out["reason"] == "连续低置信度"
    assert out["confidence"] == 0.31
    assert "tool" not in out and "args" not in out


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


# ── 事件类型清单的跨层一致性 ────────────────────────────────────────────────
# 同一份 SSE type 集合此前有三份手抄：后端 SSEAdapter._KIND_MAP、前端
# AgentSSEEvent.type 联合、agent.ts 的 switch。前两份已经各自漂走两个值——
# budget_extended / reflection_request / plan_mode_block 真的上了 Wire 却没人声明，
# clarification_* 前端在 case 里处理着而后端清单里没有（只靠 get(kind, kind) 兜底）。
# 漂移不报错，只是那两类事件在 Wire 上"存在"、在类型上"不存在"，加 default 也看不见。

CLIENT_TS = BACKEND.parent / "src" / "api" / "client.ts"
HARNESS = BACKEND / "agent" / "harness"


def _emitted_kinds() -> set[str]:
    """harness 里构造过的 TraceEvent kind。`kind="…"` 只出现在 TraceEvent(...) 形参位，
    所以这一条正则同时覆盖 reins 与 driver；用字面量扫描，变量传入的 kind 不在其列。"""
    kinds: set[str] = set()
    for path in sorted(HARNESS.rglob("*.py")):
        kinds |= set(re.findall(
            r'kind="(\w+)"', path.read_text(encoding="utf-8", errors="replace")))
    return kinds


def _union_line(src: str) -> str:
    m = re.search(r"export interface AgentSSEEvent\s*\{", src)
    assert m, "client.ts 里找不到 AgentSSEEvent"
    tm = re.search(r"\n\s*type:\s*([^\n]+)", src[m.end():m.end() + 4000])
    assert tm, "AgentSSEEvent 里找不到 type 联合"
    # 只吃一行：跨行会把接口里其它字面量算进枚举（同 test_model_default_mode_enum 的教训）
    return tm.group(1)


def _union_kinds(line: str) -> set[str]:
    return set(re.findall(r"'(\w+)'", line))


def test_kind_map_covers_every_emitted_kind():
    emitted = _emitted_kinds()
    assert {"clarification_needed", "plan_mode_block", "budget_extended"} <= emitted, (
        "扫描没走进 reins/ —— 这条守卫会因为读不到东西而恒真")
    declared = set(SSEAdapter._KIND_MAP)
    missing = emitted - declared
    assert not missing, (
        f"这些 kind 上了 Wire 却不在 _KIND_MAP 里（只靠恒等兜底，清单不再是全集）: "
        f"{sorted(missing)}")
    unused = declared - emitted
    assert not unused, f"_KIND_MAP 里有 harness 从不发出的死条目: {sorted(unused)}"


def test_frontend_union_matches_backend_registry():
    declared = set(SSEAdapter._KIND_MAP.values())
    union = _union_kinds(_union_line(CLIENT_TS.read_text(encoding="utf-8")))
    assert declared and union, "任一侧空集会把它变成恒真守卫"
    assert declared == union, (
        f"SSE type 清单漂移：后端发在 Wire 上但前端没声明 {sorted(declared - union)}；"
        f"前端声明了但后端不会发 {sorted(union - declared)}")


def test_guard_actually_catches_renamed_event_type():
    """自检：改名联合里一个 type，比对必须判为漂移。改动限定在抽出来的那一行内，
    否则 `'tool_deliver'` 之类同名字面量会被别处先命中，自检退化成空操作。"""
    line = _union_line(CLIENT_TS.read_text(encoding="utf-8"))
    declared = set(SSEAdapter._KIND_MAP.values())
    mutated = _union_kinds(line.replace("'tool_deliver'", "'deliver_cmd'", 1))
    assert "tool_deliver" not in mutated, "改名没命中 type 联合行，这条自检是摆设"
    assert declared - mutated == {"tool_deliver"} and mutated - declared == {"deliver_cmd"}, (
        f"改名后的联合没被判为漂移: {mutated ^ declared}")
