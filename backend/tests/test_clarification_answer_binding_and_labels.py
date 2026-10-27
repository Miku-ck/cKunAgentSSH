"""澄清回路的两个真缺陷：答案无主消费 + 注入文本模型读不懂。

审查里那条「tool_call_id 存了却从不比对」的说法，机制讲错了（答案只从用户 POST
进来，存的是合成 uuid，跟模型那次 tool call 的 id 无关），但**缺陷是真的**，形态不同：

1. 消费只看 run_id（原 `if run_id in _CLARIFICATION_DONE`）。答完之后紧接着的第一次
   PRE_TOOL_CALL —— 不管是不是当初问的那个工具 —— 把答案消费掉并 pop 向导上下文。
   而等待期注入给模型的文案明写「请不要重试调用同一工具」，模型转去调别的工具是
   **常态路径**：于是澄清被花在无关操作上（注入文本还接着说「请按上述澄清范围重新
   执行操作」），真正触发澄清的那次调用再进来时无据可依，还会重新问一遍。
2. `_format_answers` 只输出字母。选项文字只存在于 `clarification_needed` 的 SSE
   payload（前端本地推进），模型侧从头到尾只收到过「一个澄清问题正在等待用户回答」
   —— 注入 "  Q1: A" / "  Q2: B" 等于把用户的答案压缩成模型无法解读的字母，
   整条澄清回路对模型不产生任何可用信息。

反向也要钉：同一个工具回来时消费必须照常发生、且只发生一次（现有链路 3 用例覆盖的是
同工具场景，这里补「无关调用不许消费」这一半）。

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
async def test_无关工具调用不得消费别人的澄清答案():
    it = ClarificationInterceptor()
    ctx = _ctx("shell_exec", {"command": "rm -rf /tmp/*"})
    assert (await it.check(ctx)).intercepted is True

    record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}})

    # job_run 复用 shell 的模糊检测器：它既会想开第二张向导，又会顺手消费第一张的答案
    other = _ctx("job_run", {"cmd": "chmod -R 777 *"}, base=ctx)
    d = await it.check(other)
    assert d.intercepted is False, "无关调用被拦：应该只做「不还答案」"
    assert d.events == [], "无关调用不该发 clarification_done"
    assert d.inject_messages == [], "无关调用不该注入「请按上述澄清范围重新执行操作」"
    assert ctx.run_id in _CLARIFICATION_DONE, "答案被无关调用花掉了"
    assert _CLARIFICATION_CONTEXT[ctx.run_id]["tool"] == "shell_exec", (
        "向导上下文被第二张题覆盖 —— 用户已经答过的问题就此无主")

    # 答案仍在那儿等它问的那次调用
    again = _ctx("shell_exec", {"command": "rm -rf /tmp/*"}, base=ctx)
    d2 = await it.check(again)
    assert d2.intercepted is False
    assert [e.kind for e in d2.events] == ["clarification_done"]
    assert ctx.run_id not in _CLARIFICATION_DONE
    assert ctx.run_id not in _CLARIFICATION_CONTEXT


def test_注入文本必须带出问题原文与选项文字():
    steps = _shell_build_steps({"command": "rm -rf /tmp/*"})
    text = _format_answers({"0": {"choice": "A"}, "1": {"choice": "B"}}, steps)
    assert "作用范围" in text, f"没带出问题原文，模型不知道 A 在回答什么：{text}"
    assert "仅通配符所在目录一层" in text, f"没带出选项文字：{text}"
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
