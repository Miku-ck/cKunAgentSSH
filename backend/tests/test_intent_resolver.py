"""意图级联解析器（Phase 1）测试。

覆盖三层：
1. classify 的 ambiguous 标记（同分对峙 / 零信号 / decisive 不升级）；
2. resolve_intention 的 LLM 校准合并 / 失败降级 / 开关；
3. 任务级澄清回路（注册 → 答案 → 二次校准合并 / 跳过放行 / 无人值守不等待）。

背景：主流生产做法是「规则快路径 + 低置信升级 LLM 校准 + 仍模糊则对比式澄清」，
本模块把置信度从"只进日志"变成级联开关；ambiguous 判定与澄清回路是两个
不可回退的性质 —— 改词典/改判定口径前先看这里的用例。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.intention import (
    IntentionLevel,
    TaskType,
    compose_intention_result,
    classify,
)
from agent.harness.reins.core import AWAIT_APPROVAL_KEY
from agent.harness.reins.clarification import (
    _CLARIFICATION_CONTEXT,
    _CLARIFICATION_DONE,
    TASK_CLARIFICATION_TOOL,
    record_clarification_answers,
    release_clarification_run,
)
from agent import intent_resolver
from agent.intent_resolver import resolve_intention


# ── 1. classify 的 ambiguous 标记 ────────────────────────────────────────────

@pytest.mark.parametrize("task", [
    # 同分对峙：评估(research) vs 重构(refactor) 各 1 分 —— 首现位置仲裁能选边，
    # 但选边是启发式，必须标记 ambiguous 交给 LLM 校准裁决
    "评估这次重构的收益",
    "分析这个重构方案的改动量",
    # 零信号长文本（≥12 字、无任何词典命中）—— 词典永远列不全事故的表述方式
    "帮我把那个服务弄成之前的配置",
    "上次那个东西现在到底是个什么状态啊",
])
def test_ambiguous_tasks_flagged(task):
    assert classify(task).ambiguous is True


@pytest.mark.parametrize("task", [
    # decisive：命中分出高下 / 单类型命中 / 过短 → 不升级，零开销
    "重启 nginx",
    "查看系统负载",
    "重构重写这个模块顺便分析影响",   # refactor 2:1 research
    "看看这台机器上次那个问题好了没有啊",  # 仅 query 命中，无对峙
])
def test_decisive_or_single_hit_not_ambiguous(task):
    assert classify(task).ambiguous is False


def test_short_zero_signal_not_ambiguous():
    """过短零信号文本（<12 字）不值得一次 LLM 往返，保持现状"""
    assert classify("帮我搞一下").ambiguous is False


def test_conversation_never_ambiguous():
    """对话快速路径置信 0.95 直接返回，永不标记"""
    assert classify("你好").ambiguous is False
    assert classify("你是什么模型").ambiguous is False


def test_to_dict_includes_new_fields():
    d = classify("重启 nginx").to_dict()
    assert d["ambiguous"] is False
    assert d["source"] == "rules"


# ── 2. compose_intention_result（两条路径共用的组装器）───────────────────────

def test_compose_clamps_budget_to_model_max():
    """固定档位预算必须被模型 reasoning 上限钳住（超限 → 下轮 LLM 400）"""
    r = compose_intention_result(
        level=IntentionLevel.MODERATE, task_type=TaskType.IMPLEMENT,
        is_change=True, confidence=0.8, max_budget=1024)
    assert r.reasoning_budget is not None
    assert r.reasoning_budget <= 1024


def test_compose_planning_gate():
    r = compose_intention_result(
        level=IntentionLevel.COMPLEX, task_type=TaskType.REFACTOR,
        is_change=True, confidence=0.9)
    assert r.allow_planning_phase is True
    r2 = compose_intention_result(
        level=IntentionLevel.DEBUG, task_type=TaskType.DEBUG,
        is_change=False, confidence=0.85)
    assert r2.allow_planning_phase is False


# ── 3. resolve_intention：校准合并 / 降级 / 开关 ─────────────────────────────

def _fake_ctx(task: str, run_id: str = "t000000000001",
              await_approval: bool = False) -> SimpleNamespace:
    events: list = []
    metadata: dict = {}
    if await_approval:
        metadata[AWAIT_APPROVAL_KEY] = True

    async def emit_event(ev):
        events.append(ev)

    metadata["emit_event"] = emit_event
    return SimpleNamespace(
        run_id=run_id, task=task, messages=[], metadata=metadata,
        llm_base_url="http://mock:1/v1", llm_api_key="EMPTY",
        llm_model="mock-model", events=events,
    )


_AMBIGUOUS_TASK = "评估这次重构的收益"


@pytest.mark.asyncio
async def test_calibration_merges_llm_fields(monkeypatch):
    async def fake_cal(task, base_url, api_key, model, cache_extra=""):
        return {"task_type": "debug", "level": "debug", "is_change": False,
                "needs_clarification": False, "reason": "test"}
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", fake_cal)
    ctx = _fake_ctx(_AMBIGUOUS_TASK)
    r = await resolve_intention(ctx, max_budget=None)
    assert r.source == "llm"
    assert r.level is IntentionLevel.DEBUG
    assert r.task_type is TaskType.DEBUG
    assert r.is_change is False
    assert r.ambiguous is False
    assert ctx.events == []  # 无澄清事件


@pytest.mark.asyncio
async def test_calibration_failure_falls_back_to_rules(monkeypatch):
    async def fake_cal(task, base_url, api_key, model, cache_extra=""):
        return None
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", fake_cal)
    ctx = _fake_ctx(_AMBIGUOUS_TASK)
    base = classify(ctx.task)
    r = await resolve_intention(ctx, max_budget=None)
    assert r.source == "rules"
    assert r.task_type is base.task_type


@pytest.mark.asyncio
async def test_calibration_disabled_via_env(monkeypatch):
    monkeypatch.setenv("CKUN_INTENT_LLM_CALIBRATION", "0")
    called = {"n": 0}

    async def fake_cal(*a, **kw):
        called["n"] += 1
        return None
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", fake_cal)
    ctx = _fake_ctx(_AMBIGUOUS_TASK)
    await resolve_intention(ctx, max_budget=None)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_conversation_guard_keeps_action_tools(monkeypatch):
    """校准说 conversation 但规则底判是动作类型 → 不采纳（防工具表被清空）"""
    async def fake_cal(task, base_url, api_key, model, cache_extra=""):
        return {"task_type": "conversation", "level": "trivial",
                "is_change": False, "needs_clarification": False}
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", fake_cal)
    ctx = _fake_ctx(_AMBIGUOUS_TASK)
    r = await resolve_intention(ctx, max_budget=None)
    assert r.task_type is TaskType.RESEARCH  # 底判 research，未被 conversation 覆盖


@pytest.mark.asyncio
async def test_invalid_calibration_fields_fall_back_per_field(monkeypatch):
    async def fake_cal(task, base_url, api_key, model, cache_extra=""):
        return {"task_type": "bogus", "level": "nonsense",
                "is_change": True, "needs_clarification": False}
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", fake_cal)
    ctx = _fake_ctx(_AMBIGUOUS_TASK)
    base = classify(ctx.task)
    r = await resolve_intention(ctx, max_budget=None)
    assert r.task_type is base.task_type   # 非法值回落
    assert r.level is base.level
    assert r.is_change is True             # 合法字段采纳


# ── 4. 任务级澄清回路 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_clarification_roundtrip(monkeypatch):
    """需要澄清 → 发事件 + 等答案 → 二次校准合并 + 注入消息"""
    calls = {"n": 0}

    async def fake_cal(task, base_url, api_key, model, cache_extra=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"task_type": "query", "level": "trivial", "is_change": False,
                    "needs_clarification": True,
                    "question": "你要评估收益还是执行重构？",
                    "options": ["只评估收益，不改代码", "直接执行重构"]}
        return {"task_type": "implement", "level": "moderate", "is_change": True,
                "needs_clarification": False}
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", fake_cal)

    ctx = _fake_ctx(_AMBIGUOUS_TASK, await_approval=True)
    # 预录答案（register 发生在 resolve 内部，DONE 先就位 → wait 立即返回）
    record_clarification_answers(ctx.run_id, {"0": {"choice": "B"}})
    try:
        r = await resolve_intention(ctx, max_budget=None)
    finally:
        release_clarification_run(ctx.run_id)

    kinds = [e.kind for e in ctx.events]
    assert "clarification_needed" in kinds
    assert "clarification_done" in kinds
    needed = next(e for e in ctx.events if e.kind == "clarification_needed")
    assert needed.payload["tool"] == TASK_CLARIFICATION_TOOL
    assert needed.payload["steps"][0]["question"] == "你要评估收益还是执行重构？"
    # 二次校准合并生效 + 澄清结论注入模型消息
    assert r.source == "llm"
    assert r.level is IntentionLevel.MODERATE
    assert any("[用户澄清结果]" in m.get("content", "") for m in ctx.messages)
    # 无残留
    assert ctx.run_id not in _CLARIFICATION_CONTEXT
    assert ctx.run_id not in _CLARIFICATION_DONE


@pytest.mark.asyncio
async def test_task_clarification_skip_falls_back_to_rules(monkeypatch):
    """用户跳过（空答案）→ 无注入消息，按规则结果放行"""
    async def fake_cal(task, base_url, api_key, model, cache_extra=""):
        if cache_extra == "":
            return {"task_type": "query", "level": "trivial", "is_change": False,
                    "needs_clarification": True,
                    "question": "q", "options": ["a"]}
        raise AssertionError("跳过后不应发起二次校准")
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", fake_cal)

    ctx = _fake_ctx(_AMBIGUOUS_TASK, await_approval=True)
    record_clarification_answers(ctx.run_id, {})  # skip = 空 dict
    try:
        r = await resolve_intention(ctx, max_budget=None)
    finally:
        release_clarification_run(ctx.run_id)
    assert r.source == "rules"
    assert ctx.messages == []
    assert "clarification_done" in [e.kind for e in ctx.events]


@pytest.mark.asyncio
async def test_unattended_run_never_clarifies(monkeypatch):
    """无人值守（定时任务）：只采纳校准字段，永不发澄清事件"""
    async def fake_cal(task, base_url, api_key, model, cache_extra=""):
        return {"task_type": "query", "level": "trivial", "is_change": False,
                "needs_clarification": True,
                "question": "q", "options": ["a"]}
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", fake_cal)

    ctx = _fake_ctx(_AMBIGUOUS_TASK, await_approval=False)
    r = await resolve_intention(ctx, max_budget=None)
    assert ctx.events == []          # 无澄清事件
    assert r.source == "llm"         # 校准字段仍被采纳
    assert ctx.messages == []        # 不等待、不注入


@pytest.mark.asyncio
async def test_no_llm_config_skips_calibration():
    """无 LLM 配置（如裸 RunContext 的单测场景）→ 直接返回规则结果"""
    ctx = _fake_ctx(_AMBIGUOUS_TASK)
    ctx.llm_base_url = ""
    base = classify(ctx.task)
    r = await resolve_intention(ctx, max_budget=None)
    assert r.source == "rules"
    assert r.task_type is base.task_type


@pytest.mark.asyncio
async def test_resolve_never_raises_on_internal_error(monkeypatch):
    """级联内部任何异常都降级为规则结果 —— 意图识别不许拖死 run"""
    async def boom(*a, **kw):
        raise RuntimeError("mock explosion")
    monkeypatch.setattr(intent_resolver, "_calibrate_via_llm", boom)
    ctx = _fake_ctx(_AMBIGUOUS_TASK)
    base = classify(ctx.task)
    r = await resolve_intention(ctx, max_budget=None)
    assert r.source == "rules"
    assert r.task_type is base.task_type


# ── 5. 事故回归语料（来自 intention.py 注释里的线上事故，锁定不可回退）────────

@pytest.mark.parametrize("task", [
    "在吗，服务器挂了",                      # 事故：对话快速路径吞掉故障上报
    "hi list the processes on web-01",      # 事故：问候 + 查询动词
    "你好，帮我查看 /var/log/messages",       # 事故：问候 + 显式路径
    "调整任务优先级",                        # 事故：单字「先」误命中导致多步升级
    "查看整个目录的日志",                    # 事故：复杂度信号误抬只读查询
    "等 500ms",                             # 事故：数字兜底不可吞纯指令
])
def test_incident_regressions(task):
    r = classify(task)
    assert r.task_type is not TaskType.CONVERSATION, (
        f"{task!r} 被判成纯对话会物理清空工具表"
    )
