"""
侦察子 Agent — 让主 Agent 把「取证型子任务」交给一个受限子 Agent 处理

设计目标（2026-10 升级：无工具纯推理 → 受限侦察）：
- 子 Agent 在**自己的上下文**里执行只读观测工具（contracts.READONLY_TOOLS，
  sys_overview / process_inspect / network_probe / log_read / file_read / …），
  现场取证后只把【结论】回传主 Agent —— 观测原文不进主 Agent 上下文，
  避免主对话被原始输出淹没。
- 治理强制性：子 Agent 每次工具调用都过主 run 的【同一套】Reins（PRE 权限/
  内容级判定 + POST 审计落链）与 Saddle（沙箱），治理引用经
  ToolContext.governance 注入（harness.run 挂到 ctx.metadata["governance"]）。
  治理通道缺失时**拒绝启动**——绝不降级成绕过治理链的免审后门。
- 工具面双重封死：① LLM 只拿到白名单内工具的 schema；② 本地执行前再查一次
  白名单（防御拦截器链被裁剪/未来误配）。
- 无审批通道：子 Agent 无人可点卡片，需要审批的调用（如读 /etc/shadow 被
  FilesystemGuard 判 dangerous）直接按拦截处理，子模型看到拦截原因后自行换路。
- 失败诚实上报（沿用 2026-09 口径）：模型配置取不到 / LLM 失败 / 返回空正文
  一律以异常上抛，绝不把失败说明包装成「分析结果」。
- token 计入主 Agent CostTracker（每次子 LLM 调用都记录）。

适用场景：
1. 「看看这台机器的负载和可疑进程」→ 子 Agent 扇出观测工具，回一段结论
2. 「查一下这个报错在日志里的上下文」→ 子 Agent log_read/file_grep 取证
3. 需要大段原始输出的排查——原始输出留在子上下文，主对话只收结论

注意：子 Agent 使用与主 Agent 相同的 LLM 配置（base_url/api_key/model）。
"""
import asyncio
import json
import logging
import time
from typing import Optional

from agent.tools import BaseTool, ToolResult, ToolMeta, ToolContext
from prompts import SSH_MODE

logger = logging.getLogger(__name__)


# 子 Agent 单轮 LLM 输出上限
_MAX_SUBAGENT_TOKENS = 4096
# ReAct 轮上限：侦察是「几次观测 + 一次结论」的形态，6 轮足够覆盖
# 观测扇出 → 针对性下钻 → 结论；超限走收尾总结（诚实标注未验证项）。
_MAX_SUBAGENT_ITERATIONS = 6
# 单轮 LLM 调用超时
_SUBAGENT_LLM_TIMEOUT = 120.0
# 子 Agent 整体墙钟预算：超过则停止发起新一轮观测，基于已有观测收尾。
# 上限 = 迭代数 × (LLM + 工具) 的合理上界再收紧，防止子任务失控拖死主 run。
_SUBAGENT_TOTAL_BUDGET = 420.0
# 回传主 Agent 的结论截断上限：结论是给主模型读的，不是原始观测回放
_MAX_RESULT_CHARS = 6000


def _args_to_json(args: dict) -> str:
    """工具调用参数序列化（与主 driver 的 assistant 消息回填同一格式）。"""
    try:
        return json.dumps(args or {}, ensure_ascii=False)
    except Exception:
        return "{}"


