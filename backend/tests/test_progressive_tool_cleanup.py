"""P3 渐进式工具结果清理测试 — 组装期非破坏性归档早期 tool 结果。

覆盖：
1. 短 run（含 tool 段 ≤ kept+grace）不触发，原样返回
2. 长 run：早期段归档为占位符，最近 kept 段原样
3. tool_call_id 与 assistant(tool_calls) 配对结构保留（vLLM 400 红线）
4. ctx.messages 不被修改（非破坏性，每次 assemble 重算）
5. high 优先级结果归档时保留头尾短截断（≤ stale_tool_high_keep_chars）
6. 出网载荷仍剥离 priority 字段

运行: python -m pytest backend/tests/test_progressive_tool_cleanup.py -v
"""
import copy

import asyncio

from agent.harness.bridle import Bridle
from agent.harness.contracts import AgentConfig, RunContext


def _bridle(**cfg) -> Bridle:
    return Bridle(max_context_tokens=16384)


def _make_ctx(n_tool_segs: int = 8) -> RunContext:
    ctx = RunContext.new(task="批量重启服务", config=AgentConfig())
    for i in range(n_tool_segs):
        ctx.messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "shell_exec", "arguments": '{"command":"echo hi"}'},
            }],
        })
        ctx.messages.append({
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": "R" * 500,
            "priority": "normal",
        })
    return ctx


def test_short_run_not_affected():
    """短 run（含 tool 段 ≤ kept+grace=5）不触发清理"""
    b = _bridle()
    ctx = _make_ctx(n_tool_segs=5)
    before = copy.deepcopy(ctx.messages)
    out = asyncio.run(b.assemble(ctx))
    contents = [m.get("content") for m in out if m.get("role") == "tool"]
    assert all(c == "R" * 500 for c in contents), "短 run 不应触发归档"
    assert ctx.messages == before


def test_long_run_archives_early_segments():
    """长 run：前 (n-kept) 段归档为占位符，最近 kept 段原样"""
    b = _bridle()
    ctx = _make_ctx(n_tool_segs=8)
    out = asyncio.run(b.assemble(ctx))
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert len(tool_msgs) == 8
    kept = 3 + 2  # stale_tool_kept_segments + stale_tool_grace
    for i, m in enumerate(tool_msgs):
        if i < 8 - kept:
            assert m["content"] == "[工具结果已归档: 原 500 字符]", (
                f"第 {i} 段应归档，实得: {m['content'][:60]!r}")
        else:
            assert m["content"] == "R" * 500, f"最近 kept 段应原样保留（第 {i} 段）"


def test_tool_call_pairing_preserved():
    """tool_call_id 与 assistant(tool_calls) 配对结构保留（vLLM 400 红线）"""
    b = _bridle()
    ctx = _make_ctx(n_tool_segs=8)
    out = asyncio.run(b.assemble(ctx))
    tool_ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    assert tool_ids == [f"call_{i}" for i in range(8)], "tool_call_id 必须全保留"
    for i in range(8):
        idx = next(j for j, m in enumerate(out)
                   if m.get("role") == "tool" and m.get("tool_call_id") == f"call_{i}")
        prev = out[idx - 1]
        assert prev.get("role") == "assistant" and prev.get("tool_calls"), (
            "每个 tool 消息前必须有配对的 assistant(tool_calls)")


def test_ctx_messages_untouched():
    """非破坏性：assemble 不得修改 ctx.messages，每次重算"""
    b = _bridle()
    ctx = _make_ctx(n_tool_segs=8)
    before = copy.deepcopy(ctx.messages)
    asyncio.run(b.assemble(ctx))
    assert ctx.messages == before, "assemble 的 P3 清理必须不动 ctx.messages"
    # 再次组装仍能从原始数据重算出同样结果
    out_again = asyncio.run(b.assemble(ctx))
    archived = [m for m in out_again if m.get("role") == "tool"
                and str(m.get("content", "")).startswith("[工具结果已归档")]
    assert len(archived) == 3, "第二次组装应产出同样的归档结果"


def test_high_priority_keeps_short_truncation():
    """high 优先级结果归档时保留头尾短截断（≤ stale_tool_high_keep_chars=200）"""
    b = _bridle()
    ctx = _make_ctx(n_tool_segs=8)
    # 把第 0 段的 tool 结果改为 high
    ctx.messages[1]["priority"] = "high"
    ctx.messages[1]["content"] = "HEAD-MARKER" + "M" * 2000 + "TAIL-MARKER"
    out = asyncio.run(b.assemble(ctx))
    m = next(m for m in out if m.get("role") == "tool" and m.get("tool_call_id") == "call_0")
    assert len(m["content"]) <= 200, f"high 归档应短截断 ≤200 字符，实得 {len(m['content'])}"
    assert "HEAD-MARKER" in m["content"] and "TAIL-MARKER" in m["content"], (
        "high 归档应保留头尾内容")
    # normal 的其他早段仍是占位符
    m1 = next(m for m in out if m.get("role") == "tool" and m.get("tool_call_id") == "call_1")
    assert m1["content"] == "[工具结果已归档: 原 500 字符]"


def test_priority_stripped_from_payload():
    """出网载荷仍剥离 priority 字段（清理产物同样要过 _strip_tool_priority）"""
    b = _bridle()
    ctx = _make_ctx(n_tool_segs=8)
    out = asyncio.run(b.assemble(ctx))
    assert all("priority" not in m for m in out), [
        m for m in out if "priority" in m]
