"""S1-01：中止/断连的 run 必须留下承载轨迹的 assistant 痕迹。

守护点：
- 有半截正文 ⇒ 落一条 role=assistant，正文就是那半截（不是空、也不是占位文案）；
- 只有工具调用、没有正文 ⇒ 仍落，文案沿用 done 分支的空输出约定；
- 流式期间什么都没产出 ⇒ 不落（「我按了停止」不该被伪造成一句助手发言）；
- 快照状态是 failed + currentState=interrupted，且**不带** costInfo ——
  断连路径拿不到统计，写全 0 等于把已消耗的 token 报成没消耗；
- 源码闸门：done 分支的每个 assistant 写点都要置 assistant_persisted，
  finally 的兜底段必须挂着这个闸门并单独包 BaseException + shield（防双写、防取消）。
"""
import asyncio
import inspect
import re

import pytest

import routes.agent as ra


class _Recorder:
    def __init__(self):
        self.calls = []

    def add_message(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": len(self.calls)}


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(ra.db, "add_message", rec.add_message)
    return rec


def _persist(**overrides):
    kwargs = {
        "conversation_id": "conv-1",
        "run_id": "run-1",
        "total_content": "",
        "total_reasoning": "",
        "tool_calls": [],
        "iterations": 0,
        "model_used": "gpt-test",
    }
    kwargs.update(overrides)
    return asyncio.run(ra.persist_interrupted_run(**kwargs))


def test_partial_content_is_saved_verbatim(recorder):
    assert _persist(total_content="已经写到一半的回答", iterations=3) is True
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["role"] == "assistant"
    assert call["content"] == "已经写到一半的回答"
    assert call["conversation_id"] == "conv-1"
    assert call["run_id"] == "run-1"


def test_tools_only_run_falls_back_to_interrupted_text(recorder):
    tools = [{"id": "1", "tool": "shell_exec", "args": {}, "iteration": 1}]
    assert _persist(tool_calls=tools, iterations=2) is True
    call = recorder.calls[0]
    assert call["content"] == "(AI 运行中断，未产生输出)"
    assert call["agent_trace"]["toolCalls"] == tools
    assert call["agent_trace"]["iterations"] == 2


def test_silent_run_persists_nothing(recorder):
    assert _persist() is False
    assert recorder.calls == []


def test_reasoning_only_still_persisted(recorder):
    assert _persist(total_reasoning="在想该怎么做") is True
    call = recorder.calls[0]
    assert call["reasoning"] == "在想该怎么做"
    assert call["content"] == "(AI 运行中断，未产生输出)"


def test_snapshot_shape_marks_interruption_without_cost(recorder):
    _persist(total_content="x")
    trace = recorder.calls[0]["agent_trace"]
    assert trace["status"] == "failed"
    assert trace["currentState"] == "interrupted"
    assert trace["lastRunId"] == "run-1"
    assert trace["errorMessage"] is None
    # 缺席而非 0：读侧按 costInfo?.xxx 真值判断，写 0 会说「这个 run 没花 token」
    assert "costInfo" not in trace
    assert "duration" not in trace


def _source() -> str:
    return inspect.getsource(ra)


def test_done_branch_write_points_arm_the_gate():
    """done 分支每处 assistant 落库都要紧跟 assistant_persisted = True。

    漏一处的后果是双写：兜底段以为没落过，再补一条一模一样的助手消息。
    """
    src = _source()
    hits = list(re.finditer(r'agent_trace=agent_trace_snapshot,', src))
    assert len(hits) >= 2, "done 分支的两处 assistant 落库点少了一处"
    for m in hits:
        tail = src[m.end():m.end() + 300]
        assert "assistant_persisted = True" in tail, "落库点未置闸门，断连兜底会双写"


def test_finally_fallback_is_gated_and_cancel_safe():
    src = _source()
    assert "if not assistant_persisted:" in src
    body = src[src.index("if not assistant_persisted:"):]
    assert "await asyncio.shield(" in body[:400]
    assert "except BaseException:" in body[:900]


def test_persist_helper_has_no_post_process():
    """后处理（标题/记忆/技能）在断连路径**刻意**不跑 —— 半截答案不该喂给记忆库。

    这条不是给实现开脱，是把取舍写成一等公民：将来谁要加 submit()，必须连带
    更新 docs/reviews 里的裁决口径，而不是顺手补上。
    """
    src = inspect.getsource(ra.persist_interrupted_run)
    assert "get_post_process_manager" not in src