class SpawnSubagentTool(BaseTool):
    """侦察子 Agent 工具

    让主 Agent 把取证型子任务交给子 Agent：子 Agent 带着只读观测工具在自己的
    上下文里跑轻量 ReAct 循环，回传结论文本。

    使用场景：
    - 多工具观测扇出（系统概览/进程/端口/日志一次采集），原始输出不进主对话
    - 需要多轮「看一眼 → 再下钻」的排查取证
    - 主 Agent 上下文紧张时，把大段输出的分析下沉到子 Agent
    """

    name = "spawn_subagent"
    description = (
        "启动一个侦察子 Agent 处理取证型子任务。子 Agent 在自己的上下文里执行"
        "只读观测工具（系统概览/进程检查/端口探测/日志读取/文件读取/检索等），"
        "多轮取证后只返回结论文本，原始观测输出不会占用主对话上下文。"
        "适合：多工具观测扇出、日志/文件排查、需要大段原始输出分析的任务。"
        "子 Agent 只有只读能力，不能写文件/执行命令/管理服务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "子任务描述，需清晰独立，包含足够上下文让子 Agent 理解要查什么、回什么结论",
            },
            "system_prompt": {
                "type": "string",
                "description": "子 Agent 的角色设定/专长指引。例如：'你是一位 Linux 运维专家，专注于性能瓶颈定位'",
            },
            "extra_context": {
                "type": "string",
                "description": "可选的额外上下文（如报错信息、已知线索等），会拼接到 task 之后",
            },
        },
        "required": ["task", "system_prompt"],
        "additionalProperties": False,
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        cacheable=False,
        # 整体墙钟预算 _SUBAGENT_TOTAL_BUDGET 的声明值（当前无强制消费点，
        # 声明与实现保持一致，供前端展示与将来超时强制使用）
        timeout=_SUBAGENT_TOTAL_BUDGET,
    )

    async def execute(self, context: ToolContext, task: str,
                      system_prompt: str, extra_context: str = "") -> ToolResult:
        """执行侦察子 Agent 调用"""
        if not task.strip():
            return ToolResult(
                False, "", "task 不能为空", priority="high"
            )
        if not system_prompt.strip():
            return ToolResult(
                False, "", "system_prompt 不能为空", priority="high"
            )
        governance = getattr(context, "governance", None)
        reins = getattr(governance, "reins", None) if governance is not None else None
        saddle = getattr(governance, "saddle", None) if governance is not None else None
        if reins is None or saddle is None:
            # 治理通道缺失 = 装配契约被破坏（脱离 harness 直连工具/老调用方）。
            # 拒绝启动而不是降级成纯推理：降级路径就是 2026-09 之前的形态，
            # 而「带工具但不过治理链」正是本次升级要封死的免审后门。
            logger.error("spawn_subagent 缺少治理通道（governance 未注入），拒绝启动")
            return ToolResult(
                False, "",
                "子 Agent 治理通道未注入（governance 缺失），拒绝启动。"
                "请通过 Agent 运行入口（harness.run）使用本工具。",
                priority="high",
            )

        try:
            result_text = await self._run_subagent(
                task=task,
                system_prompt=system_prompt,
                extra_context=extra_context or "",
                tool_ctx=context,
                reins=reins,
                saddle=saddle,
            )
            return ToolResult(
                True,
                result_text,
                priority="normal",
            )
        except Exception as e:
            logger.exception("子 Agent 运行失败")
            return ToolResult(
                False, "", f"子 Agent 运行失败: {e}", priority="high"
            )

    # ──────────────────────────────────────────────────────────────────────

    async def _run_subagent(
        self,
        task: str,
        system_prompt: str,
        extra_context: str,
        tool_ctx: ToolContext,
        reins,
        saddle,
    ) -> str:
        """运行侦察子 Agent — 轻量 ReAct 循环（观测 → 结论）"""
        from agent.harness.contracts import READONLY_TOOLS, AgentConfig, RunContext
        from agent.harness.llm_client import StreamLLMClient

        # 从 DB 获取默认模型配置
        # D 修复（事件循环阻塞）：内部含多次同步 db 查询，放线程池执行
        model_cfg = await asyncio.to_thread(self._get_model_config)
        if not model_cfg:
            # 失败必须以异常上抛：return 一句错误文本会被 execute 包成
            # ToolResult(True)，主 Agent 把失败说明当成子 Agent 的分析结果读进正文。
            raise RuntimeError("[子 Agent] 未找到可用模型配置，无法运行")

        model_id = model_cfg.get("model_id") or model_cfg.get("id", "")
        llm = StreamLLMClient(
            base_url=model_cfg["base_url"],
            api_key=model_cfg["api_key"],
        )

        # 子 Agent 工具面 = 只读观测白名单（contracts.READONLY_TOOLS 单一事实源，
        # 与 driver 并行化/循环检测共用同一判定）。schema 面先收一道口子。
        all_schemas = saddle.schemas() if hasattr(saddle, "schemas") else []
        schemas = [
            s for s in all_schemas
            if (s.get("function") or {}).get("name") in READONLY_TOOLS
        ]

        # 子 RunContext：per-run 状态隔离（tool_calls_log / undo_stack / metadata
        # 全部独立），继承父 run 的 ssh_session_id / environment / conversation_id
        # （侦察同一目标机 + prod 审批强化口径），visible_terminal 强制 False。
        sub_ctx = RunContext.new(
            task=task,
            conversation_id=tool_ctx.conversation_id,
            ssh_session_id=tool_ctx.ssh_session_id,
            environment=tool_ctx.environment,
            visible_terminal=False,
            system_prompt=system_prompt,
            history=[],
            memories=[],
            config=AgentConfig(max_iterations=_MAX_SUBAGENT_ITERATIONS + 1),
        )
        # 拦截器（FilesystemGuard/PermissionGate/AuditLogger 经 resolve_registry）
        # 与审计记录都从 ctx.metadata["saddle"] 取当前 run 的 Saddle
        sub_ctx.metadata["saddle"] = saddle

        messages: list[dict] = [
            {"role": "system", "content": self._subagent_system_prompt(
                system_prompt, has_tools=bool(schemas))},
        ]
        user_content = task
        if extra_context:
            user_content += f"\n\n--- 附加上下文 ---\n{extra_context}"
        messages.append({"role": "user", "content": user_content})

        observations: list[str] = []
        started = time.monotonic()
        final_text = ""
        for iteration in range(_MAX_SUBAGENT_ITERATIONS):
            remaining = _SUBAGENT_TOTAL_BUDGET - (time.monotonic() - started)
            if remaining <= 5.0:
                logger.warning("子 Agent 触达整体预算（%.0fs），停止发起新一轮观测", remaining)
                break
            sub_ctx.iteration = iteration
            sub_ctx.metadata["iteration"] = iteration
            try:
                llm_out = await asyncio.wait_for(
                    llm.call(
                        messages=messages,
                        tools=schemas,
                        model=model_id,
                        temperature=0.5,
                        max_tokens=_MAX_SUBAGENT_TOKENS,
                        enable_thinking=False,
                    ),
                    timeout=min(_SUBAGENT_LLM_TIMEOUT, remaining),
                )
            except asyncio.TimeoutError:
                logger.warning("子 Agent LLM 调用超时（%.0fs）", min(_SUBAGENT_LLM_TIMEOUT, remaining))
                raise RuntimeError("[子 Agent LLM 调用超时]")
            except Exception as e:
                logger.warning("子 Agent LLM 调用失败: %s", e)
                raise RuntimeError(f"[子 Agent LLM 调用失败: {e}]") from e
            self._record_tokens(tool_ctx, llm_out)

            if not llm_out.tool_calls:
                # 无工具调用 = 子模型认为取证完成（或无话可说）
                final_text = (llm_out.content or "").strip()
                if not final_text and not observations:
                    # 首轮就既不观测也不产出 —— 与「模型配置取不到」同一口径：
                    # 失败必须上抛，不许被 execute 包成 ToolResult(True) 混进正文
                    raise RuntimeError("[子 Agent 返回空内容]")
                break

            # assistant(tool_calls) 回填 —— 与主 driver 同一消息格式
            messages.append({
                "role": "assistant",
                "content": llm_out.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": _args_to_json(tc.args)},
                    }
                    for tc in llm_out.tool_calls
                ],
            })
            for tc in llm_out.tool_calls:
                result_text = await self._execute_one(
                    tc, sub_ctx, messages, reins, saddle)
                observations.append(f"[{tc.name}] {result_text}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                })

        if not final_text:
            # 迭代耗尽 / 预算触顶 / 只观测未总结 → 收尾总结调用（tools=[]）。
            # 诚实标注：明确告诉父 Agent 哪些方面没来得及验证，而不是伪装成完整结论。
            final_text = await self._summarize(
                llm, model_id, system_prompt, task, observations, tool_ctx)

        if not final_text:
            # 空结论按失败处理（与「模型配置取不到」同一口径：失败必须上抛，
            # 不许被 execute 包成 ToolResult(True) 混进主 Agent 正文）
            raise RuntimeError("[子 Agent 返回空内容]")

        if len(final_text) > _MAX_RESULT_CHARS:
            head = _MAX_RESULT_CHARS * 2 // 3
            tail = _MAX_RESULT_CHARS - head
            final_text = (
                final_text[:head]
                + f"\n…[子 Agent 结论过长已截断，原文 {len(final_text)} 字符]…\n"
                + final_text[-tail:]
            )
        return final_text

    # ──────────────────────────────────────────────────────────────────────

    async def _execute_one(self, tc, sub_ctx, messages, reins, saddle) -> str:
        """子 Agent 内执行单个工具调用 — 全程过治理链。

        顺序与主 driver 逐口径对齐：登记日志 → PRE 检查（拦截/注入落位）→
        Saddle 执行 → POST 检查（AuditLogger 审计落链）→ 结果文本。
        """
        from agent.harness.contracts import READONLY_TOOLS, ReinPoint

        entry = {
            "tool": tc.name,
            "args": tc.args,
            "iteration": sub_ctx.iteration,
            "success": None,
        }
        sub_ctx.tool_calls_log.append(entry)
        sub_ctx.current_tool_call = entry

        # PRE 检查在前：拦截器链要看到子 Agent 声明的每一次调用（LoopDetector
        # 的窗口计数、审批 pending 记账与主链一致），白名单是执行前的最后一道闸
        pre = await reins.check(ReinPoint.PRE_TOOL_CALL, sub_ctx)
        if pre.events:
            # 子 Agent 无遥测总线：permission_needed 等事件只落日志。
            # 需要审批的调用在这里按拦截处理 —— 侦察无人可点卡片，等下去就是挂死。
            for ev in pre.events:
                logger.info("[子 Agent] PRE 事件(丢弃): %s", getattr(ev, "kind", "?"))
        if pre.inject_messages:
            # LoopDetector 纠偏等注入仍要给子模型看，落位在本工具结果之前
            messages.extend(pre.inject_messages)
        if pre.intercepted:
            entry["success"] = False
            logger.info("[子 Agent] 工具 %s 被 PRE 拦截: %s",
                        tc.name, pre.intercept_reason)
            return f"工具调用被拦截: {pre.intercept_reason}"

        # 白名单兜底：即使拦截器链被裁剪/误配，白名单外的调用也进不了执行
        if tc.name not in READONLY_TOOLS:
            entry["success"] = False
            logger.warning("子 Agent 请求白名单外工具 %s，已拒绝", tc.name)
            return f"[子 Agent 拒绝] 工具 {tc.name} 不在侦察白名单内（仅允许只读观测工具）。"

        result, _undo = await saddle.execute(tc, sub_ctx, auto_rollback=False)

        entry["success"] = result.success
        # POST 检查 — 当前链上唯一成员是 AuditLogger，负责把 executed/failed
        # 写进审计链（与主 run 同一条链，等保口径不因子 Agent 而出现盲区）
        try:
            await reins.check(ReinPoint.POST_TOOL_CALL, sub_ctx)
        except Exception as e:  # 审计链失败不阻塞侦察，但必须留痕
            logger.warning("[子 Agent] POST_TOOL_CALL 检查失败: %s", e)
        sub_ctx.current_tool_call = None

        if getattr(result, "pending_delivery", None):
            # 只读观测工具不产生 pending_delivery（契约与主 driver 并行段一致）；
            # 子 Agent 更没有可见终端通道，出现即按失败如实上报
            entry["success"] = False
            return "工具调用被拦截: 只读观测工具不允许交付可见终端（子 Agent 无该通道）"
        if result.success:
            return result.output or "(空输出)"
        text = f"工具执行失败: {result.error or '未知错误'}"
        if result.output:
            # 失败时带回的部分输出（远程 stderr 等）一并回传给子模型
            text += f"\n--- 部分输出（末尾 2000 字）---\n{result.output[-2000:]}"
        return text

    async def _summarize(self, llm, model_id: str, system_prompt: str,
                         task: str, observations: list, tool_ctx: ToolContext) -> str:
        """收尾总结：迭代耗尽/预算触顶后，基于已收集的观测给结论。"""
        if not observations:
            # 一次观测都没拿到就要结论 —— 没有材料，如实失败
            raise RuntimeError("[子 Agent 未取得任何观测即到达迭代上限]")
        summary_system = (
            f"{system_prompt}\n\n"
            "（侦察迭代数已达上限，停止继续观测。基于下方已收集的观测给出结论，"
            "并明确列出哪些方面尚未验证、建议主 Agent 如何补查。）"
        )
        obs_block = "\n\n".join(observations[-12:])  # 防御：观测过多时取最近的
        try:
            result = await asyncio.wait_for(
                llm.call(
                    messages=[
                        {"role": "system", "content": summary_system},
                        {"role": "user", "content": f"{task}\n\n--- 已收集的观测 ---\n{obs_block}"},
                    ],
                    tools=[],
                    model=model_id,
                    temperature=0.5,
                    max_tokens=_MAX_SUBAGENT_TOKENS,
                    enable_thinking=False,
                ),
                timeout=_SUBAGENT_LLM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("[子 Agent 收尾总结调用超时]")
        except Exception as e:
            raise RuntimeError(f"[子 Agent 收尾总结调用失败: {e}]") from e
        self._record_tokens(tool_ctx, result)
        text = (result.content or "").strip()
        if not text:
            raise RuntimeError("[子 Agent 收尾总结返回空内容]")
        return text

    @staticmethod
    def _subagent_system_prompt(base_prompt: str, has_tools: bool) -> str:
        """子 Agent system prompt — 在主 Agent 给定的角色设定上叠加侦察约束。"""
        lines = [base_prompt.strip()]
        if has_tools:
            lines.append(
                "\n[侦察约束] 你是一个受限侦察 Agent，只能使用提供的只读观测工具"
                "（查看系统状态/进程/端口/日志/文件），没有任何写入或命令执行能力。"
                "请围绕任务做必要的观测后，用一段结构化中文给出结论："
                "关键事实 → 判断依据（引用具体观测数据）→ 不确定项。"
                "原始输出不要整段照抄，提炼与结论相关的部分。"
            )
        else:
            lines.append(
                "\n[侦察约束] 本次未装配观测工具，请直接基于任务描述与附加上下文推理作答。"
            )
        return "\n".join(lines)

    @staticmethod
    def _record_tokens(tool_ctx: ToolContext, llm_out) -> None:
        """子 Agent 的 LLM token 计入主 Agent CostTracker（P2 E6 口径延续）。"""
        tracker = getattr(tool_ctx, "cost_tracker", None)
        if tracker is None or not hasattr(llm_out, "prompt_tokens"):
            return
        try:
            tracker.record_llm_call(
                getattr(llm_out, "prompt_tokens", 0),
                getattr(llm_out, "completion_tokens", 0),
            )
        except Exception as rec_err:
            logger.debug("子 Agent token 记录失败: %s", rec_err)

    def _get_model_config(self) -> Optional[dict]:
        """从 DB 获取默认模型配置

        优先级：
        1. SSH 模式的默认模型
        2. 全局默认模型
        3. 任意一个可用模型
        """
        try:
            import database as db
            # 1. 模式默认 → 2. 全局默认 → 3. 任意可用（list_models_config 返回 list[dict]，取前 10 个）
            model = db.get_default_model(SSH_MODE)
            if not model:
                model = db.get_default_model(None)
            if not model:
                models = db.list_models_config()[:10]
                model = models[0] if models else None
            if not model:
                return None
            # 以上查询的 api_key 均已脱敏，子 Agent 的 LLM 调用需要真实 key，按 id 重取
            cfg_id = model.get("id")
            if cfg_id:
                fresh = db.get_model_config(cfg_id, mask_key=False)
                if not fresh:
                    # 退回那份**脱敏**的 cfg 等于一定拿去发一个必 401 的 key，
                    # 故障点还被推远到「LLM 调用失败」—— 按 id 取不到就是没读到，直说。
                    raise RuntimeError(f"模型配置按 id 读取失败: {cfg_id}")
                return fresh
            return model
        except Exception as e:
            # 「没读到」不许冒充「你没配模型」：原先这里吞成 None，调用方就会报
            # 「未找到可用模型配置」—— 一句对用户设置状态的假话（S11-01 同族）。
            # 现在照抛，由 execute 汇成「子 Agent 运行失败: <真实原因>」。
            logger.warning("获取模型配置失败: %s", e)
            raise
