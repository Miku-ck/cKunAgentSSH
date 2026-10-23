"""
Driver — 驭手

ReAct 决策循环的执行者 — THINK → ACT → OBSERVE → THINK → ... → COMPLETED/FAILED/MAX_REACHED

与原 engine.py 的区别：
- 不再持有所有子模块，通过 Harness 注入
- 通过 Reins 检查策略（统一 hooks/permissions/self_monitor）
- 通过 Saddle 执行工具（含沙箱 + Undo）
- 通过 Stirrups 调 LLM（含重试 + 压缩触发）
- 通过 Bridle 组装输入
- 通过 TelemetryBus 发事件
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from agent.remote_protocol import EXIT_CODE_PREFIX
# 审查修复：热路径 import 提到模块级——原实现每轮迭代 from...import 跑 4 次
# （classify / reclassify / get_guide_for_iteration / detect_batch_reading），
# Python 缓存模块但 attribute lookup 仍每轮跑一次。循环外提一次即可。
# Phase 1 起 classify 不再被 driver 直连（走 intent_resolver.resolve_intention 级联）。
from agent.intention import (
    TaskType,
    detect_batch_reading,
    get_guide_for_iteration,
    get_progressive_gathering_guide,
    get_reasoning_budget_for_iteration,
    reclassify,
)
from agent.intent_resolver import resolve_intention

from .contracts import (
    FILE_MUTATION_TOOLS,
    LLMOutput,
    READONLY_TOOLS,
    ReinDecision,
    ReinPoint,
    RunContext,
    State,
    ToolCall,
    ToolResult,
    TraceEvent,
)
from .command_explain import explain_compound, explain_for_reason, maybe_explain_via_llm
from .readonly_policy import is_provably_readonly
from .reins import approval_wait
from .reins.core import (
    AWAIT_APPROVAL_KEY,
    _audit_environment,
    approval_fallback_seconds,
    tool_call_signature,
)
from .safety import classify_command
from .telemetry import TelemetryBus

logger = logging.getLogger(__name__)


class Driver:
    """
    驭手 — ReAct 决策循环的执行者

    通过 Harness 拿到所有组件引用，不直接持有。
    """

    async def drive(
        self,
        ctx: RunContext,
        harness: "Harness",
    ) -> AsyncIterator[TraceEvent]:
        """
        主循环 — yield TraceEvent

        事件类型（与原 SSE 协议兼容）：
        - state / content / reasoning / tool_call_start / tool_call_result
        - warning / error / human_help_needed / done
        - intention（新增：意图识别结果，首轮发出供前端展示）
        """
        telemetry = harness.telemetry
        tools_schema = harness.saddle.schemas() if ctx.config.enable_tools else []

        # C-review M8 修复：once 授权在消费时移除（check_permission 计数递减），
        # run 终态由 ReflectionRein.cleanup_run_once 回收。此处只防御性清理
        # 本 run_id 的残留池（上次进程崩溃时 AGENT_COMPLETE 未触发的情形）。
        # 原先调用全局 clear_once() 会误清并发会话在途 run 的 once 授权 —
        # once 池已按 run_id 键控，跨 run 泄漏已由键控本身消除。
        try:
            from .reins import _get_global_permission_gate_sync
            _get_global_permission_gate_sync().cleanup_run_once(ctx.run_id)
        except Exception as e:
            logger.warning("cleanup_run_once 失败: %s", e)

        # ── agent_start ──
        await harness.reins.check(ReinPoint.AGENT_START, ctx)

        # ── 意图识别（在 agent_start 后、主循环前）──
        # 根据 task 文本推断复杂度等级，设置 max_reasoning_tokens + allow_planning_phase
        # 后续每轮 LLM 调用前会基于 iteration 动态调整预算并注入引导语
        # 模型配置的 reasoning_tokens 作为上限，意图识别的预算不能超过该值
        intention = None
        model_max_reasoning = ctx.config.max_reasoning_tokens  # 保存模型配置的上限
        if ctx.config.enable_intention_guide:
            try:
                # Phase 1 意图级联：规则分类 → 低置信 LLM 校准 → 任务级澄清。
                # 高置信路径（绝大多数 run）内部直接返回规则结果，零额外开销；
                # 校准/澄清的任何失败都由 resolve_intention 内部降级回规则结果。
                # 外层 try 保留为最后防线（预算钳制等后续步骤的意外）。
                intention = await resolve_intention(
                    ctx, telemetry=telemetry, max_budget=model_max_reasoning)
                # 意图识别的预算不能超过模型配置的上限
                if intention.reasoning_budget is not None and model_max_reasoning is not None:
                    ctx.config.max_reasoning_tokens = min(intention.reasoning_budget, model_max_reasoning)
                elif model_max_reasoning is not None:
                    ctx.config.max_reasoning_tokens = model_max_reasoning
                else:
                    ctx.config.max_reasoning_tokens = intention.reasoning_budget
                ctx.config.allow_planning_phase = intention.allow_planning_phase
                ctx.metadata["intention"] = intention.to_dict()
                logger.info(
                    "意图识别 (run_id=%s): %s",
                    ctx.run_id, intention.to_dict(),
                )
                # 发 trace 事件让前端可见（TraceViewer 可展示）
                await telemetry.emit(TraceEvent(
                    kind="intention",
                    source="driver",
                    run_id=ctx.run_id,
                    iteration=0,
                    payload=intention.to_dict(),
                ))

                # 根据意图动态调整可用工具列表（如 CONVERSATION 禁用工具）
                tools_schema = self._filter_tools_by_intention(tools_schema, intention)
            except Exception as e:
                logger.warning("意图识别失败（降级为无引导）: %s", e)
                intention = None

        # ── INIT → THINKING ──
        await ctx.transition(State.THINKING, event="skip_plan")
        await telemetry.emit(TraceEvent(
            kind="state",
            source="driver",
            run_id=ctx.run_id,
            iteration=0,
            payload={"state": "thinking"},
        ))

        full_content = ""
        full_reasoning = ""
        iteration = -1  # 默认值，防止 max_iterations=0 时未定义
        max_reached = False
        # 墙钟打点：WallClockRein（POST_ITERATION）按此计算 run 累计时长。
        # monotonic 不受系统时钟跳变影响；setdefault 防御续跑场景已带值。
        ctx.metadata.setdefault("run_started_mono", time.monotonic())

        # while 循环（而非 for range）以支持运行时动态调整 max_iterations
        while iteration + 1 < ctx.config.max_iterations:
            iteration += 1
            ctx.iteration = iteration

            # ── 动态意图重分类（每 reclassify_interval 轮触发）──
            # 根据已积累的工具调用日志、失败率、进度等信号调整意图等级和思考预算
            if (
                intention is not None
                and iteration > 0
                and ctx.config.reclassify_interval > 0
                and iteration % ctx.config.reclassify_interval == 0
            ):
                try:
                    new_intention = reclassify(
                        original=intention,
                        iteration=iteration,
                        tool_calls_log=ctx.tool_calls_log,
                        errors=ctx.errors,
                        max_iterations=ctx.config.max_iterations,
                        max_budget=model_max_reasoning,
                    )
                    if new_intention is not intention:
                        old_level = intention.level.value
                        intention = new_intention
                        ctx.config.max_reasoning_tokens = intention.reasoning_budget
                        ctx.metadata["intention"] = intention.to_dict()
                        logger.info(
                            "意图重分类 (run=%s, iter=%d): %s → %s",
                            ctx.run_id, iteration, old_level, intention.level.value,
                        )
                        # 发 trace 事件让前端可见
                        await telemetry.emit(TraceEvent(
                            kind="intention",
                            source="driver_reclassify",
                            run_id=ctx.run_id,
                            iteration=iteration,
                            payload={
                                **intention.to_dict(),
                                "reclassified": True,
                                "previous_level": old_level,
                            },
                        ))
                except Exception as e:
                    logger.warning("意图重分类失败（保持原意图）: %s", e)

            # 当剩余迭代数不多时，提醒 Agent 准备生成最终总结，
            # 避免把最后一次迭代浪费在工具调用上，导致最终 content 为空。
            remaining = ctx.config.max_iterations - (iteration + 1)
            if remaining <= 2 and not ctx.metadata.get("final_summary_hint_sent"):
                ctx.metadata["final_summary_hint_sent"] = True
                # user 角色（末尾注入的 400 约束，口径见 bridle._build）
                ctx.messages.append({
                    "role": "user",
                    "content": (
                        f"[系统提示] 你还剩 {remaining + 1} 轮迭代机会。"
                        "如果任务尚未完成，请直接在下一轮生成当前进度总结和后续建议，"
                        "不要再调用新工具，避免最终没有输出。"
                    ),
                })

            # ── 强制收敛轮（机制式）：最后一轮物理禁工具 ──
            # 软预警只是劝导，模型仍可能把最后一轮耗在工具调用上。收敛轮把劝导
            # 升级为机制：call_llm 不传 tools（下方调用点），模型物理上无法调工具，
            # 输出必然是文本 → 走收尾分支，终态 COMPLETED（而非 loop-else 的
            # MAX_REACHED）。DynamicBudgetRein 若在 POST_ITERATION 追加了预算，
            # 下一轮 remaining 重算 > 0，收敛自然顺延到新最后一轮；收敛轮无工具
            # 调用，工作密度分不再增长，不会再触发追加。
            forced_convergence = (
                ctx.config.forced_convergence_enabled and remaining == 0
            )
            if forced_convergence:
                # user 角色（末尾注入的 400 约束，口径见 bridle._build）
                ctx.messages.append({"role": "user", "content": (
                    "[系统提示] 本轮是最后一轮，禁止调用任何工具。"
                    "请只输出：1) 已完成事项与关键结果；2) 未完成部分与原因；"
                    "3) 风险与建议的下一步。"
                )})
                # 复用 state kind（不新增事件类型，避开 harness kind 字面量守卫）
                # TelemetryBus.emit 不会把事件带回调用方，必须 yield 才进入 run trace
                converge_ev = TraceEvent(
                    kind="state",
                    source="driver",
                    run_id=ctx.run_id,
                    iteration=iteration,
                    payload={"state": "converging"},
                )
                await telemetry.emit(converge_ev)
                yield converge_ev

            # ── pre_iteration ──
            pre_decision = await harness.reins.check(ReinPoint.PRE_ITERATION, ctx)
            if pre_decision.intercepted:
                async for ev in self._emit_decision_events(telemetry, pre_decision, ctx, iteration):
                    yield ev
                await ctx.transition(State.FAILED, event="pre_iteration_intercepted")
                # H13 修复 B1：触发 AGENT_FAILED ReinPoint，让 ReflectionRein 推送反思事件
                # 原实现直接 break，未触发 AGENT_FAILED，导致失败 run 错过反思，演化数据偏斜
                await harness.reins.check(ReinPoint.AGENT_FAILED, ctx)
                break

            # ── Bridle 组装输入（P1 起 async：溢出压缩时生成式摘要需 await）──
            messages = await harness.bridle.assemble(ctx)
            # 注入 Reins 纠偏消息
            if pre_decision.inject_messages:
                messages.extend(pre_decision.inject_messages)

            # ── 意图引导语注入 + 思考预算动态调整 ──
            # 每轮根据 iteration 注入不同的引导语：
            #   - iteration=0 + allow_planning_phase → 规划阶段引导（首轮长思考，不调工具）
            #   - iteration=0 + 不允许规划 → 直接执行引导
            #   - iteration>0 + complex → 收紧思考预算（< 1024 tokens）
            #   - 其他等级 → 保持原引导
            if intention is not None:
                try:
                    guide = get_guide_for_iteration(intention, iteration)
                    # 动态调整本轮思考预算（首轮保持完整，后续收紧到 1024）
                    # 必须带 model_max_reasoning 钳制：intention.reasoning_budget 是
                    # 未钳制的等级预算，赋给 max_reasoning_tokens 后超限会让下一轮
                    # LLM 调用报 400（classify 路径由上面的 min() 防住，这里曾漏防）
                    ctx.config.max_reasoning_tokens = get_reasoning_budget_for_iteration(
                        intention, iteration, max_budget=model_max_reasoning,
                    )
                    # 引导语走 user 角色（400 约束见 bridle._build）
                    if guide:
                        messages.append({"role": "user", "content": f"[意图引导] {guide}"})
                except Exception as e:
                    logger.warning("意图引导注入失败（跳过）: %s", e)

            # ── pre_llm_call ──
            await harness.reins.check(ReinPoint.PRE_LLM_CALL, ctx)

            # ── THINK: Stirrups 调 LLM ──
            try:
                # 收敛轮传空工具表：物理禁用工具调用（模型侧无法选择 function）
                llm_out = await harness.stirrups.call_llm(
                    ctx, messages, [] if forced_convergence else tools_schema)
            except Exception as e:
                logger.exception("LLM 调用最终失败 (iteration=%d)", iteration)
                await ctx.transition(State.FAILED, event="llm_failed")
                await harness.reins.check(ReinPoint.AGENT_FAILED, ctx)
                yield TraceEvent(
                    kind="error",
                    source="driver",
                    run_id=ctx.run_id,
                    iteration=iteration,
                    payload={"message": f"LLM 调用失败: {e}"},
                )
                async for ev in self._emit_done(
                    telemetry, ctx, iteration + 1, State.FAILED,
                    full_content, full_reasoning, harness,
                ):
                    yield ev
                return

            # ── post_llm_call ──
            await harness.reins.check(ReinPoint.POST_LLM_CALL, ctx)

            if forced_convergence and llm_out.tool_calls:
                # 收敛轮幻觉防御：tools=[] 下模型仍可能凭惯性输出 tool_calls，
                # 物理丢弃按纯文本收尾（空输出由下方 _build_run_summary 兜底）
                logger.warning(
                    "收敛轮丢弃幻觉 tool_calls: %d 个 (run=%s, iteration=%d)",
                    len(llm_out.tool_calls), ctx.run_id, iteration,
                )
                llm_out.tool_calls = []

            full_content += llm_out.content
            full_reasoning += llm_out.reasoning

            # ── 工具调用上限裁剪（架构层硬约束）──
            # 修复（审查 M-12）：裁剪必须先于 assistant 消息入队。
            # 原实现先入队全部 tool_calls 再裁剪，被裁掉的调用永不执行、
            # 也永不产生配对 tool 结果 → 下一轮 LLM 请求因缺配对而 400。
            max_per_turn = ctx.config.max_tool_calls_per_turn
            deferred = []
            if max_per_turn > 0 and llm_out.tool_calls and len(llm_out.tool_calls) > max_per_turn:
                deferred = llm_out.tool_calls[max_per_turn:]
                llm_out.tool_calls = llm_out.tool_calls[:max_per_turn]
                logger.info(
                    "工具调用裁剪: %d → %d (deferred=%d)",
                    max_per_turn + len(deferred),
                    max_per_turn, len(deferred),
                )

            # 记录 assistant 消息（只携带实际将执行的 tool_calls，保证配对完整）
            if llm_out.content or llm_out.tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": llm_out.content or "",
                }
                if llm_out.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": _args_to_json(tc.args)},
                        }
                        for tc in llm_out.tool_calls
                    ]
                ctx.messages.append(assistant_msg)

            # content/reasoning 已由 StreamLLMClient 的 chunk 回调实时 emit 到 TelemetryBus，
            # Driver 这里不再重复 yield 整段内容，避免前端出现"整段突然出来"的观感。

            # ── 判断是否结束 ──
            if not llm_out.tool_calls:
                # goal 验收锚：agent 声称完成（不再调工具）时，先对照成功标准自检一轮。
                # 已有 goal 且本次 run 尚未自检 → 注入自检提示并 continue（下一轮 agent
                # 必须给出「已达成/未达成」结论；未达成则继续）。自检过或本无 goal → 正常收尾。
                goal_text = ctx.metadata.get("_goal_text", "")
                if ctx.conversation_id and not goal_text:
                    # driver 不依赖预注入：收尾时才读一次 goal 原文（文件 I/O 只在
                    # 需要自检的这一刻发生，不进每轮热路径）。读取失败保持空串 → 走原收尾。
                    try:
                        from agent.workflow import read_artifacts
                        goal_text = read_artifacts(ctx.conversation_id)["goal"]
                        ctx.metadata["_goal_text"] = goal_text
                    except Exception:
                        goal_text = ""
                if goal_text and not ctx.metadata.get("_goal_self_checked") \
                        and iteration + 1 < ctx.config.max_iterations:
                    # 仅当还有下一轮预算时才注入自检：continue 回 while 需 iteration+1<max
                    # 成立，否则在最后一轮 continue 会直接落到 loop-else → 误判 MAX_REACHED
                    # 且自检永不被回答（agent 已声明完成，这里应正常收尾为 COMPLETED）。
                    try:
                        from agent.workflow import goal_self_check_prompt
                        self_check = goal_self_check_prompt(goal_text)
                    except Exception:
                        self_check = ""
                    if self_check:
                        # 置位必须在自检真的注入之后：原先写在 try 之前，于是
                        # goal 为纯空白（prompt 返回 ""）或 workflow 导入失败时，
                        # 标记照样落下而自检从未发生 —— done 里的
                        # workflow_goal.self_checked 因此会谎报「已对照成功标准自检」。
                        ctx.metadata["_goal_self_checked"] = True
                        ctx.messages.append({"role": "user", "content": self_check})
                        continue

                # ── 假完成强校验（CompletionGuard）──
                # goal 锚只覆盖有 workflow goal 的 run；无 goal 时「宣告完成」的
                # 唯一凭据是模型自己不调工具了。这里用一次轻量 LLM 裁决把钉扎任务
                # 原文与最终答案对表（详见 _completion_guard docstring）。
                guard_bounce, guard_events = await self._completion_guard(
                    ctx, harness, llm_out.content, iteration,
                    next_round_budget=iteration + 1 < ctx.config.max_iterations,
                    intention_level=getattr(getattr(intention, "level", None), "value", ""),
                )
                for ev in guard_events:
                    await telemetry.emit(ev)
                    yield ev
                if guard_bounce:
                    continue

                # 收敛轮兜底：收敛输出为空时，用工具调用日志生成确定性摘要，
                # 避免用户拿到空结果（口径同 max_reached 分支的 _build_run_summary）
                if forced_convergence and not full_content.strip():
                    summary = _build_run_summary(ctx)
                    if summary:
                        full_content = summary
                        yield TraceEvent(
                            kind="content",
                            source="driver",
                            run_id=ctx.run_id,
                            iteration=max(0, iteration),
                            payload={"content": summary},
                        )
                await ctx.transition(State.COMPLETED, event="done")
                break

            # ── ACT: 执行工具 + OBSERVE → THINK ──
            async for ev in self._execute_tool_calls(llm_out, ctx, harness, telemetry, iteration):
                yield ev

            # C-review M6：工具阶段拦截器要求终止（force_terminated）→ 退出主循环，
            # 走下方 _emit_done（ctx.state 已是 FAILED）
            if ctx.metadata.pop("force_terminated", False):
                break

            # ── 裁剪提示注入（必须在 tool 结果消息之后，保证 assistant.tool_calls
            #    与 role=tool 消息紧邻配对；user 提示插在中间会触发 OpenAI/vLLM 400）──
            if deferred:
                # 注入提示：告诉 LLM 还有工具未执行，下轮继续（角色取 user，见 bridle._build）
                ctx.messages.append({
                    "role": "user",
                    "content": (
                        f"[工具调用纪律] 你本轮请求了 {len(llm_out.tool_calls) + len(deferred)} 个工具调用，"
                        f"但系统限制每轮最多执行 {max_per_turn} 个。"
                        f"本轮只执行前 {max_per_turn} 个，剩余 {len(deferred)} 个未执行。"
                        "请等待当前结果返回后再决定是否需要继续调用剩余工具。"
                        "不要假设未执行工具的结果。"
                    ),
                })

            # ── 渐进式信息收集检测（同样在 tool 结果之后注入）──
            if (
                ctx.config.enable_progressive_gathering
                and intention is not None
            ):
                try:
                    if detect_batch_reading(llm_out.tool_calls, threshold=3):
                        guide = get_progressive_gathering_guide(iteration)
                        # 渐进收集引导走 user 角色（400 约束见 bridle._build）
                        ctx.messages.append({"role": "user", "content": f"[渐进收集] {guide}"})
                        logger.info("检测到批量读取，已注入渐进收集引导")
                except Exception as e:
                    logger.warning("渐进收集检测失败: %s", e)

            # ── post_iteration ──
            post_decision = await harness.reins.check(ReinPoint.POST_ITERATION, ctx)
            # 统一发出拦截器返回的 events（设置 iteration 并推送到 TelemetryBus）
            # 注意：只通过 _emit_decision_events 发送一次，避免重复 human_help_needed 卡片
            async for ev in self._emit_decision_events(telemetry, post_decision, ctx, iteration):
                yield ev
            if post_decision.intercepted:
                # C-review M10：POST_ITERATION 拦截器（成本/token 熔断）要求终止 →
                # transition(FAILED) + AGENT_FAILED + 退出主循环（与 M6 的
                # PRE_TOOL_CALL terminate 同一条终止路径）
                if post_decision.terminate:
                    await ctx.transition(State.FAILED, event="force_terminated")
                    await harness.reins.check(ReinPoint.AGENT_FAILED, ctx)
                    break
                # H14 修复：human_help_needed 是终止信号，应停止运行而不是继续循环
                # 原实现"求助类拦截不终止"导致同一 run 无限次触发求助，产生大量重复卡片
                if any(ev.kind == "human_help_needed" for ev in post_decision.events):
                    await ctx.transition(State.HUMAN_HELP, event="human_help_needed")
                    await harness.reins.check(ReinPoint.AGENT_FAILED, ctx)
                    break
                if post_decision.inject_messages:
                    ctx.messages.extend(post_decision.inject_messages)
            elif post_decision.inject_messages:
                ctx.messages.extend(post_decision.inject_messages)

        else:
            # while 正常耗尽（未 break）→ 达到最大迭代数
            max_reached = True

        if max_reached:
            # 达到最大迭代数
            await ctx.transition(State.MAX_REACHED, event="max_reached")

            # 若运行期间未产生任何正文，基于工具调用日志生成一份摘要，
            # 避免前端显示 "未产生输出" 时用户完全不知道 Agent 做了什么。
            if not full_content.strip():
                summary = _build_run_summary(ctx)
                if summary:
                    full_content = summary
                    yield TraceEvent(
                        kind="content",
                        source="driver",
                        run_id=ctx.run_id,
                        iteration=max(0, iteration),
                        payload={"content": summary},
                    )

            yield TraceEvent(
                kind="warning",
                source="driver",
                run_id=ctx.run_id,
                iteration=max(0, iteration),
                payload={
                    "message": f"达到最大迭代数 {ctx.config.max_iterations}，Agent 终止",
                    "max_iterations": ctx.config.max_iterations,
                    "hint": "可在 Agent 设置中调高 maxIterations，或检查任务是否过复杂",
                },
            )

        # ── agent_complete ──
        # H13 修复 C2：写入 cost_snapshot 到 ctx.metadata
        # 供 ReflectionRein 在 AGENT_COMPLETE/AGENT_FAILED 时读取 has_cost 字段
        if harness.cost_tracker:
            try:
                ctx.metadata["cost_snapshot"] = harness.cost_tracker.snapshot(
                    iterations=max(0, iteration + 1) if ctx.config.max_iterations > 0 else 0
                ).to_dict()
            except Exception as e:
                logger.warning("写入 cost_snapshot 失败: %s", e)
        # FAILED/HUMAN_HELP 终态已在主循环 break 前触发 AGENT_FAILED（含反思 + once 池清理），
        # 此处再触发 AGENT_COMPLETE 会让 ReflectionRein 对同一 run 重复推送 reflection_request，
        # 导致失败运行的反思/演化被执行两次。COMPLETED / MAX_REACHED 未经 AGENT_FAILED，仍需在此反思。
        if ctx.state not in (State.FAILED, State.HUMAN_HELP):
            await harness.reins.check(ReinPoint.AGENT_COMPLETE, ctx)

        # ── done ──
        # iteration=-1 表示循环未执行（max_iterations=0），此时 iterations=0
        iterations_done = max(0, iteration + 1) if ctx.config.max_iterations > 0 else 0
        async for ev in self._emit_done(
            telemetry, ctx,
            iterations_done,
            ctx.state,
            full_content, full_reasoning, harness,
        ):
            yield ev

    async def _execute_tool_calls(
        self,
        llm_out: LLMOutput,
        ctx: RunContext,
        harness: "Harness",
        telemetry: TelemetryBus,
        iteration: int,
    ) -> AsyncIterator[TraceEvent]:
        """ACT 阶段：执行本轮所有工具调用 + OBSERVE → THINK 状态转移。

        包含 pre_tool_call 拦截、Saddle 执行、post_tool_call 拦截/回滚、
        span 追踪、once 权限清理。

        只读工具并行化（2026-10）：
        连续 ≥2 个只读观测调用（contracts.READONLY_TOOLS）构成一个并行段 ——
        段内先串行逐个跑 PRE_TOOL_CALL（append 紧跟检查，ctx.active_tool_call
        的取值与串行语义逐字一致），再 asyncio.gather 并发执行 Saddle，最后
        按原始顺序串行做 POST_TOOL_CALL + 审计 + 消息追加。审计链写入顺序与
        OpenAI「role=tool 紧随 assistant.tool_calls 按序配对」（vLLM 400 红线，
        见 bridle._split_segments）两条约束都不被破坏。
        段与段之间严格串行：副作用工具（shell_exec/file_write/...）是天然
        屏障，[read, write, read] 中 write 前后的 read 不会被交换进同一批。
        并发安全性依据：ssh2 每次 exec() 开独立 channel（同会话可并发）、
        bridge_relay 以 req_id→Future 支持并发请求、本地文件读走
        asyncio.to_thread、JobManager 由 threading.Lock + 逐 job asyncio.Lock
        保护、Saddle 只读路径不触碰 tool_calls_log。
        """
        # ── ACT: 执行工具 ──
        await ctx.transition(State.ACTING, event="need_tool")

        calls = llm_out.tool_calls
        i = 0
        n = len(calls)
        while i < n:
            # 连续只读段划分：≥2 才走并行路径；单条只读与全部副作用工具
            # 走原串行路径（行为与并行化之前逐字等价）
            if calls[i].name in READONLY_TOOLS:
                j = i
                while j + 1 < n and calls[j + 1].name in READONLY_TOOLS:
                    j += 1
                if j > i:
                    async for ev in self._execute_readonly_segment(
                        calls[i:j + 1], ctx, harness, telemetry, iteration,
                    ):
                        yield ev
                    i = j + 1
                    if ctx.metadata.get("force_terminated"):
                        break
                    continue
            async for ev in self._execute_serial_call(
                calls[i], ctx, harness, telemetry, iteration,
            ):
                yield ev
            i += 1
            if ctx.metadata.get("force_terminated"):
                break

        # 收尾：清掉「当前调用」指针，避免陈旧值泄漏到本轮之后的检查点
        ctx.current_tool_call = None

        # 注：once 授权不再于每轮迭代末清空（C-review M8：改为 run 开始时
        # 清空 + 消费时移除，消除与审批的时序竞态，见 drive() 开头）

        # C-review M6：force_terminated 时状态已是 FAILED，跳过 OBSERVE→THINK
        # 转移，避免从终态再转移污染 state_history
        if ctx.metadata.get("force_terminated"):
            return

        # ── OBSERVING → THINK ──
        await ctx.transition(State.OBSERVING, event="continue")
        await ctx.transition(State.THINKING, event="continue")
        yield TraceEvent(
            kind="state",
            source="driver",
            run_id=ctx.run_id,
            iteration=iteration + 1,
            payload={"state": "thinking"},
        )

    async def _execute_serial_call(
        self,
        tc: ToolCall,
        ctx: RunContext,
        harness: "Harness",
        telemetry: TelemetryBus,
        iteration: int,
    ) -> AsyncIterator[TraceEvent]:
        """串行执行单个工具调用 —— 原 _execute_tool_calls 循环体的逐字搬移。

        副作用工具与单条只读调用走这里：append → PRE → 执行 → POST → 消息
        全部在本次调用的位置完成，行为与并行化之前逐字等价。
        唯一结构差异：日志条目改为持有 entry 引用直接写入（不再经
        tool_calls_log[-1] 间接寻址），取值完全相同、且不再依赖「当前条目
        恰好是列表末尾」这一位置巧合。
        """
        # 记录到 ctx.tool_calls_log（供 Reins 检查）
        entry: dict[str, Any] = {
            "tool": tc.name,
            "args": tc.args,
            "iteration": iteration,
            "success": None,  # 待执行
        }
        ctx.tool_calls_log.append(entry)
        ctx.current_tool_call = entry

        # ── pre_tool_call（Reins 检查）──
        pre_tool_decision = await harness.reins.check(ReinPoint.PRE_TOOL_CALL, ctx)
        # 审批等待闸（一）：先登记等待项，再把卡片事件发出去。顺序反了就是死锁 ——
        # permission_needed 挂在决策返回值上，不发出去用户根本看不到卡，
        # 而「等一个看不见的卡片」只能等到兜底超时（默认 10 分钟）。
        approval_handle = self._register_approval(ctx, pre_tool_decision)
        try:
            if pre_tool_decision.intercepted:
                # 拦截
                for ev in pre_tool_decision.events:
                    await telemetry.emit(ev)
                    yield ev
                # 已发出，清空避免下方分支二次发同一批
                pre_tool_decision.events = []
            # 审批等待闸（二）：卡片已到用户眼前，这里真的停住，等 grant / deny 唤醒
            # 后重判一次 —— 授权状态是权威，gate/守卫自己给出放行或拒绝结论，
            # Driver 不复制第二套判定。未启用等待（定时任务）时原样透传。
            if approval_handle is not None:
                pre_tool_decision, approval_events = await self._finish_approval(
                    ctx, harness, pre_tool_decision, approval_handle)
                for ev in approval_events:
                    await telemetry.emit(ev)
                    yield ev
        finally:
            # 发卡期间抛异常，或客户端断连导致生成器在 yield 处被关闭，都会绕开
            # wait_registered 的清理 —— 这里兜一层（正常路径是 no-op）。
            if approval_handle is not None:
                approval_wait.discard(approval_handle[0])
        if pre_tool_decision.intercepted:
            result_text = f"工具调用被拦截: {pre_tool_decision.intercept_reason}"
            entry["success"] = False
            # 拦截原因落到条目上：ConfidenceMonitor 靠它区分「策略拒绝」
            # （prod 未预授权，豁免置信度扣分）与「真实失败」
            entry["intercept_reason"] = pre_tool_decision.intercept_reason
            # 被拦截说明是危险命令，priority 标记为 high 供 compress_messages 优先保留
            ctx.messages.append(harness.bridle.build_tool_result_message(tc.id, result_text, priority="high"))
            yield TraceEvent(
                kind="tool_call_result",
                source="reins",
                run_id=ctx.run_id,
                iteration=iteration,
                payload={
                    "tool": tc.name,
                    "result": result_text,
                    "success": False,
                    "duration_ms": 0,
                },
            )
            if pre_tool_decision.terminate:
                # C-review M6：拦截器要求终止整个 run（如 LoopDetector 第 3
                # 次触发）。原实现 continue 继续下一工具调用/迭代，"强制终止"
                # 文案与行为不符。这里 transition(FAILED) + 通知主循环 break。
                await ctx.transition(State.FAILED, event="force_terminated")
                await harness.reins.check(ReinPoint.AGENT_FAILED, ctx)
                ctx.metadata["force_terminated"] = True
                return
            return

        # 审查R2修复：非拦截分支原先整体丢弃 Reins.check 聚合的 events/
        # inject_messages（core.check 的未拦截透传没人消费）— 澄清答案的
        # [用户澄清结果] 注入与 clarification_done 事件、LoopDetector 第 1 级
        # 纠偏全部静默蒸发，用户答完向导后模型仍看不到答案。与
        # POST_TOOL_CALL 非拦截分支同口径消费。
        if pre_tool_decision.events or pre_tool_decision.inject_messages:
            for ev in pre_tool_decision.events:
                await telemetry.emit(ev)
                yield ev
            if pre_tool_decision.inject_messages:
                ctx.messages.extend(pre_tool_decision.inject_messages)

        # ── 发 tool_call_start ──
        # H13 修复 A3：push span_id 到栈，供嵌套工具调用的 parent_span_id 追踪
        ctx.span_stack.append(tc.id)
        parent_span = ctx.span_stack[-2] if len(ctx.span_stack) >= 2 else ""
        yield TraceEvent(
            kind="tool_call_start",
            source="driver",
            run_id=ctx.run_id,
            iteration=iteration,
            payload={"tool": tc.name, "args": tc.args, "tool_call_id": tc.id},
            parent_span_id=parent_span,
        )

        # ── Saddle 执行工具 ──
        result, undo = await harness.saddle.execute(
            tc, ctx,
            auto_rollback=ctx.config.auto_rollback_on_tool_failure,
        )
        # 可见终端模式：工具要求把命令交付到可见终端（前端 PTY/SSH）而非
        # headless 执行。发出 tool_deliver 事件挂起等待前端回传结果，
        # 跳过正常 post_tool_call / tool_call_result / 消息追加路径。
        if getattr(result, "pending_delivery", None):
            async for ev in self._emit_and_await_visible(
                ctx, harness, telemetry, result, tc, iteration,
            ):
                yield ev
            return
        entry["success"] = result.success
        # ConfidenceMonitor 用输出尾部的 [exit_code=N] 哨兵区分"探针非零
        # 退出"与"调用真实失败"，只需尾部片段（哨兵在末行，stderr 在前）
        if not result.success and result.output:
            entry["output_tail"] = result.output[-512:]
        # C-review M7：命令执行完成（审批已放行）→ 移出「等待审批」集合。
        # 审查R2修复：原实现只在 success=True 时 pop — once 授权在
        # check_permission 放行时已被消费清零，若随后工具执行失败，旧 sig
        # 滞留 pending：同命令重试走 is_first=False 分支不再建卡（用户无卡
        # 可点），注入文案却称"已在界面上请求授权"，空转至 600s 兜底；
        # 且 pending 内签名永久豁免循环检测。授权既已消费，成败都应出集。
        self._pop_permission_pending(ctx, tc)

        # ── post_tool_call ──
        post_tool_decision = await harness.reins.check(ReinPoint.POST_TOOL_CALL, ctx)
        # H13 修复 B2：补全 intercepted + undo_actions 处理
        # 原实现只处理 events + inject_messages，未处理 intercepted 和 undo_actions 字段
        if post_tool_decision.intercepted:
            # 拦截器要求终止本次工具调用 → 用拦截结果替代 saddle 的 result
            # 审查修复（P2 E7）：拦截分支漏 await telemetry.emit — 原实现
            # 只 yield 事件给 Harness 合并流，未写入 TelemetryBus，
            # 导致 TraceViewer / audit 记录中 POST_TOOL_CALL 拦截事件丢失
            for ev in post_tool_decision.events:
                await telemetry.emit(ev)
                yield ev
            # 执行拦截器声明的 undo_actions（如严重错误时回滚已写入文件）
            if post_tool_decision.undo_actions:
                for action in post_tool_decision.undo_actions:
                    try:
                        await action.apply_reverse()
                    except Exception as e:
                        logger.warning("POST_TOOL_CALL undo 失败: %s", e)
            # 构建拦截结果（high 优先级，确保压缩时保留）
            result = ToolResult(
                success=False,
                error=post_tool_decision.intercept_reason or "POST_TOOL_CALL 拦截",
                priority="high",
            )
            entry["success"] = False
            if post_tool_decision.inject_messages:
                ctx.messages.extend(post_tool_decision.inject_messages)
        elif post_tool_decision.events or post_tool_decision.inject_messages:
            # 原有逻辑：发出 events + 注入 messages（不拦截）
            # 审查修复（P2 E7）：非拦截分支同样漏 await telemetry.emit
            for ev in post_tool_decision.events:
                await telemetry.emit(ev)
                yield ev
            if post_tool_decision.inject_messages:
                ctx.messages.extend(post_tool_decision.inject_messages)

        # ── 发 tool_call_result ──
        if result.success:
            result_text = result.output
        else:
            # H13 修复 C6：使用 Stirrups.build_error_feedback 构建差异化反馈
            # 按错误类型给 LLM 不同的修复指引（参数错误/权限拦截/瞬时故障/致命错误）
            from .stirrups import ErrorClassifier, build_error_feedback
            error_type = ErrorClassifier.classify_tool_error(result.error or "", tc.name)
            result_text = build_error_feedback(result.error or "", tc.name, error_type)
            # 工具失败时带回的 output（远程 stderr / 已产生的部分输出）一并回传，
            # 否则 Agent 只看得到一行 error，只能原样重试
            if result.output:
                result_text += f"\n--- 失败时的部分输出（末尾 2000 字）---\n{result.output[-2000:]}"
        yield TraceEvent(
            kind="tool_call_result",
            source="saddle",
            run_id=ctx.run_id,
            iteration=iteration,
            payload={
                "tool": tc.name,
                "result": result_text,
                "success": result.success,
                "duration_ms": result.duration_ms,
                "tool_call_id": tc.id,
            },
        )

        # 工具结果加入消息历史，透传 priority 供 compress_messages 读取
        ctx.messages.append(harness.bridle.build_tool_result_message(tc.id, result_text, priority=result.priority))

        # H13 修复 A3：pop span_id（与 tool_call_start 的 push 配对）
        if ctx.span_stack:
            ctx.span_stack.pop()

    async def _execute_readonly_segment(
        self,
        seg: list[ToolCall],
        ctx: RunContext,
        harness: "Harness",
        telemetry: TelemetryBus,
        iteration: int,
    ) -> AsyncIterator[TraceEvent]:
        """并行执行一段连续的只读观测调用（len ≥ 2，成员均属 READONLY_TOOLS）。

        三阶段流水线：
        A. 串行逐个 append + PRE_TOOL_CALL —— append 紧跟检查，
           ctx.active_tool_call 的取值与串行路径逐字一致；被拦截的成员
           不进执行批（其消息顺延到 C 阶段按序落位），terminate 成员截断
           其后所有成员（与串行 break 语义一致）。
        B. asyncio.gather 并发执行 Saddle —— 逐调用隔离异常：失败转成带
           异常文本的 ToolResult 诚实上报，不放大为整段失败。
        C. 按原始顺序串行提交：PRE 注入落位 → POST_TOOL_CALL（审计链按
           调用顺序写入）→ tool_call_result → 消息追加（role=tool 与
           assistant.tool_calls 按序配对）。
        段内 PRE 注入顺延到 C 阶段才进 ctx.messages：串行路径下注入与本调用
        的工具结果之间没有其他调用的消息，顺延后相对顺序逐字不变；若在
        A 阶段立即落位，注入会插到前一个成员的结果之前，破坏消息顺序。
        """
        @dataclass
        class _Member:
            tc: ToolCall
            entry: dict[str, Any]
            intercepted: bool = False
            intercept_text: str = ""
            terminate: bool = False
            pre_injects: list[dict[str, str]] = field(default_factory=list)

        members: list[_Member] = []
        # H13 span 语义保持：串行路径下每个调用 push 时栈深恒为 1（前一个已
        # 配对 pop），parent_span 恒等于「本批入栈前的栈顶」。段内一次性 push
        # 全部通过成员、C 阶段配对 pop，parent_span 取值与串行逐字一致。
        seg_parent = ctx.span_stack[-1] if ctx.span_stack else ""

        # ── A. 串行 PRE 检查（append 紧跟检查）──
        for tc in seg:
            entry: dict[str, Any] = {
                "tool": tc.name,
                "args": tc.args,
                "iteration": iteration,
                "success": None,
            }
            ctx.tool_calls_log.append(entry)
            ctx.current_tool_call = entry

            pre_tool_decision = await harness.reins.check(ReinPoint.PRE_TOOL_CALL, ctx)
            # 审批等待闸（一）：先登记等待项、再发卡片（与串行路径同口径）。
            approval_handle = self._register_approval(ctx, pre_tool_decision)
            try:
                if pre_tool_decision.intercepted:
                    for ev in pre_tool_decision.events:
                        await telemetry.emit(ev)
                        yield ev
                    pre_tool_decision.events = []
                # 审批等待闸（二）：该成员需要审批时真的停住等裁决，醒来重判；
                # 阻塞发生在 A 阶段，其后成员的 PRE 检查顺延到裁决之后
                # （与串行「审批卡住后面全部」的时序一致）。
                if approval_handle is not None:
                    pre_tool_decision, approval_events = await self._finish_approval(
                        ctx, harness, pre_tool_decision, approval_handle)
                    for ev in approval_events:
                        await telemetry.emit(ev)
                        yield ev
            finally:
                # 同串行路径：发卡期间异常/断连绕开 wait_registered 的清理
                if approval_handle is not None:
                    approval_wait.discard(approval_handle[0])
            m = _Member(tc=tc, entry=entry)
            if pre_tool_decision.intercepted:
                m.intercepted = True
                m.intercept_text = f"工具调用被拦截: {pre_tool_decision.intercept_reason}"
                entry["success"] = False
                # 与串行拦截分支同口径：落拦截原因供置信度豁免识别
                entry["intercept_reason"] = pre_tool_decision.intercept_reason
                members.append(m)
                if pre_tool_decision.terminate:
                    # 与串行 break 语义一致：其后成员不再登记、不再执行、
                    # 不产生任何消息（LLM 只看到已提交成员与终止提示）
                    m.terminate = True
                    break
                continue

            if pre_tool_decision.events:
                for ev in pre_tool_decision.events:
                    await telemetry.emit(ev)
                    yield ev
            m.pre_injects = list(pre_tool_decision.inject_messages or [])

            ctx.span_stack.append(tc.id)
            yield TraceEvent(
                kind="tool_call_start",
                source="driver",
                run_id=ctx.run_id,
                iteration=iteration,
                payload={"tool": tc.name, "args": tc.args, "tool_call_id": tc.id},
                parent_span_id=seg_parent,
            )
            members.append(m)

        # ── B. 并发执行（仅通过 PRE 的成员）──
        results: dict[int, ToolResult] = {}
        executing = [(idx, m) for idx, m in enumerate(members) if not m.intercepted]
        if executing:
            async def _run_one(idx: int, m: "_Member") -> None:
                try:
                    result, _undo = await harness.saddle.execute(
                        m.tc, ctx,
                        auto_rollback=ctx.config.auto_rollback_on_tool_failure,
                    )
                except Exception as e:
                    # 逐调用隔离：并行段内一个调用的异常不放大为整段失败；
                    # 异常文本诚实上报（LLM 看得到真实原因并自行重试/换路）
                    logger.exception("并行只读调用 %s 执行异常", m.tc.name)
                    result = ToolResult(success=False, error=str(e), priority="high")
                results[idx] = result

            await asyncio.gather(*[_run_one(idx, m) for idx, m in executing])

        # ── C. 按原始顺序串行提交 ──
        for idx, m in enumerate(members):
            ctx.current_tool_call = m.entry
            if m.intercepted:
                # 与串行拦截分支同口径：拦截决策的 inject_messages 不消费
                # （串行路径同样丢弃），只落拦截结果消息 + tool_call_result
                ctx.messages.append(harness.bridle.build_tool_result_message(
                    m.tc.id, m.intercept_text, priority="high"))
                yield TraceEvent(
                    kind="tool_call_result",
                    source="reins",
                    run_id=ctx.run_id,
                    iteration=iteration,
                    payload={
                        "tool": m.tc.name,
                        "result": m.intercept_text,
                        "success": False,
                        "duration_ms": 0,
                    },
                )
                if m.terminate:
                    await ctx.transition(State.FAILED, event="force_terminated")
                    await harness.reins.check(ReinPoint.AGENT_FAILED, ctx)
                    ctx.metadata["force_terminated"] = True
                    break
                continue

            result = results.get(idx)
            if result is None:  # 防御：B 阶段未产出（理论不可达）
                result = ToolResult(success=False, error="并行执行未返回结果", priority="high")
            # 只读观测工具不产生 pending_delivery（唯一产生点 shell_exec 不在
            # READONLY_TOOLS）；若未来有工具违反该契约，在这里显式失败而不是
            # 静默丢交付 —— 可见终端挂起等待必须走串行路径。
            if getattr(result, "pending_delivery", None):
                logger.error(
                    "只读工具 %s 返回 pending_delivery（契约违规），按失败处理", m.tc.name)
                result = ToolResult(
                    success=False,
                    error="只读工具不允许交付可见终端（并行段不支持 pending_delivery）",
                    priority="high",
                )
            m.entry["success"] = result.success
            if not result.success and result.output:
                m.entry["output_tail"] = result.output[-512:]
            self._pop_permission_pending(ctx, m.tc)

            # PRE 注入按原顺序落位（先于本调用的工具结果，与串行一致）
            if m.pre_injects:
                ctx.messages.extend(m.pre_injects)

            post_tool_decision = await harness.reins.check(ReinPoint.POST_TOOL_CALL, ctx)
            if post_tool_decision.intercepted:
                for ev in post_tool_decision.events:
                    await telemetry.emit(ev)
                    yield ev
                if post_tool_decision.undo_actions:
                    for action in post_tool_decision.undo_actions:
                        try:
                            await action.apply_reverse()
                        except Exception as e:
                            logger.warning("POST_TOOL_CALL undo 失败: %s", e)
                result = ToolResult(
                    success=False,
                    error=post_tool_decision.intercept_reason or "POST_TOOL_CALL 拦截",
                    priority="high",
                )
                m.entry["success"] = False
                if post_tool_decision.inject_messages:
                    ctx.messages.extend(post_tool_decision.inject_messages)
            elif post_tool_decision.events or post_tool_decision.inject_messages:
                for ev in post_tool_decision.events:
                    await telemetry.emit(ev)
                    yield ev
                if post_tool_decision.inject_messages:
                    ctx.messages.extend(post_tool_decision.inject_messages)

            if result.success:
                result_text = result.output
            else:
                from .stirrups import ErrorClassifier, build_error_feedback
                error_type = ErrorClassifier.classify_tool_error(result.error or "", m.tc.name)
                result_text = build_error_feedback(result.error or "", m.tc.name, error_type)
                if result.output:
                    result_text += f"\n--- 失败时的部分输出（末尾 2000 字）---\n{result.output[-2000:]}"
            yield TraceEvent(
                kind="tool_call_result",
                source="saddle",
                run_id=ctx.run_id,
                iteration=iteration,
                payload={
                    "tool": m.tc.name,
                    "result": result_text,
                    "success": result.success,
                    "duration_ms": result.duration_ms,
                    "tool_call_id": m.tc.id,
                },
            )
            ctx.messages.append(harness.bridle.build_tool_result_message(
                m.tc.id, result_text, priority=result.priority))
            # 配对 pop（A 阶段为通过成员 push 的 span）
            if ctx.span_stack:
                ctx.span_stack.pop()

        ctx.current_tool_call = None
    async def _emit_and_await_visible(
        self,
        ctx: RunContext,
        harness: "Harness",
        telemetry: TelemetryBus,
        result: ToolResult,
        tc: ToolCall,
        iteration: int,
    ) -> AsyncIterator[TraceEvent]:
        """可见终端模式：发出 tool_deliver 事件并挂起等待前端回传结果。

        流程：
        1. 发出 TraceEvent(kind="tool_deliver")，通知前端把 command 写入真实 PTY/SSH 会话。
        2. 经 PendingDeliveries.wait() 挂起，等待前端 POST /api/agent/tool-result 恢复。
        3. 挂起用 try/finally 包裹：外层 CancelledError（用户中止 SSE → run 被取消）
           或超时都会清理注册表中的待交付项，避免泄漏。
        4. 恢复后按 超时/被拒/成功 构建 result_text，发 tool_call_result 事件结束本调用。
        """
        import asyncio
        from agent.pending_delivery import PendingDeliveries

        pd = result.pending_delivery
        tool_id = pd["tool_id"]
        command = pd.get("command", "")

        # 1. tool_deliver 事件 —— 解释层与审批卡同款（纯展示，不参与放行判定）：
        #    交付卡也要回答「这条命令会做什么」，否则用户面对一行裸命令无从拍板。
        #    safe 命令 reason 为 None，用只读白名单的确定文案兜底，不落「无法预知」
        #    的 fallback（那句话对 ls/cat 是误导）。
        #    复合命令（; / && 多段）走拆解层：逐段意图 + 聚合结论，单句 explain
        #    只承载聚合文案。展示用的 risk 取段级聚合；权威判定仍是上面对整条
        #    命令跑的 classify_command —— 拆解层绝不参与放行。
        level, reason = classify_command(command)
        comp = explain_compound(command)
        ro = is_provably_readonly(command) if level == "safe" else None
        segments_payload: list[dict[str, Any]] = []
        if comp.compound:
            explain_text, explain_source, needs_llm = comp.text, "static", comp.needs_llm
            # 展示风险不得低于权威判定：整串写目标可能让 comp 段级聚合偏轻
            display_level = level if level != "safe" else comp.level
            segments_payload = [
                {"command": s.command, "intent": s.intent, "level": s.level, "note": s.note}
                for s in comp.segments
            ]
        elif level == "safe":
            explain_text, explain_source, needs_llm = (
                "这是一条只读命令，不会改动系统配置或数据。", "static", False)
            display_level = level
        else:
            expl = explain_for_reason(reason or "")
            explain_text, explain_source, needs_llm = (
                expl.text, expl.source, expl.needs_llm)
            display_level = level
        # safe ≠ readonly：tee/curl -o 等免审批但可能写盘，不能说成只读
        if level == "safe" and ro is not None and not ro.safe:
            explain_text, explain_source, needs_llm = (
                "这条命令本身无需审批，但未能证明只读：可能写入磁盘或改变目标机状态，"
                "执行前请核对。",
                "static",
                False,
            )
        deliver_ev = TraceEvent(
            kind="tool_deliver",
            source="saddle",
            run_id=ctx.run_id,
            iteration=iteration,
            payload={
                "tool_id": tool_id,
                "command": command,
                "session_id": pd.get("session_id", ""),
                "requires_confirm": pd.get("requires_confirm", False),
                "readonly": bool(ro and ro.safe),
                "readonly_rule": ro.rule_id if ro else "",
                "explain": explain_text,
                "explain_source": explain_source,
                "risk": display_level,
                "task": ctx.task,
                "segments": segments_payload,
            },
        )
        await telemetry.emit(deliver_ev)
        yield deliver_ev

        # 「未知命令」族的静态文案信息量不足 —— 与审批卡同款：后台异步补一条
        # LLM 生成的一句话解释（带 tool_id 让前端精确回填交付卡）。不等待模型，
        # 失败/超时只是没有回填事件，卡片保留静态文案。
        if needs_llm:
            await maybe_explain_via_llm(
                ctx, tc.name, command, command, _audit_environment(ctx),
                target_tool_id=tool_id,
            )

        try:
            timeout = PendingDeliveries.timeout_of(tool_id)
            try:
                await asyncio.wait_for(PendingDeliveries.wait(tool_id), timeout=timeout)
            except asyncio.TimeoutError:
                result_text = (
                    f"等待可见终端执行超时（或会话已结束），命令 {command} 未能确认完成；"
                    "请手动检查终端。"
                )
                ev = await self._finish_visible(
                    ctx, harness, telemetry, result, tc, iteration,
                    result_text=result_text, success=False,
                )
                yield ev
                return

            reg = PendingDeliveries.get(tool_id)
            if reg is None:
                # 等待被唤醒但注册项已被清理（异常/并发中止）→ 视为未确认
                result_text = (
                    f"可见终端交付已结束，命令 {command} 未能确认完成；请手动检查终端。"
                )
                ev = await self._finish_visible(
                    ctx, harness, telemetry, result, tc, iteration,
                    result_text=result_text, success=False,
                )
                yield ev
                return

            if reg.rejected:
                result_text = f"[用户拒绝执行] {command}"
                ev = await self._finish_visible(
                    ctx, harness, telemetry, result, tc, iteration,
                    result_text=result_text, success=False,
                )
                yield ev
                return

            # 成功
            result_text = f"[已写入可见终端] {command}\n{reg.output}"
            if reg.exit_code is not None:
                result_text += f"\n{EXIT_CODE_PREFIX}{reg.exit_code}]"
            ev = await self._finish_visible(
                ctx, harness, telemetry, result, tc, iteration,
                result_text=result_text, success=reg.success,
            )
            yield ev
        finally:
            # 无论成功/超时/异常/外部取消（CancelledError），都清理注册项防泄漏
            PendingDeliveries.remove(tool_id)

    def _pop_permission_pending(self, ctx: RunContext, tc: ToolCall) -> None:
        """把本次调用的签名移出「等待审批」集 —— 成败、是否走可见终端都要出集。

        S2-02：可见终端分支的 `continue` 跳过了这一步，后果有两条，都在默认路径上
        （ssh-ai.ts 硬编 visibleTerminal: true）：① 授权既已在 check_permission
        放行时消费掉，滞留的旧签名让同命令本 run 内永不再出卡（guards 的
        is_first=False 分支 events 为空），而注入文案仍写着「已在界面上向用户请求
        授权，请重新发起完全相同的命令（授权后会被放行）」—— 原卡已 resolved 进
        只读追溯区，用户无从照做，只能空转到兜底时长；② flow.py 对在册签名直接
        no-op，等于该命令永久豁免循环检测。
        """
        pending = ctx.metadata.get("permission_pending")
        if not pending:
            return
        try:
            pending.pop(tool_call_signature({"tool": tc.name, "args": tc.args}), None)
        except Exception:
            pass

    def _register_approval(
        self,
        ctx: RunContext,
        decision: ReinDecision,
    ) -> tuple[approval_wait.Waiter, str] | None:
        """审批等待闸（一）：为「即将弹卡」的调用登记等待项。

        为什么必须与发卡分开：permission_needed 躺在 ReinDecision.events 里，由
        调用方 yield 出去用户才看得见。若先阻塞再发卡，等的是用户看不见的卡片
        —— 只能等到兜底超时（默认 600s）；若先发卡再登记，用户在卡片刚出现那一
        瞬间点「允许」会唤醒落空，run 同样挂死。唯一正确的顺序是：登记 → 发卡 →
        等待，所以这里只登记，等待交给 _finish_approval。

        只在交互式 run 启用（AWAIT_APPROVAL_KEY，routes 层置位）：定时任务没有
        可点卡片的人，等下去就是挂死。

        Returns: (等待项, 本次调用的签名)；不需要等待时 None。
        """
        if not ctx.metadata.get(AWAIT_APPROVAL_KEY):
            return None
        if not decision.intercepted:
            # 没人被拦就没人要等（approval_key 只由发卡分支填写；将来若真有
            # 拦截器「只给键不拦截」，也不能把 run 白挂一次到兜底超时）
            return None
        key = decision.approval_key
        if not key:
            # hardline / 曾被拒绝 / prod 未预登记……那些是「直接否掉」，没有人要等
            return None

        # 并行段内 [-1] 已不是当前条目 → 走 active_tool_call（调用方已指向本条目）
        last = ctx.active_tool_call or {}
        tool = str(last.get("tool") or "")
        sig = tool_call_signature({"tool": tool, "args": last.get("args") or {}})
        # 同一签名只等一次：醒来重判若仍未放行（授权键对不上、批的是另一台主机），
        # 再等就是永久挂起 —— 把决策交回模型，让它看到「被拦截」并换方案。
        waited = ctx.metadata.setdefault("approval_waited", set())
        if sig in waited:
            return None
        waiter = approval_wait.register(ctx.run_id, key, tool)
        if waiter is None:
            # 没有事件循环（同步调用方）→ 不阻塞，交回原有拦截路径
            return None
        waited.add(sig)
        return waiter, sig

    async def _finish_approval(
        self,
        ctx: RunContext,
        harness: "Harness",
        decision: ReinDecision,
        handle: tuple[approval_wait.Waiter, str],
    ) -> tuple[ReinDecision, list[TraceEvent]]:
        """审批等待闸（二）：停住等裁决，醒来重判一次 PRE_TOOL_CALL。

        为什么停下：拦截器发完 permission_needed 只 return intercepted，Driver 把
        「工具调用被拦截」当结果回给模型就 continue —— 用户还在看卡片，run 已经把
        同一轮剩下的工具、后面几轮全跑完了。审批在物理上存在，在时序上不起作用。

        为什么醒来要重判而不是另写一套：授权状态（permanent / session / once /
        拒绝标记）此刻已是权威，让 gate 和守卫自己给出「放行」或「曾被用户拒绝」，
        Driver 不复制第二套判定 —— 两套判定必然漂移。

        Returns: (生效的决策, 需要在调用方发出的追加事件)。
        """
        waiter, sig = handle
        verdict = await approval_wait.wait_registered(
            waiter, approval_fallback_seconds())
        if verdict not in (approval_wait.ALLOWED, approval_wait.DENIED):
            # 兜底超时 / 等待项被回收 → 落回原有拦截路径（卡片保留待办，
            # 模型重发时由守卫发 permission_expired）
            return decision, []

        logger.info(
            "审批裁决到达 run=%s key=%s verdict=%s，重判一次 PRE_TOOL_CALL",
            ctx.run_id, waiter.key, verdict,
        )
        redone = await harness.reins.check(ReinPoint.PRE_TOOL_CALL, ctx)
        # 重判产生的事件交给调用方发；这里清空避免拦截分支二次发同一批
        events = list(redone.events)
        redone.events = []
        if verdict == approval_wait.ALLOWED and redone.intercepted:
            # 批了却仍拦：批的键和这次调用的键不是一把（command 归一化差异、
            # 主机作用域不匹配……）。不能把模型晾在「已请求授权」上反复重试 ——
            # 把签名移出审批豁免，循环检测才能接管。
            pending = ctx.metadata.get("permission_pending")
            if isinstance(pending, dict):
                pending.pop(sig, None)
            redone.intercept_reason = (
                f"{redone.intercept_reason}（用户已授权，但授权范围未覆盖本次调用）")
        elif verdict == approval_wait.DENIED and not redone.intercepted:
            # 拒绝标记没盖住本次调用（键漂移时兜底按工具唤醒，落到了同工具的
            # 另一个命令上）：不能因为「有人点了拒绝」就当没事发生放行。
            # 拒绝一律 fail-closed —— 宁可拦下让模型换方案，不可静默执行
            # 用户从未批准的命令。
            redone.intercepted = True
            redone.intercept_reason = "用户拒绝了本次审批请求"
            # 与 ALLOWED 分支同口径：出集后不再假定有卡可点，让循环检测接管
            pending = ctx.metadata.get("permission_pending")
            if isinstance(pending, dict):
                pending.pop(sig, None)
        return redone, events

    async def _completion_guard(
        self,
        ctx: RunContext,
        harness: "Harness",
        final_text: str,
        iteration: int,
        next_round_budget: bool,
        intention_level: str,
    ) -> tuple[bool, list[TraceEvent]]:
        """
        假完成强校验 — 只覆盖无 workflow goal 的 run。

        有 goal 的 run 走上方 goal 自检锚（_goal_self_checked）。无 goal 时，
        模型宣告完成的唯一凭据是「它自己不调工具了」——答非所问也会被放行。
        这里用一次轻量 LLM 裁决把钉扎任务原文与最终答案对表：

        - FAIL → 注入纠偏 user 消息 + return True（调用方 continue 续跑）；
          每 run 至多一次（metadata 标记），第二次放行防死循环，且续跑天然受
          max_iterations / 成本熔断 / 墙钟三重兜底
        - PASS / 超时 / 异常 / 解析失败 → fail-open 放行（绝不阻断收尾，
          口径同生成式摘要静默降级）

        触发门槛：任务非空、意图等级 ≥ moderate（trivial/simple 不值得多花
        一次调用）、还有下一轮预算（最后一轮 continue 会落 loop-else 误判
        MAX_REACHED，同 goal 自检锚的教训）、最终答案非空。
        裁决期间临时摘掉 chunk 回调：PASS/FAIL 片段不得流进用户对话。
        """
        if not ctx.config.completion_guard_enabled:
            return False, []
        if not next_round_budget:
            return False, []
        if not final_text or not final_text.strip():
            return False, []
        if ctx.metadata.get("_completion_guarded"):
            return False, []
        task_text = (ctx.task or "").strip()
        if not task_text:
            return False, []
        if intention_level not in ("moderate", "complex"):
            return False, []
        try:
            # 置位在裁决前：异常/重入下宁可少校验一次，不可双发注入
            ctx.metadata["_completion_guarded"] = True
            return await self._completion_guard_judge(
                ctx, harness, task_text, final_text, iteration)
        except Exception as e:  # noqa: BLE001 — fail-open 铁律
            logger.warning("CompletionGuard 异常，fail-open 放行: %s", e)
            return False, []

    async def _completion_guard_judge(
        self,
        ctx: RunContext,
        harness: "Harness",
        task_text: str,
        final_text: str,
        iteration: int,
    ) -> tuple[bool, list[TraceEvent]]:
        """执行裁决调用并解析结果（PASS 放行 / FAIL 纠偏续跑 / 异常向上抛）"""
        client = harness.stirrups.llm_client
        prompt = (
            "你是任务完成度裁决器。判断下面的「最终答案」是否实质覆盖了「原始任务」"
            "的全部核心要求。宁可保守：只答了一部分、答非所问、把计划当结果交付，"
            "都算未完成。输出格式：第一行只写 PASS 或 FAIL；若 FAIL，"
            "第二行用一句话说明缺了什么。\n\n"
            f"[原始任务]\n{task_text[:2000]}\n\n"
            f"[最终答案]\n{final_text[:4000]}"
        )
        prev_cb = getattr(client, "_on_chunk", None)
        setattr(client, "_on_chunk", None)  # 裁决片段不得流入用户对话
        try:
            out = await asyncio.wait_for(
                client.call(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[],
                    model=ctx.llm_model,
                    temperature=0.1,
                    max_tokens=64,
                    enable_thinking=False,
                ),
                timeout=ctx.config.completion_guard_timeout_s,
            )
        finally:
            setattr(client, "_on_chunk", prev_cb)
        raw = (getattr(out, "content", "") or "").strip()
        if not raw:
            return False, []
        lines = raw.splitlines()
        verdict = lines[0].strip().upper()
        if verdict.startswith("PASS"):
            return False, []
        if not verdict.startswith("FAIL"):
            # 输出不合约定 → fail-open（口径同 auto_approve 的 _parse_verdict）
            logger.warning("CompletionGuard 输出不合约定，fail-open 放行: %r", verdict)
            return False, []
        gap = lines[1].strip() if len(lines) > 1 else ""
        reason = gap or "最终答案未完整覆盖任务要求"
        ctx.messages.append({
            "role": "user",
            "content": (
                f"[系统拦截] 完成度校验未通过：{reason}。"
                "请补充缺失部分后重新给出完整结果；若确有客观限制无法完成，"
                "请在结果中明确说明已完成与未完成的边界。"
            ),
        })
        events = [TraceEvent(
            kind="warning",
            source="driver.completion_guard",
            run_id=ctx.run_id,
            iteration=max(0, iteration),
            payload={
                "message": f"完成度校验未通过，已要求 Agent 补充：{reason[:200]}",
            },
        )]
        logger.info(
            "CompletionGuard FAIL，注入纠偏续跑 run=%s iteration=%d",
            ctx.run_id, iteration,
        )
        return True, events

    async def _finish_visible(
        self,
        ctx: RunContext,
        harness: "Harness",
        telemetry: TelemetryBus,
        result: ToolResult,
        tc: ToolCall,
        iteration: int,
        result_text: str,
        success: bool,
    ) -> TraceEvent:
        """可见终端恢复后收尾：更新日志、追加消息历史、pop span、发 tool_call_result。"""
        # 走 active_tool_call 而非 [-1]：串行路径下二者等价（调用方已把
        # current_tool_call 指向本条目），统一后不再依赖位置巧合
        call_entry = ctx.active_tool_call
        if call_entry is not None:
            call_entry["success"] = success
        # S2-02：交付路径与 headless 路径同口径 —— 超时/被拒/注册项丢失/成功
        # 四种收尾都必须把签名移出「等待审批」集。
        self._pop_permission_pending(ctx, tc)
        # 审查R2修复：pending_delivery 分支 continue 跳过了 POST_TOOL_CALL 链，
        # 真实交付到可见终端执行过的命令在执行层没有任何 executed/failed 审计
        # （该链当前唯一成员 AuditLogger 不拦截，直接触发其落审计即可）。
        await harness.reins.check(ReinPoint.POST_TOOL_CALL, ctx)
        # 追加到消息历史，透传 priority 供 compress_messages 读取（与正常路径一致）
        ctx.messages.append(
            harness.bridle.build_tool_result_message(tc.id, result_text, priority=result.priority)
        )
        # H13 修复 A3：pop span_id（与 tool_call_start 的 push 配对）
        if ctx.span_stack:
            ctx.span_stack.pop()
        ev = TraceEvent(
            kind="tool_call_result",
            source="saddle",
            run_id=ctx.run_id,
            iteration=iteration,
            payload={
                "tool": tc.name,
                "result": result_text,
                "success": success,
                "duration_ms": result.duration_ms,
                "tool_call_id": tc.id,
            },
        )
        await telemetry.emit(ev)
        return ev

    def _filter_tools_by_intention(
        self,
        tools_schema: list[dict[str, Any]],
        intention: Any,
    ) -> list[dict[str, Any]]:
        """根据意图识别结果动态调整可用工具列表

        - CONVERSATION：禁用所有工具，避免问候/身份问题被引导去调工具
        - QUERY / RESEARCH：保持全部工具，由 guide prompt 引导模型主动联网
        - 其他：保持全部工具
        """
        if intention.task_type == TaskType.CONVERSATION:
            logger.info("意图为 CONVERSATION，禁用所有工具 schema")
            return []

        # QUERY / RESEARCH / 其他：均保持全部工具（由 guide prompt 引导，
        # 不在架构层裁剪）
        return tools_schema

    async def _emit_decision_events(
        self,
        telemetry: TelemetryBus,
        decision: ReinDecision,
        ctx: RunContext,
        iteration: int,
    ) -> AsyncIterator[TraceEvent]:
        """发拦截器的所有事件（不修改原事件对象，创建副本设置 iteration）"""
        for ev in decision.events:
            # 创建副本避免修改拦截器内部对象
            ev_copy = TraceEvent(
                kind=ev.kind,
                source=ev.source,
                run_id=ev.run_id,
                phase=ev.phase,
                iteration=iteration,
                payload=ev.payload,
                timestamp=ev.timestamp,
                span_id=ev.span_id,
                parent_span_id=ev.parent_span_id,
            )
            await telemetry.emit(ev_copy)
            yield ev_copy

    async def _emit_done(
        self,
        telemetry: TelemetryBus,
        ctx: RunContext,
        iterations: int,
        final_state: State,
        full_content: str,
        full_reasoning: str,
        harness: "Harness",
    ) -> AsyncIterator[TraceEvent]:
        """发 done 事件"""
        # 失败时回滚所有文件变更（事务性保证）
        if final_state == State.FAILED and ctx.undo_stack:
            logger.info("Agent 失败，回滚 %d 个文件操作", len(ctx.undo_stack))
            await harness.stirrups.undo_all(ctx)
        
        # 成本快照
        cost_snapshot = harness.cost_tracker.snapshot(iterations=iterations).to_dict() if harness.cost_tracker else {}

        done_event = TraceEvent(
            kind="done",
            source="driver",
            run_id=ctx.run_id,
            iteration=iterations,
            payload={
                "run_id": ctx.run_id,
                "content": full_content,
                "reasoning": full_reasoning,
                "iterations": iterations,
                "state": final_state.value,
                "state_history": ctx.state_history,
                "cost": cost_snapshot,
                # spec §8：run 内滚动摘要随 done 出栈，由 routes 落到 agent_runs 行，
                # 否则摘要随 ctx 一起消亡，轨迹回放与 episodic job 都拿不到
                "context_summary": ctx.metadata.get("context_summary") or "",
                # goal 验收闭环：随 done 透出本 run 的目标状态，闭环信息才可见。
                # goal_text=目标原文（无目标为空）；self_checked=收尾时是否做过「对照
                # 成功标准自检」；acknowledged=agent 最终文本是否显式承认"全部达成"
                # （仅关键词粗判，供前端提示；未检出置 None，不强断成败）。
                "workflow_goal": {
                    "goal_text": ctx.metadata.get("_goal_text", ""),
                    "self_checked": bool(ctx.metadata.get("_goal_self_checked")),
                    "acknowledged": _goal_acknowledged(full_content),
                },
            },
        )
        await telemetry.emit(done_event)
        yield done_event


# 文件变更工具集取自 contracts.FILE_MUTATION_TOOLS（唯一源，与预算守卫共用）


def _goal_acknowledged(content: str) -> Optional[bool]:
    """粗判 agent 最终文本是否显式承认目标「全部达成」。

    仅做关键词规约，供 done 事件的 workflow_goal.acknowledged 提示用，**不**作为
    收尾成败的强判据：
      - 命中明确达成措辞（已全部达成/目标已实现/成功标准均已满足 等）→ True
      - 命中明确未达成措辞（尚未达成/仍有未满足/部分达成/还差 等）→ False
      - 均未命中（含糊或无关内容，如未设目标的普通对话）→ None
    """
    if not content:
        return None
    text = content.strip()
    achieved = (
        "全部达成" in text or "全部满足" in text or "均已满足" in text
        or "目标已实现" in text
        or "已全部完成" in text or "已实现目标" in text or "达成目标" in text
    )
    not_achieved = (
        "未达成" in text or "尚未达成" in text or "部分达成" in text
        or "仍有未满足" in text or "未完全达成" in text or "未能达成" in text
    )
    if achieved and not not_achieved:
        return True
    if not_achieved and not achieved:
        return False
    return None


def _build_run_summary(ctx: RunContext) -> str:
    """
    当 Agent 达到最大迭代数却未产生正文时，基于 tool_calls_log 生成一份简短摘要，
    让用户知道 Agent 在这期间做了什么、卡在哪里。
    """
    if not ctx.tool_calls_log:
        return (
            f"Agent 已达到最大迭代数（{ctx.config.max_iterations} 轮），"
            "但未执行任何操作。任务可能过于复杂或初始计划缺失，请尝试拆分任务。"
        )

    tool_counts: dict[str, int] = {}
    mutated: set[str] = set()
    failed_tools: list[str] = []
    for entry in ctx.tool_calls_log:
        tool = entry.get("tool") or "unknown"
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        if tool in FILE_MUTATION_TOOLS and entry.get("success"):
            args = entry.get("args") or {}
            path = args.get("path") or args.get("file_path") or args.get("target") or ""
            if path:
                mutated.add(path)
        if entry.get("success") is False:
            failed_tools.append(tool)

    lines = [
        f"Agent 已达到最大迭代数（{ctx.config.max_iterations} 轮），任务尚未完成。",
        "",
        "已执行操作统计：",
    ]
    for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- {tool}: {count} 次")

    if mutated:
        lines.extend(["", "已修改文件："])
        for path in sorted(mutated):
            lines.append(f"- {path}")

    if failed_tools:
        failed_counts: dict[str, int] = {}
        for t in failed_tools:
            failed_counts[t] = failed_counts.get(t, 0) + 1
        lines.extend(["", "失败工具调用："])
        for t, c in sorted(failed_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {t}: {c} 次")

    lines.extend([
        "",
        "建议：拆分复杂任务，或在设置中提高 Agent 最大迭代数。",
    ])
    return "\n".join(lines)


def _args_to_json(args: dict) -> str:
    """args dict → JSON 字符串（OpenAI 格式）"""
    import json
    try:
        return json.dumps(args, ensure_ascii=False)
    except Exception:
        return "{}"
