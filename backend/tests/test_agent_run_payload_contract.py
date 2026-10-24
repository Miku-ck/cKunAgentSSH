"""`/agent/run` 的实发键 ⇄ `AgentRunRequest` 字段（跨层键漂移防线，续 round 22 的思路）。

round 22 的 `test_settings_key_contract.py` 只覆盖设置面的 8 个方法；
**运行请求**这条链上一次都没被钉过，而它恰恰是本仓最容易说谎的接缝：
`src/api/sse.ts` 里 payload 是一个手写的内联对象（camelCase 选项 → snake_case
 wire 键一个个手抄），pydantic 默认**静默丢弃**未声明字段 —— 前端多发一个键、
 或后端改了名，两边自己的测试都还是绿的，只是那一项永远进不去。
 本仓已经付过学费的实例：`reasoning_tokens` / `is_default_ssh` 就是这么丢的，
 而 `enable_thinking` 的「没表态」与「显式关」也是靠这条链上的字段形状区分的。

方向不对称，与 round 22 同口径：
1) 前端实发的每个 wire 键都必须在后端模型里（否则被静默丢弃 = 真事故）；
2) 后端的**必填**键前端必须都发（否则恒 422 = 真事故）；
   后端多出来的可选键不算错 —— 那是这一版 UI 没暴露，不是失配。

每条守卫都带变异自检：解析器也是代码，抽取集合为空时断言会「因看不见而通过」，
那比没有测试更糟（round 22 记过一次）。

运行: python -m pytest backend/tests/test_agent_run_payload_contract.py -v
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_source import SOURCE_LABEL, frontend_api_source  # noqa: E402


def _balanced(text: str, start: int) -> str:
    """从 text[start]（必须是 '{'）取到配对 '}'。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("未闭合的 {}")


def _sent_wire_keys(block: str) -> set[str]:
    """从 payload 字面量里取 wire 键。

    条件展开（`...(x === undefined ? {} : { enable_thinking: x })`）里的键同样是
    wire 键，所以这里按「行首的 `ident:`」收，而不是把嵌套整块剥掉 —— 剥掉会让
    enable_thinking 这类只在分支里出现的键从集合里消失，方向 2 的自检就抓不到它。
    值表达式里的 `a: b` 不存在（TS 对象字面量），所以这个宽松法不会误收。
    """
    keys = set()
    for line in block.splitlines():
        m = re.match(r"^\s*\.{3}?\s*\(?\s*\{?\s*([a-z][a-z0-9_]*)\s*:", line)
        if m:
            keys.add(m.group(1))
        m2 = re.match(r"^\s*([a-z][a-z0-9_]*)\s*:", line)
        if m2:
            keys.add(m2.group(1))
    return keys


@pytest.fixture(scope="module")
def payload_block() -> str:
    src = frontend_api_source()
    # 必须先从 streamAgentRun 切段：`body: JSON.stringify({` 在前端 api 里不唯一
    # （会话改标题那处也是同样的写法），从全文找第一个命中会抽到一个 15 字节的
    # `{ title, mode }`，于是整条守卫抽出 0 个键还能「跑得绿」。
    fn_at = src.find("export function streamAgentRun")
    assert fn_at >= 0, f"{SOURCE_LABEL} 里找不到 streamAgentRun —— 解析前提已失效"
    body = src[fn_at:]
    marker = "body: JSON.stringify({"
    at = body.find(marker)
    assert at >= 0, "streamAgentRun 里找不到 body: JSON.stringify({ —— 解析前提已失效"
    return _balanced(body, at + len(marker) - 1)


@pytest.fixture(scope="module")
def sent_keys(payload_block: str) -> set[str]:
    keys = _sent_wire_keys(payload_block)
    # 空集合 = 守卫恒真；这里先钉住「真的抽到了东西」
    assert len(keys) >= 10, f"只抽出 {len(keys)} 个 wire 键，解析器八成失效了"
    return keys


@pytest.fixture(scope="module")
def request_model():
    from routes.agent import AgentRunRequest

    return AgentRunRequest


def test_frontend_sends_only_declared_fields(sent_keys, request_model):
    undeclared = sent_keys - set(request_model.model_fields)
    assert not undeclared, (
        f"这些键会被 pydantic 静默丢弃（前端以为生效、后端从没读过）: {sorted(undeclared)}"
    )


def test_backend_required_fields_are_all_sent(sent_keys, request_model):
    required = {
        name
        for name, f in request_model.model_fields.items()
        if f.is_required()
    }
    missing = required - sent_keys
    assert not missing, f"后端必填但前端不发，请求会恒 422: {sorted(missing)}"


def test_guard_actually_catches_renamed_key(sent_keys, request_model):
    """变异自检：把一个真实存在的 wire 键改名，必须产出一条「未被声明」的差集。"""
    renamed = set(sent_keys)
    victim = "conversation_id"
    assert victim in renamed, "conversation_id 不在抽取集合里，说明解析器已经看不见这条链"
    renamed.discard(victim)
    renamed.add("conversation_id_typo")
    undeclared = renamed - set(request_model.model_fields)
    assert undeclared == {"conversation_id_typo"}, (
        f"改名没有被抓住，实际差集: {sorted(undeclared)}"
    )


def test_guard_actually_catches_dropped_required_key(sent_keys, request_model):
    """变异自检：少发一个必填键，必须产出一条 422 方向的缺失。"""
    dropped = set(sent_keys)
    dropped.discard("conversation_id")
    required = {n for n, f in request_model.model_fields.items() if f.is_required()}
    missing = required - dropped
    assert missing == {"conversation_id"}, f"漏发必填没有被抓住，实际缺失: {sorted(missing)}"
