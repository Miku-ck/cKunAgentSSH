"""意图级联解析器（Phase 1）— 规则分类 → 低置信 LLM 校准 → 任务级澄清。

对齐主流生产做法的三层级联（Router Latency Paradox：路由器自身必须低延迟，
LLM 只为规则裁决不了的少数 case 付费）：

  classify（本地规则，µs 级）
    ├─ 非 ambiguous → 直接返回（绝大多数 run，零行为变化、零额外开销）
    └─ ambiguous（多类型同分对峙 / 零信号长文本，见 intention.classify 1.5 节）
        → _calibrate_via_llm：1 次结构化小输出调用（禁 thinking，8s 超时，
          按 (task, model) LRU 缓存；任何失败降级回规则结果）
            ├─ needs_clarification=false → 合并校准字段返回（source="llm"）
            └─ needs_clarification=true → 任务级澄清：
                复用澄清向导的 pending 存储 / SSE 契约 / 前端 ClarifyingWizard，
                等答案后携带澄清上下文二次校准；再失败 → 按增强文本重跑规则。

调用点：driver.run 的意图识别块（原 classify_intention 直连处）。
约束：
- 本模块内部吞掉一切异常并降级，绝不向上抛 —— 意图识别失败不能拖死 run。
- 无人值守入口（定时任务，AWAIT_APPROVAL_KEY 未置位）只做 LLM 校准，
  永不等待澄清 —— 没有人可以答题。
- 校准输出的 CONVERSATION 仅在规则底判为 QUERY（弱信号）时才采纳，
  防止一次误判把带动作信号的 run 的工具表物理清空（CONVERSATION 门控
  的"宁滥勿缺"教训，见 intention._DEBUG_MARKERS 注释）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any, Optional

from agent.intention import (
    IntentionLevel,
    TaskType,
    classify,
    compose_intention_result,
)
from agent.harness.contracts import (
    ClarificationOption,
    ClarificationStep,
    TraceEvent,
)
from agent.harness.reins.clarification import (
    _CLARIFICATION_CONTEXT,
    _CLARIFICATION_DONE,
    _CLARIFICATION_TTL_SEC,
    TASK_CLARIFICATION_TOOL,
    _build_clarification_needed,
    _format_answers,
    pop_task_clarification,
    register_task_clarification,
)
from agent.harness.reins.core import AWAIT_APPROVAL_KEY

logger = logging.getLogger(__name__)

# LLM 校准单次调用上限：意图分类是几百 token 的活，模型啰嗦也该掐住
_CALIBRATION_TIMEOUT_S = 8.0
_CALIBRATION_MAX_TOKENS = 220
_CALIBRATION_TEMPERATURE = 0.1
# 校准结果缓存：同一任务文本（+模型）重复 run 不重复付费（典型：重试、定时任务）
_CACHE_MAX_ENTRIES = 128
_calibration_cache: "OrderedDict[tuple, dict]" = OrderedDict()

_TASK_CLARIFICATION_REASON = "任务目标存在多种合理理解，执行前需要你确认"

_SYSTEM_PROMPT = (
    "你是运维 Agent 的意图分类器。分析用户任务文本，只输出一个 JSON 对象，"
    "不要输出任何其他文字：\n"
    '{"task_type": "query|implement|modify|refactor|debug|research|conversation",\n'
    ' "level": "trivial|simple|moderate|complex|debug",\n'
    ' "is_change": true或false,\n'
    ' "needs_clarification": true或false,\n'
    ' "question": "needs_clarification=true 时要问用户的单个澄清问题",\n'
    ' "options": ["选项A", "选项B", "选项C"],\n'
    ' "reason": "一句话判断依据"}\n'
    "判定口径：\n"
    "- task_type 描述任务性质；is_change 描述是否产生副作用（只读取证一律 false，\n"
    "  只有明确改动文件/服务/配置/代码才是 true）。\n"
    "- level：单步只读=trivial；单步写=simple；多步骤=moderate；复杂规划/重构=complex；\n"
    "  排障=debug（debug 优先于 moderate/complex）。\n"
    "- conversation 仅限问候/身份/能力询问等纯闲聊；带任何动作诉求都不算。\n"
    "- 只有当任务目标确实存在多种合理理解、且理解错误会导致错误执行时，\n"
    "  才 needs_clarification=true（question 用中文，options 给 2-3 个互斥的"
    "候选理解）；能合理推断时不要问。"
)


def _calibration_enabled() -> bool:
    """总开关：默认开（enable_intention_guide 已是第一道闸），CKUN_INTENT_LLM_CALIBRATION=0 关闭。"""
    return os.environ.get("CKUN_INTENT_LLM_CALIBRATION", "1") != "0"


def _parse_calibration_json(raw: str) -> Optional[dict]:
    """从模型输出提取并校验 JSON；任何不合法形态返回 None（调用方降级）。

    容忍 ```json 围栏与前后废话：取首个 {...} 平衡段做 json.loads。
    """
    if not raw or not raw.strip():
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _cache_put(key: tuple, value: dict) -> None:
    _calibration_cache[key] = value
    _calibration_cache.move_to_end(key)
    while len(_calibration_cache) > _CACHE_MAX_ENTRIES:
        _calibration_cache.popitem(last=False)


async def _calibrate_via_llm(
    task: str,
    base_url: str,
    api_key: str,
    model: str,
    cache_extra: str = "",
) -> Optional[dict]:
    """单次结构化 LLM 校准。失败/超时/不可解析一律返回 None（降级规则结果）。"""
    key = (task, base_url, model, cache_extra)
    hit = _calibration_cache.get(key)
    if hit is not None:
        _calibration_cache.move_to_end(key)
        return hit

    from llm_adapter import chat_stream

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    buf: list[str] = []

    async def _call() -> None:
        async for chunk in chat_stream(
            base_url, api_key, model, messages,
            temperature=_CALIBRATION_TEMPERATURE,
            max_tokens=_CALIBRATION_MAX_TOKENS,
            enable_thinking=False,
        ):
            if chunk.content:
                buf.append(chunk.content)

    started = time.monotonic()
    try:
        await asyncio.wait_for(_call(), timeout=_CALIBRATION_TIMEOUT_S)
    except asyncio.CancelledError:
        raise  # run 被取消（用户停止/断连）必须原样上抛，吞掉会变僵尸等待
    except Exception as e:
        logger.warning("意图 LLM 校准失败（降级规则结果）: %s", e)
        return None

    raw = "".join(buf)
    parsed = _parse_calibration_json(raw)
    if parsed is None:
        logger.warning(
            "意图 LLM 校准输出不可解析（降级规则结果）: %.200s", raw)
        return None
    logger.info(
        "意图 LLM 校准成功 (%.2fs): %s",
        time.monotonic() - started,
        {k: parsed.get(k) for k in ("task_type", "level", "is_change",
                                    "needs_clarification")},
    )
    _cache_put(key, parsed)
    return parsed


def _merge_calibration(
    base: Any,
    cal: dict,
    max_budget: Optional[int],
) -> Any:
    """把校准 JSON 合并进 IntentionResult（字段不合法时逐项回落规则值）。"""
    level = base.level
    try:
        level = IntentionLevel(str(cal.get("level", "")))
    except ValueError:
        pass
    task_type = base.task_type
    try:
        tt = TaskType(str(cal.get("task_type", "")))
    except ValueError:
        tt = None
    if tt is not None:
        # CONVERSATION 门控守卫：校准说"纯对话"但规则底判是带动作信号的类型
        # （tie 升级路径底判不会是 QUERY）→ 不采纳，防止工具表被物理清空。
        # 底判 QUERY（弱/零信号）时允许采纳 —— "你觉得我该学 rust 还是 go"
        # 这类零信号文本确实可能是纯咨询。
        if tt == TaskType.CONVERSATION and base.task_type != TaskType.QUERY:
            tt = base.task_type
        task_type = tt
    is_change = bool(cal.get("is_change", base.is_change))
    return compose_intention_result(
        level=level,
        task_type=task_type,
        is_change=is_change,
        confidence=0.8,
        max_budget=max_budget,
        source="llm",
        ambiguous=False,
    )


async def _wait_task_answers(run_id: str) -> Optional[dict]:
    """轮询等待任务级澄清答案。

    返回值三态：
    - dict：用户答案（可能为空 dict = 用户点了跳过）
    - None：TTL 到期 / pending 被回收（用户停止 run、断连、release-run）
    取消（CancelledError）原样上抛，由 driver 的取消链收尾。
    """
    deadline = time.monotonic() + _CLARIFICATION_TTL_SEC
    while time.monotonic() < deadline:
        if run_id in _CLARIFICATION_DONE:
            return _CLARIFICATION_DONE[run_id]
        if run_id not in _CLARIFICATION_CONTEXT:
            # release_clarification_run / TTL 清扫已回收 → 不会有人再答题
            return None
        await asyncio.sleep(0.5)
    return None


async def _run_task_clarification(
    ctx: Any,
    base: Any,
    cal: dict,
    max_budget: Optional[int],
) -> Any:
    """发任务级澄清题 → 等答案 → 带澄清上下文二次校准。

    事件通道用 ctx.metadata["emit_event"]（harness._chunk_queue 直通）：
    本函数阻塞等待期间 driver 生成器被挂起，ReinsDecision.events 搭车
    通道不可用 —— 审批卡片的迟到增益通道正是为这个场景修的。
    """
    emit_event = ctx.metadata.get("emit_event")
    base_url = getattr(ctx, "llm_base_url", "") or ""
    api_key = getattr(ctx, "llm_api_key", "") or ""
    model = getattr(ctx, "llm_model", "") or ""

    question = str(cal.get("question") or "").strip() or "这个任务的目标是什么？请补充说明。"
    labels = [str(o).strip() for o in (cal.get("options") or []) if str(o).strip()][:3]
    options = [ClarificationOption(id=chr(65 + i), label=lbl)
               for i, lbl in enumerate(labels)]
    steps = [ClarificationStep(question=question, options=options, allow_custom=True)]
    task_preview = (ctx.task or "")[:160]

    tool_call_id = register_task_clarification(
        ctx.run_id, steps, _TASK_CLARIFICATION_REASON, task_preview)
    try:
        await emit_event(_build_clarification_needed(
            ctx.run_id, TASK_CLARIFICATION_TOOL, {"command": task_preview},
            steps, 0, len(steps), tool_call_id,
            reason=_TASK_CLARIFICATION_REASON,
        ))
        answers = await _wait_task_answers(ctx.run_id)
    except asyncio.CancelledError:
        # run 被取消：清掉 pending 再上抛，别让孤儿题等 TTL
        pop_task_clarification(ctx.run_id)
        raise

    # 无论答案与否都发 clarification_done（前端向导必须关闭），并清理状态
    ctx_info, ans = pop_task_clarification(ctx.run_id)
    try:
        await emit_event(TraceEvent(
            kind="clarification_done",
            source="intent_resolver",
            run_id=ctx.run_id,
            payload={
                "run_id": ctx.run_id,
                "tool": TASK_CLARIFICATION_TOOL,
                "answers": ans or {},
                "summary": "",
            },
        ))
    except Exception as e:
        logger.warning("clarification_done 事件发送失败（不影响主流程）: %s", e)

    if not ans:
        # 跳过 / 超时 / pending 被回收 → 按规则结果直接放行
        logger.info("任务级澄清未获得答案（run=%s），按规则结果执行", ctx.run_id)
        return base

    summary = _format_answers(ans, (ctx_info or {}).get("steps") or [])
    # 注入为主循环第一条追加消息：模型第一轮就看到澄清结论
    ctx.messages.append({
        "role": "user",
        "content": f"[用户澄清结果]\n{summary}\n请按上述澄清结果执行任务。",
    })
    augmented = f"{ctx.task}\n\n[用户澄清结果]\n{summary}"
    second = await _calibrate_via_llm(
        augmented, base_url, api_key, model, cache_extra="answered")
    if second is not None and not second.get("needs_clarification"):
        return _merge_calibration(base, second, max_budget)
    # 二次校准失败/仍要问 → 用增强文本重跑规则（答案文本自带动作词，
    # "仅报告，列出匹配项" 会把类型拽回 query）
    return classify(augmented, max_budget=max_budget)


async def resolve_intention(
    ctx: Any,
    telemetry: Any = None,
    max_budget: Optional[int] = None,
) -> Any:
    """意图级联主入口（driver 意图识别块调用，替代 classify 直连）。

    本函数承诺不抛异常（CancelledError 除外）：任何一层失败都降级回
    规则结果，意图识别永远不给 run 添堵。
    """
    base = classify(ctx.task, max_budget=max_budget)
    if not base.ambiguous or not _calibration_enabled():
        return base

    base_url = getattr(ctx, "llm_base_url", "") or ""
    api_key = getattr(ctx, "llm_api_key", "") or ""
    model = getattr(ctx, "llm_model", "") or ""
    if not base_url or not model:
        return base

    try:
        cal = await _calibrate_via_llm(ctx.task, base_url, api_key, model)
        if cal is None:
            return base
        if cal.get("needs_clarification"):
            interactive = bool(ctx.metadata.get(AWAIT_APPROVAL_KEY))
            emit_event = ctx.metadata.get("emit_event")
            if not (interactive and callable(emit_event)):
                # 无人值守 / 无事件通道：没人可以答题，只采纳校准字段
                logger.info(
                    "意图校准要求澄清但 run=%s 无人值守，跳过澄清", ctx.run_id)
                return _merge_calibration(base, cal, max_budget)
            return await _run_task_clarification(ctx, base, cal, max_budget)
        return _merge_calibration(base, cal, max_budget)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("意图级联异常（降级规则结果）: %s", e, exc_info=True)
        return base
