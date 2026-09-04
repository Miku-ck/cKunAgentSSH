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
from typing import Any, AsyncIterator, Optional

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

        # C-review M8 修复：run 开始时清空遗留 once 授权（原实现在每轮迭代
        # 结束时清空，与审批存在时序竞态：permission_needed 发出后、本轮
        # 迭代结束前用户点"仅本次"，授权被静默清掉，下一轮重发仍被拦 →
        # "点了允许却仍被拦"）。现在 once 授权在消费时移除
        # （PermissionGate.check_permission 计数递减），此处仅负责防上个
        # run 的过期授权泄漏进新 run。
        try:
            from .reins import _get_global_permission_gate_sync
            _get_global_permission_gate_sync().clear_once()
        except Exception as e:
            logger.warning("clear_once 失败: %s", e)

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
                    ctx.config.max_reasoning_tokens = get_reasoning_budget_for_iteration(
                        intention, iteration,
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
                # 完成前强制校验：若存在未修复的语法错误，注入并强制再迭代
                syntax_errors = ctx.metadata.get("syntax_errors") or []
                if syntax_errors:
                    # 按文件路径去重（保留最新错误）
                    seen_paths: dict[str, dict] = {}
                    for e in syntax_errors:
                        seen_paths[e["path"]] = e
                    error_list = "\n".join(
                        f"  - {e['path']}: {e['error']}" for e in seen_paths.values()
                    )
                    # 修复：使用 "user" 角色而非 "system"，避免添加到消息末尾时触发 400 错误
                    ctx.messages.append({
                        "role": "user",
                        "content": (
                            f"[验证门] 任务尚未通过验证，以下文件存在语法错误：\n{error_list}\n"
                            f"请先修复上述语法错误，确保所有文件可运行后再结束任务。"
                        ),
                    })
                    # 不清空 syntax_errors：仅在对应文件被重新编辑并通过验证后由 SyntaxVerifier 移除
                    # 跳过 ACTING/OBSERVING 状态转移，直接进入下一轮 THINKING
                    await ctx.transition(State.THINKING, event="verification_gate")
                    continue
                else:
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
                        read_names = [tc.name for tc in llm_out.tool_calls]
                        guide = get_progressive_gathering_guide(read_names, iteration)
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
            ctx.tool_calls_log[-1]["success"] = result.success
            # C-review M7：命令执行成功（审批已放行）→ 移出「等待审批」集合，
            # 同命令后续重发恢复正常循环检测
            if result.success:
                pending = ctx.metadata.get("permission_pending")
                if pending:
                    try:
                        from .reins import LoopDetector
                        pending.pop(LoopDetector._signature(
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

        if intention.task_type in (TaskType.QUERY, TaskType.RESEARCH):
            net_tool_names = {"web_search", "web_fetch"}
            available = {
                schema.get("function", {}).get("name")
                for schema in tools_schema
                if schema.get("type") == "function"
            }
            has_net = bool(net_tool_names & available)
            logger.info(
                "意图为 %s，联网工具可用: %s",
                intention.task_type.value, has_net,
            )

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
        
        # 成功完成时清理磁盘备份
        if final_state == State.COMPLETED:
            try:
                from agent.harness.saddle import cleanup_run_backups
                cleanup_run_backups(ctx.run_id)
            except Exception as e:
                logger.warning("清理备份失败: %s", e)

        # 成本快照
        cost_snapshot = harness.cost_tracker.snapshot(iterations=iterations).to_dict() if harness.cost_tracker else {}

        # 文件变更摘要
        file_changes_summary: dict[str, Any] = {}
        try:
            from agent.file_changes import get_file_changes_summary
            file_changes_summary = get_file_changes_summary(ctx.run_id)
        except Exception:
            pass

        # 文件变更验证 footer：检测失败的文件变更工具调用，防止 over-claiming
        verifier_footer = _build_file_mutation_footer(ctx)
        if verifier_footer:
            # 先发 content 事件，让 footer 流入内容流（前端自动追加显示）
            footer_event = TraceEvent(
                kind="content",
                source="driver",
                run_id=ctx.run_id,
                iteration=iterations,
                payload={"content": verifier_footer},
            )
            await telemetry.emit(footer_event)
            yield footer_event
            full_content += verifier_footer

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
                "file_changes": file_changes_summary,
                "verifier_footer": verifier_footer or None,
            },
        )
        await telemetry.emit(done_event)
        yield done_event


# 会修改文件系统的工具名（用于 file-mutation verifier footer 检测）
_FILE_MUTATION_TOOLS = frozenset({
    "write_file", "patch_file", "edit_file", "create_file",
    "delete_file", "move_file", "rename_file",
})


def _build_file_mutation_footer(ctx: RunContext) -> str:
    """
    构建文件变更验证 footer

    扫描本次运行中失败的文件变更工具调用，生成 advisory 文本。
    防止模型 over-claiming：声称已修改文件但实际工具返回失败。
    """
    failed: list[str] = []
    for entry in ctx.tool_calls_log:
        if entry.get("tool") in _FILE_MUTATION_TOOLS and entry.get("success") is False:
            args = entry.get("args") or {}
            path = args.get("path") or args.get("file_path") or args.get("target") or "<unknown>"
            failed.append(f"  - {entry['tool']}({path})")

    if not failed:
        return ""

    return (
        "\n\n---\n[Verification] 以下文件变更可能未生效（工具返回失败）：\n"
        + "\n".join(failed)
        + "\n请手动检查上述文件。"
    )


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
