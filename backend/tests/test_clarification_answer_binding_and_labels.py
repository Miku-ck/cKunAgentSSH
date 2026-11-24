"""澄清回路的两个真缺陷及其修复后的反向守卫。

审查里那条「tool_call_id 存了却从不比对」的说法，机制讲错了（答案只从用户 POST
进来，存的是合成 uuid，跟模型那次 tool call 的 id 无关），但**缺陷是真的**，形态不同：

1. 旧实现消费只看 run_id、且消费后放行下一次任意调用 —— 答案被花在无关操作上，
   模型转去的那个工具收不到任何澄清结果。现行设计（P2 域拆分后的
   consume_answer_decision）：答案就绪后**下一次任意 PRE_TOOL_CALL 都恒拦截**，
   强制把澄清结果注入给模型（答案不是执行许可，执行许可必须由新一轮调用重新
   走完整治理链）。本文件钉住新语义的两侧：结果必达模型、且答案只消费一次。
2. `_format_answers` 只输出字母。选项文字只存在于 `clarification_needed` 的 SSE
   payload（前端本地推进），模型侧从头到尾只收到过「一个澄清问题正在等待用户回答」
   —— 注入 "  Q1: A" / "  Q2: B" 等于把用户的答案压缩成模型无法解读的字母，
   整条澄清回路对模型不产生任何可用信息。现要求带回问题原文与选项文字。

运行: python -m pytest backend/tests/test_clarification_answer_binding_and_labels.py -v
"""
import pytest

from agent.harness.contracts import AgentConfig, RunContext
from agent.harness.reins.clarification import (
    ClarificationInterceptor,
    _CLARIFICATION_CONTEXT,
    _CLARIFICATION_DONE,
    _format_answers,
    _shell_build_steps,
    record_clarification_answers,
)
from agent.harness.reins.core import AWAIT_APPROVAL_KEY


@pytest.fixture(autouse=True)
def _clear_store():
    _CLARIFICATION_DONE.clear()
    _CLARIFICATION_CONTEXT.clear()
    yield
    _CLARIFICATION_DONE.clear()
    _CLARIFICATION_CONTEXT.clear()


def _ctx(tool: str, args: dict, base: RunContext | None = None) -> RunContext:
    """同一个 run 里换下一次工具调用 —— run_id 必须一致，否则测不到「同 run 不同工具」"""
    ctx = base or RunContext.new(task="t", config=AgentConfig())
    ctx.metadata[AWAIT_APPROVAL_KEY] = True
    ctx.tool_calls_log = [{"tool": tool, "args": args, "iteration": 0, "success": None}]
    return ctx


@pytest.mark.asyncio
async def test_答案就绪后无论下一次调什么都会收到澄清结果且只消费一次():
    it = ClarificationInterceptor()
    ctx = _ctx("shell_exec", {"command": "rm -rf /tmp/*"})
    assert (await it.check(ctx)).intercepted is True

    record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}})

    # 等待期模型转去调别的工具是常态路径（文案明写「不要重试同一工具」）。
    # 新语义：无论下一次是什么调用，都先作废并强制注入澄清结果 —— 否则模型
    # 永远收不到「用户已回答」的任何信息。
    other = _ctx("job_run", {"cmd": "chmod -R 777 *"}, base=ctx)
    d = await it.check(other)
    assert d.intercepted is True, "答案就绪后的首次调用必须被作废（答案不是执行许可）"
    assert [e.kind for e in d.events] == ["clarification_done"]
    assert any(
        "[用户澄清结果]" in (m.get("content") or "") for m in d.inject_messages
    ), "澄清结果必须注入给模型（无论它转向哪个工具）"
    assert ctx.run_id not in _CLARIFICATION_DONE, "答案被消费一次后不得残留"
    assert ctx.run_id not in _CLARIFICATION_CONTEXT, "向导上下文必须随消费清空"

    # 已消费的答案不得再次触发作废（此例中同一模糊命令会另起一张新向导，
    # 那是新一轮澄清，与本次答案无关）
    again = _ctx("shell_exec", {"command": "rm -rf /tmp/*"}, base=ctx)
    d2 = await it.check(again)
    assert all(e.kind != "clarification_done" for e in d2.events), (
        "答案已消费，不得再次触发作废")


def test_注入文本必须带出问题原文与选项文字():
    steps = _shell_build_steps({"command": "rm -rf /tmp/*"})
    text = _format_answers({"0": {"choice": "A"}, "1": {"choice": "B"}}, steps)
    assert "作用范围" in text, f"没带出问题原文，模型不知道 A 在回答什么：{text}"
    assert "仅当前目录一层" in text, f"没带出选项文字：{text}"
    assert "Q1" in text and "A." in text


def test_自定义补充不再只在_choice_custom_时出现():
    """用户在选了字母的同时补了自定义路径，两半都得回灌（原来只在 choice=='custom' 才带）"""
    steps = _shell_build_steps({"command": "rm -rf /tmp/*"})
    text = _format_answers({"0": {"choice": "C", "custom_text": "只删 /tmp/*/access.log"}}, steps)
    assert "只删 /tmp/*/access.log" in text


def test_steps_缺失时降级为字母而不抛():
    """历史调用方与桩答案都没有 steps：宁可信息少，也不能让注入路径炸掉。"""
    assert "Q1" in _format_answers({"0": {"choice": "Z"}}, [])
    assert "Z" in _format_answers({"0": {"choice": "Z"}}, None)
    assert "跳过" in _format_answers({}, [])


def test_dict_形态的_steps_也能还原选项():
    """ctx 里存过 ClarificationStep，路由/测试侧传的是 dict —— 两种都得认。"""
    steps = [{"question": "作用范围是？", "options": [{"id": "A", "label": "只当前目录"}]}]
    text = _format_answers({"0": {"choice": "A"}}, steps)
    assert "只当前目录" in text and "作用范围" in text
