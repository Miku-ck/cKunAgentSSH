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

import logging
from typing import Any, AsyncIterator

from .contracts import (
    LLMOutput,
    ReinDecision,
    ReinPoint,
    RunContext,
    State,
    ToolCall,
    ToolResult,
    TraceEvent,
    TracePhase,
)
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
                from agent.intention import classify as classify_intention
                intention = classify_intention(ctx.task, max_budget=model_max_reasoning)
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
                    from agent.intention import reclassify
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
                # 修复：使用 "user" 角色而非 "system"，避免添加到消息末尾时触发 400 错误
                ctx.messages.append({
                    "role": "user",
                    "content": (
                        f"[系统提示] 你还剩 {remaining + 1} 轮迭代机会。"
                        "如果任务尚未完成，请直接在下一轮生成当前进度总结和后续建议，"
                        "不要再调用新工具，避免最终没有输出。"
                    ),
                })

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

            # ── Bridle 组装输入 ──
            messages = harness.bridle.assemble(ctx)
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
                    from agent.intention import (
                        get_guide_for_iteration,
                        get_reasoning_budget_for_iteration,
                    )
                    guide = get_guide_for_iteration(intention, iteration)
                    # 动态调整本轮思考预算（首轮保持完整，后续收紧到 1024）
                    # 必须带 model_max_reasoning 钳制：intention.reasoning_budget 是
                    # 未钳制的等级预算，赋给 max_reasoning_tokens 后超限会让下一轮
                    # LLM 调用报 400（classify 路径由上面的 min() 防住，这里曾漏防）
                    ctx.config.max_reasoning_tokens = get_reasoning_budget_for_iteration(
                        intention, iteration, max_budget=model_max_reasoning,
                    )
                    # 修复：引导语使用 "user" 角色而非 "system"，避免添加到消息末尾时触发 400 错误
                    if guide:
                        messages.append({"role": "user", "content": f"[意图引导] {guide}"})
                except Exception as e:
                    logger.warning("意图引导注入失败（跳过）: %s", e)

            # ── pre_llm_call ──
            await harness.reins.check(ReinPoint.PRE_LLM_CALL, ctx)

            # ── THINK: Stirrups 调 LLM ──
            try:
                llm_out = await harness.stirrups.call_llm(ctx, messages, tools_schema)
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
                # 注入提示：告诉 LLM 还有工具未执行，下轮继续
                # 修复：使用 "user" 角色而非 "system"，避免添加到消息末尾时触发 400 错误
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
                    from agent.intention import detect_batch_reading, get_progressive_gathering_guide
                    if detect_batch_reading(llm_out.tool_calls, threshold=3):
                        guide = get_progressive_gathering_guide(iteration)
                        # 修复：使用 "user" 角色而非 "system"，避免添加到消息末尾时触发 400 错误
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
        """
        # ── ACT: 执行工具 ──
        await ctx.transition(State.ACTING, event="need_tool")

        for tc in llm_out.tool_calls:
            # 记录到 ctx.tool_calls_log（供 Reins 检查）
            ctx.tool_calls_log.append({
                "tool": tc.name,
                "args": tc.args,
                "iteration": iteration,
                "success": None,  # 待执行
            })

            # ── pre_tool_call（Reins 检查）──
            pre_tool_decision = await harness.reins.check(ReinPoint.PRE_TOOL_CALL, ctx)
            if pre_tool_decision.intercepted:
                # 拦截
                for ev in pre_tool_decision.events:
                    await telemetry.emit(ev)
                    yield ev
                result_text = f"工具调用被拦截: {pre_tool_decision.intercept_reason}"
                ctx.tool_calls_log[-1]["success"] = False
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
                    break
                continue

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
                continue
            ctx.tool_calls_log[-1]["success"] = result.success
            # C-review M7：命令执行成功（审批已放行）→ 移出「等待审批」集合，
            # 同命令后续重发恢复正常循环检测
            if result.success:
                pending = ctx.metadata.get("permission_pending")
                if pending:
                    try:
                        from .reins import tool_call_signature
                        pending.pop(tool_call_signature(
                            {"tool": tc.name, "args": tc.args}), None)
                    except Exception:
                        pass

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
                ctx.tool_calls_log[-1]["success"] = False
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

        # 1. tool_deliver 事件
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
            },
        )
        await telemetry.emit(deliver_ev)
        yield deliver_ev

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
                result_text += f"\n[exit_code={reg.exit_code}]"
            ev = await self._finish_visible(
                ctx, harness, telemetry, result, tc, iteration,
                result_text=result_text, success=reg.success,
            )
            yield ev
        finally:
            # 无论成功/超时/异常/外部取消（CancelledError），都清理注册项防泄漏
            PendingDeliveries.remove(tool_id)

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
        ctx.tool_calls_log[-1]["success"] = success
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
        from agent.intention import TaskType

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
            },
        )
        await telemetry.emit(done_event)
        yield done_event


# 会修改文件系统的工具名（用于 run 摘要 mutated 统计）。
# 必须是 builtin 实际注册名 —— work 模式旧名（write_file/patch_file 等）
# 已随全库清理移除，残留旧名会让摘要统计静默失效（2026-09 修复）。
_FILE_MUTATION_TOOLS = frozenset({
    "file_write", "file_edit",
})


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
        if tool in _FILE_MUTATION_TOOLS and entry.get("success"):
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
