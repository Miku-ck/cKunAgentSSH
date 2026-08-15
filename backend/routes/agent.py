"""
Agent 路由 — SSE 流式 Agent 运行 + Agent 运行历史查询

POST /api/agent/run    — 启动 Agent，SSE 流式返回运行过程
GET  /api/agent/runs   — 查询 Agent 运行历史
GET  /api/agent/tools  — 查询当前可用工具列表
GET  /api/agent/memories — 查询 Agent 三层记忆
DELETE /api/agent/memories/{id} — 删除单条记忆
GET  /api/agent/strategies — 查询反思/演化策略
POST /api/agent/evolve  — 手动触发策略演化
GET  /api/agent/tick    — 查询意识 tick 状态
POST /api/agent/tick    — 手动触发意识 tick
"""
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import database as db
from database import get_model_config as _db_get_model_config_raw
from sse_utils import sse_event
from agent.tools import create_registry_for_mode, ToolRegistry
from agent.memory import get_memory_manager
from agent.consciousness_tick import get_global_tick
from agent.reflection import MetaReflection, RunTrace
from agent.evolution import EvolutionEngine
from agent.cost_tracker import CostTracker
from prompts import _current_time_str
from safe_tokens import safe_max_tokens
# Harness 框架（P8：切换到 Harness）
from agent.harness import Harness, HarnessConfig
from agent.harness.contracts import AgentConfig as HarnessAgentConfig
from agent.harness.llm_client import StreamLLMClient
from agent.harness.telemetry import SSEAdapter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])

HEARTBEAT_INTERVAL = 15


def _get_model_config(model_id: str):
    """获取模型配置（含真实 api_key）"""
    return _db_get_model_config_raw(model_id, mask_key=False)


async def _run_reflection_and_evolution(
    run_id: str,
    task: str,
    mode: str,
    final_state: str,
    iterations: int,
    tool_calls: list[dict],
    final_content: str,
    cost_usd: float = 0.0,
    total_tokens: int = 0,
    state_history: Optional[list[dict]] = None,
) -> None:
    """
    事后反思 + 策略演化（异步任务，不阻塞 SSE 流）

    1. 构建 RunTrace + 调用 MetaReflection.reflect()
    2. 保存反思结果到 agent_strategies 表（含 cost_usd/iterations/total_tokens）
    3. 加载近期反思 + 调用 EvolutionEngine.evolve()
    4. 保存演化后的配置（如有调整）

    H12: 新增 cost_usd / total_tokens / state_history 参数，
    供 MetaReflection 多维反思（成本维度 + 状态转移异常检测）使用，
    并持久化到 agent_strategies 表供 EvolutionEngine 多维演化读取。
    """
    try:
        reflection_mgr = MetaReflection()
        evolution_mgr = EvolutionEngine()

        # 构建 RunTrace（含成本/状态历史）
        trace = RunTrace(
            run_id=run_id,
            task=task,
            mode=mode,
            final_state=final_state,
            iterations=iterations,
            tool_calls=tool_calls,
            cost_usd=cost_usd,
            total_tokens=total_tokens,
            state_history=state_history or [],
        )
        result = reflection_mgr.reflect(trace)
        reflection_mgr.save_reflection(
            result, mode=mode, task=task,
            cost_usd=cost_usd, iterations=iterations, total_tokens=total_tokens,
        )
        logger.info(
            "反思完成 (run_id=%s, score=%.2f, cost=$%.3f): %s",
            run_id, result.score, cost_usd, result.summary,
        )

        # 加载近 10 条反思，触发演化
        recent = reflection_mgr.load_recent_reflections(mode, limit=10)
        if len(recent) >= 3:  # 至少 3 条反思才演化
            evolved = evolution_mgr.evolve(mode=mode, recent_reflections=recent)
            if evolved.adjustments:
                evolution_mgr.save_evolved_config(evolved, mode=mode)
                logger.info(
                    "策略演化完成 (mode=%s, adjustments=%d): %s",
                    mode, len(evolved.adjustments), "; ".join(evolved.adjustments),
                )
    except Exception as e:
        logger.warning("反思/演化失败: %s", e, exc_info=True)


# ── Helper Functions ──

def _build_agent_system_prompt(
    req: "AgentRunRequest",
    reflection_injection: str,
    is_conversation_task: bool,
) -> str:
    """构建 Agent 系统提示（时间注入 + 模式角色 + 用户 prompt + 反思 + 任务引导）"""
    from prompts import MODE_PROMPTS

    # 开头注入当前时间（解决 LLM 训练数据截止导致日期/星期回答过时的问题）
    time_injection = (
        f"[系统时间] 当前时间：{_current_time_str()}。"
        f"如用户询问日期/星期/时间，请以本信息为准，不要使用训练数据中的旧时间。"
    )

    # 拼装：时间 + 模式基础 prompt + 用户 system_prompt + 反思
    mode_prompt = MODE_PROMPTS.get(req.mode, "")
    parts = [time_injection]
    if mode_prompt:
        parts.append(mode_prompt)
    if req.system_prompt:
        parts.append(req.system_prompt)
    if reflection_injection:
        parts.append(reflection_injection)

    if is_conversation_task:
        # 对话型任务：跳过工具效率/任务拆分/子Agent/输出格式等大段引导
        parts.append(
            "本任务为纯对话型问题，请直接简洁回答，不要调用任何工具，不要做长篇规划。"
            "回答应基于你自身的身份和能力，避免引用无关上下文或记忆。"
        )
    else:
        # 工具使用效率指引：减少无意义重复读取和循环调用
        parts.append(
            "工具使用效率：读取文件后请基于返回内容直接推理和修改，避免在短时间内多次读取同一文件。"
            "如需查看大文件的不同位置，请优先使用 read_file 的 start_line/end_line 参数读取相关片段，"
            "而不是重新读取整个文件。"
        )
        # 任务拆分指引：鼓励 Agent 把大任务拆成可独立验证的子任务
        parts.append(
            "任务拆分：面对复杂任务（涉及多文件修改、多步骤验证、跨模块重构等），请先在首轮输出一个简短的"
            "子任务清单（用编号列表），然后逐个完成并验证。每完成一个子任务就明确标注"
            "'✓ 子任务 N 完成'，便于追踪进度。若某个子任务卡住，先跳过并继续后续子任务，"
            "最后统一汇报未完成项。这样即使迭代数耗尽，也能保证部分成果交付。"
        )
        # 子 Agent 指引：告知可用 spawn_subagent 工具委派独立子任务
        parts.append(
            "子 Agent：你可以使用 spawn_subagent 工具把独立子任务（如代码审查、方案设计、文档生成、"
            "测试用例生成等）交给一个子 Agent 处理。子 Agent 是一次无工具的 LLM 推理调用，"
            "适合做可独立完成的推理类任务。传入清晰的 task 和 system_prompt（角色设定），"
            "可选 extra_context 提供代码片段等上下文。子 Agent 返回结果后，你负责整合并执行后续操作。"
            "合理使用子 Agent 可以避免主上下文被细节淹没，提高复杂任务的执行效率。"
        )
        # 输出格式约束：避免中英文混排、过程性内容污染最终报告
        parts.append(
            "输出格式：在工具调用迭代过程中保持输出简洁，避免把每一步的内心独白、英文碎碎念或执行日志写入正文。"
            "完成所有操作后，用结构化中文报告总结：先给总体结论，再用 ## 二级标题分节，每个修复点独立成段，"
            "代码用代码块包裹。标题、列表、代码块前后必须换行，确保 Markdown 渲染正确。"
        )

    return "\n\n".join(parts)


def _resolve_evolution_config(
    req: "AgentRunRequest",
    model_cfg: dict,
    safe_max: int,
    evolution_mgr: "EvolutionEngine",
) -> dict:
    """解析演化配置，返回 effective thinking/max_iter/temperature/max_tokens"""
    try:
        evolved_cfg = evolution_mgr.load_latest_evolved_config(req.mode)
        if evolved_cfg:
            # 用户未显式 enable_thinking 时，应用演化值
            thinking = req.enable_thinking or evolved_cfg.get("enable_thinking", False)
            # H11：应用演化后的 max_iterations（学到的最优值），用户值作为兜底
            evolved_iter = evolved_cfg.get("max_iterations")
            max_iter = evolved_iter if evolved_iter is not None else req.max_iterations
            # 演化值覆盖模型配置默认值
            evolved_temp = evolved_cfg.get("temperature")
            temperature = evolved_temp if evolved_temp is not None else model_cfg.get("temperature", 0.7)
            # H13：应用演化后的 max_tokens
            evolved_max_tokens = evolved_cfg.get("max_tokens")
            max_tokens = evolved_max_tokens if evolved_max_tokens else safe_max
        else:
            thinking = req.enable_thinking
            max_iter = req.max_iterations
            temperature = model_cfg.get("temperature", 0.7)
            max_tokens = safe_max
    except Exception as e:
        logger.warning("加载演化配置失败: %s", e)
        thinking = req.enable_thinking
        max_iter = req.max_iterations
        temperature = model_cfg.get("temperature", 0.7)
        max_tokens = safe_max

    return {
        "thinking": thinking,
        "max_iter": max_iter,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


# ── Request Models ──


class AgentRunRequest(BaseModel):
    """Agent 运行请求"""
    task: str = Field(..., min_length=1, max_length=50000)
    conversation_id: str
    model_id: str = ""
    mode: str = "ssh"
    system_prompt: Optional[str] = None
    ssh_session_id: Optional[str] = None
    enable_thinking: bool = False
    enable_tools: bool = True
    max_iterations: int = 100
    skill_ids: list[str] = Field(default_factory=list)
    # 意图识别 + 思考预算控制（默认开启）
    # 关闭时降级为旧行为：无 thinking 预算限制、无规划阶段、无引导语注入
    enable_intention_guide: bool = True
    # 动态步数预算开关（可由前端设置面板控制）
    enable_dynamic_budget: bool = True


# ── Routes ──


@router.post("/agent/run")
async def agent_run(req: AgentRunRequest, request: Request):
    """启动 Agent — SSE 流式返回运行过程"""
    # ── 按 mode 选择默认模型（兜底）──
    effective_model_id = req.model_id
    if not effective_model_id:
        default_model = db.get_default_model(req.mode)
        if default_model:
            effective_model_id = default_model.get("model_id") or default_model.get("id")
    if not effective_model_id:
        default_model = db.get_default_model(None)
        if default_model:
            effective_model_id = default_model.get("model_id") or default_model.get("id")
    if not effective_model_id:
        raise HTTPException(status_code=400, detail=f"未找到 mode={req.mode} 的默认模型，请先配置模型")

    req.model_id = effective_model_id

    # 获取模型配置
    model_cfg = _get_model_config(req.model_id)
    if not model_cfg:
        raise HTTPException(status_code=404, detail=f"模型不存在: {req.model_id}")

    # 懒启动后台意识 tick — 仅在首次 Agent 运行时触发，
    # 避免纯聊天用户产生空 DB 查询开销
    get_global_tick().ensure_started()

    # ── 按 mode 过滤显式 skill_ids ──
    if req.skill_ids:
        disabled_skill_ids = set(db.list_disabled_skills_for_mode(req.mode))
        original_skill_ids = req.skill_ids
        req.skill_ids = [sid for sid in req.skill_ids if sid not in disabled_skill_ids]
        if len(req.skill_ids) != len(original_skill_ids):
            logger.info(
                "Agent 显式 skill_ids 按 mode=%s 过滤: %d -> %d",
                req.mode, len(original_skill_ids), len(req.skill_ids),
            )

    # 获取历史消息
    history = db.get_recent_messages(req.conversation_id, limit=20)

    # ── 意图预识别：用于决定是否跳过记忆检索、是否禁用工具 schema ──
    # 对话型任务（问候/身份询问/能力询问）不需要记忆、不需要工具
    # 避免用户问"你是什么模型"时被无关记忆污染、被引导去调工具
    pre_intention = None
    is_conversation_task = False
    if req.enable_intention_guide:
        try:
            from agent.intention import classify as _classify_intention
            pre_intention = _classify_intention(req.task)
            is_conversation_task = (
                pre_intention.task_type.value == "conversation"
            )
        except Exception as e:
            logger.warning("意图预识别失败（降级为常规流程）: %s", e)

    # 初始化记忆管理器（缓存单例，与当前模型共享 base_url/api_key 用于 embedding）
    memory_mgr = get_memory_manager(
        base_url=model_cfg["base_url"],
        api_key=model_cfg["api_key"],
        embedding_model=model_cfg.get("model_id") or model_cfg.get("name", ""),
    )

    # 读取记忆设置 — 关闭时跳过检索与保存
    mem_settings = db.get_memory_settings()
    memory_enabled = mem_settings["enabled"]

    # 三层记忆检索：episodic + semantic 合并
    # 对话型任务跳过记忆检索（避免无关记忆污染简单对话）
    memory_contents: list[str] = []
    if memory_enabled and not is_conversation_task:
        try:
            # M1 修复：加超时保护，防止 embedding API 阻塞导致 SSE 连接挂起无数据
            results = await asyncio.wait_for(
                memory_mgr.retrieve(
                    query=req.task, mode=req.mode, top_k=5, threshold=0.3
                ),
                timeout=30.0,
            )
            memory_contents = [r.memory.content for r in results if r.memory.content]
        except asyncio.TimeoutError:
            logger.warning("记忆检索超时（30s），降级为空记忆继续运行")
        except Exception as e:
            logger.warning("记忆检索失败（降级为空记忆继续运行）: %s", e)

        # 兜底：若三层记忆为空，仍取旧的简单记忆表
        if not memory_contents:
            memory_contents = [m["content"] for m in db.get_relevant_memories(req.mode, limit=5)]

    # ── 加载历史反思 + 演化配置 ──
    reflection_mgr = MetaReflection()
    evolution_mgr = EvolutionEngine()

    # 加载近 3 条反思策略，注入 system prompt
    try:
        recent_reflections = reflection_mgr.load_recent_reflections(req.mode, limit=3)
        reflection_injection = reflection_mgr.build_prompt_injection(recent_reflections)
    except Exception as e:
        logger.warning("加载反思策略失败（降级为无反思）: %s", e)
        reflection_injection = ""

    # M3 修复：提取 system prompt 构建为独立函数
    final_system_prompt = _build_agent_system_prompt(
        req, reflection_injection, is_conversation_task
    )

    # 应用演化配置（仅覆盖用户未显式传入的字段）
    # M3 修复：提取演化配置解析为独立函数
    _prompt_chars = (
        len(req.task or "")
        + len(final_system_prompt or "")
        + sum(len(m.get("content", "")) for m in history)
        + sum(len(m) for m in memory_contents)
    )
    _safe_max = safe_max_tokens(
        model_cfg.get("max_tokens"),
        _prompt_chars,
        model_cfg.get("max_output_tokens"),
    )
    _evo = _resolve_evolution_config(req, model_cfg, _safe_max, evolution_mgr)
    effective_thinking = _evo["thinking"]
    effective_max_iter = _evo["max_iter"]
    effective_temperature = _evo["temperature"]
    effective_max_tokens = _evo["max_tokens"]

    # 生成 run_id（与旧 AgentEngine.run_id 格式一致：8 位 hex）
    import uuid as _uuid
    run_id = _uuid.uuid4().hex[:8]

    # ── P8: 创建 Harness 实例 ──
    harness = Harness(HarnessConfig(
        base_url=model_cfg["base_url"],
        api_key=model_cfg["api_key"],
        model=model_cfg.get("model_id") or model_cfg["id"],
        max_context_tokens=model_cfg.get("max_tokens", 16384) or 16384,
    ))
    # LLM 客户端
    llm_client = StreamLLMClient(
        base_url=model_cfg["base_url"],
        api_key=model_cfg["api_key"],
    )
    # 工具注册表（按模式创建；enable_tools=False 时用空注册表）
    # 对话型任务强制禁用工具 schema：用户问"你是什么模型"时不应看到 read_file/write_file 等工具
    # 否则 LLM 会被工具 schema 引导去调用工具，产生无关内容
    effective_enable_tools = req.enable_tools and not is_conversation_task
    tool_registry = create_registry_for_mode(req.mode) if effective_enable_tools else ToolRegistry()
    # 成本追踪器
    cost_tracker = CostTracker(model_id=model_cfg.get("model_id") or model_cfg["id"])
    # 组装
    harness.assemble(
        llm_client=llm_client,
        tool_registry=tool_registry,
        cost_tracker=cost_tracker,
    )

    # 持久化 agent_run 记录
    db.create_agent_run(
        run_id=run_id,
        conversation_id=req.conversation_id,
        task=req.task,
        max_iterations=req.max_iterations,
        mode=req.mode,
        model_used=model_cfg.get("model_id") or model_cfg["id"],
    )

    # 保存用户消息到 db（修复：原 Agent 模式未保存用户消息，导致 post_process 的标题生成检查 len(recent)==2 失败，
    # 且历史会话切换时丢失用户问题）
    db.add_message(
        req.conversation_id,
        "user",
        req.task,
        model_used=model_cfg.get("model_id") or model_cfg["id"],
    )
    db.touch_conversation(req.conversation_id)
    db.update_conversation_status(req.conversation_id, "in_progress")

    # 收集运行轨迹（用于结束后生成情景记忆）
    run_trace_tool_calls: list[dict] = []

    async def event_stream():
        """SSE 事件流（C7 修复：使用 Queue 多路复用 heartbeat 和 engine 事件）"""
        queue: asyncio.Queue = asyncio.Queue()
        # H10 修复：待更新的 tool_call_id 队列（按 tool_call_start 顺序匹配 tool_call_result）
        pending_tool_call_ids: list[str] = []

        # ── C7 修复：heartbeat 作为普通协程，往 queue 投递心跳标记 ──
        async def heartbeat_producer():
            try:
                while True:
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                    await queue.put({"__heartbeat__": True})
            except asyncio.CancelledError:
                pass

        # ── engine 事件生产者（P8: 使用 Harness）──
        async def engine_producer():
            try:
                # P8: 通过 Harness 运行，TraceEvent → SSE dict
                harness_config = HarnessAgentConfig(
                    max_iterations=effective_max_iter,
                    # 修复：应用演化后的 temperature（原实现硬编码 model_cfg.get("temperature", 0.7)）
                    temperature=effective_temperature,
                    # H13 修复 C7：应用演化后的 max_tokens（原实现硬编码 _safe_max）
                    max_tokens=effective_max_tokens,
                    enable_thinking=effective_thinking,
                    enable_tools=effective_enable_tools,
                    enable_intention_guide=req.enable_intention_guide,
                    enable_dynamic_budget=req.enable_dynamic_budget,
                )
                async for trace_event in harness.run(
                    task=req.task,
                    mode=req.mode,
                    conversation_id=req.conversation_id,
                    workspace_path="",
                    ssh_session_id=req.ssh_session_id,
                    system_prompt=final_system_prompt,
                    history=history,
                    memories=memory_contents,
                    config=harness_config,
                    run_id=run_id,
                    skill_ids=req.skill_ids,
                ):
                    # M2 修复：检测客户端断连后立即停止 producer，避免空转浪费算力
                    if await request.is_disconnected():
                        logger.info("SSE 客户端断连，engine_producer 主动停止")
                        break
                    # TraceEvent → 前端 SSE dict（SSEAdapter 保持协议兼容）
                    sse_dict = SSEAdapter.to_sse_dict(trace_event)
                    # 强制注入 run_id（Harness 内部 ctx.run_id 与 run_id 一致）
                    sse_dict["run_id"] = run_id
                    await queue.put(sse_dict)
            except Exception as e:
                logger.exception("Agent 运行异常 (engine_producer)")
                err_msg = f"{type(e).__name__}: {str(e) or '无错误信息'}"
                await queue.put({"type": "error", "message": err_msg})
            finally:
                await queue.put(None)  # sentinel

        heartbeat_task = asyncio.create_task(heartbeat_producer())
        engine_task = asyncio.create_task(engine_producer())

        total_content = ""
        total_reasoning = ""

        # H14 修复：立即发送 state=thinking 事件，让前端马上显示"思考中..."提示
        # 原问题：从 fetch 发起到 driver 发出第一个 state 事件之间可能等 2-5 秒
        # （后端预处理：意图识别 + 记忆检索 embedding + 反思加载 + LLM 首 token）
        # 这期间前端 UI 完全空白，用户以为卡住了
        yield sse_event({"type": "state", "state": "thinking", "iteration": 0, "run_id": run_id})

        try:
            while True:
                event = await queue.get()
                # sentinel → 结束
                if event is None:
                    break
                # 心跳
                if event.get("__heartbeat__"):
                    yield ": heartbeat\n\n"
                    continue

                # 累积内容
                if event.get("type") == "content":
                    total_content += event["content"]
                elif event.get("type") == "reasoning":
                    total_reasoning += event["reasoning"]
                elif event.get("type") == "tool_call_start":
                    # 记录工具调用开始
                    tc_id = db.add_tool_call(
                        agent_run_id=run_id,
                        iteration=event.get("iteration", 0),
                        tool_name=event["tool"],
                        arguments=json.dumps(event.get("args", {}), ensure_ascii=False),
                    )
                    # H10 修复：保存 id 以便 tool_call_result 时回写结果
                    pending_tool_call_ids.append(tc_id)
                    run_trace_tool_calls.append({
                        "tool": event["tool"],
                        "args": event.get("args", {}),
                        "iteration": event.get("iteration", 0),
                    })
                elif event.get("type") == "tool_call_result":
                    # H10 修复：将工具结果回写到 tool_calls 表
                    if pending_tool_call_ids:
                        tc_id = pending_tool_call_ids.pop(0)
                        try:
                            result_str = event.get("result", "")
                            if not isinstance(result_str, str):
                                result_str = json.dumps(result_str, ensure_ascii=False)
                            # m1 修复：截断时标记，避免 Agent 拿到不完整数据做出错误决策
                            truncated = len(result_str) > 5000
                            db.update_tool_call(
                                tool_call_id=tc_id,
                                result=result_str[:5000] + ("\n...[truncated]" if truncated else ""),
                                success=event.get("success", True),
                                duration_ms=event.get("duration_ms", 0),
                            )
                        except Exception as e:
                            logger.warning("更新 tool_call 结果失败: %s", e)
                    # 回写 run_trace_tool_calls（用于 agent_trace_snapshot 持久化）
                    # 反向查找同 tool + 同 iteration + 无 result 的条目
                    event_iter = event.get("iteration", 0)
                    for tc_entry in reversed(run_trace_tool_calls):
                        if (tc_entry.get("tool") == event.get("tool")
                                and tc_entry.get("iteration", 0) == event_iter
                                and "result" not in tc_entry):
                            tc_entry["result"] = event.get("result", "")
                            tc_entry["success"] = event.get("success", True)
                            break
                elif event.get("type") == "done":
                    # 更新 agent_run 状态
                    final_state = event.get("state", "completed")
                    # P7: 提取成本快照
                    cost_data = event.get("cost") or {}
                    try:
                        db.update_agent_run(
                            run_id=run_id,
                            status=final_state,
                            iteration=event.get("iterations", 0),
                            state_history=json.dumps(event.get("state_history", []), ensure_ascii=False),
                            total_tokens=cost_data.get("total_tokens", 0),
                            prompt_tokens=cost_data.get("prompt_tokens", 0),
                            completion_tokens=cost_data.get("completion_tokens", 0),
                            cost_usd=cost_data.get("cost_usd", 0.0),
                            duration_ms=cost_data.get("duration_ms", 0),
                            tool_call_count=cost_data.get("tool_call_count", 0) or len(run_trace_tool_calls),
                        )
                        # H14 修复：构造 AgentTraceSnapshot（无论是否有正文都构造）
                        # 原实现只在 total_content 非空时保存消息，导致运行失败且无正文输出时
                        # 文件变更记录丢失，用户完全看不到 Agent 修改了什么
                        file_changes_summary = event.get("file_changes") or {}
                        has_file_changes = (file_changes_summary.get("files_changed") or 0) > 0
                        agent_trace_snapshot = {
                            # H14 修复：human_help 状态在前端显示为 failed（兼容现有 UI），
                            # 但保留 currentState = human_help 供组件识别为"需要人工介入"
                            "status": "failed" if final_state in ("failed", "human_help") else "completed",
                            "currentState": final_state,
                            "iterations": event.get("iterations", 0),
                            "duration": round((cost_data.get("duration_ms", 0) or 0) / 1000, 1),
                            "toolCalls": run_trace_tool_calls,
                            "reasoning": total_reasoning or "",
                            "lastRunId": run_id,
                            "fileChanges": file_changes_summary,
                            "costInfo": {
                                "total_tokens": cost_data.get("total_tokens", 0),
                                "prompt_tokens": cost_data.get("prompt_tokens", 0),
                                "completion_tokens": cost_data.get("completion_tokens", 0),
                                "cost_usd": cost_data.get("cost_usd", 0.0),
                                "duration_ms": cost_data.get("duration_ms", 0),
                                "tool_call_count": cost_data.get("tool_call_count", 0) or len(run_trace_tool_calls),
                            },
                            "errorMessage": None,
                        }

                        # 存储最终消息：有正文 或 有文件变更 时都需要保存
                        # 无正文但有变更时，生成一段摘要作为 content，避免消息为空
                        content_to_save = total_content
                        if not content_to_save and has_file_changes:
                            changes = file_changes_summary.get("changes", [])
                            lines = ["Agent 运行未产生正文输出，但已修改以下文件："]
                            for c in changes:
                                op = c.get("operation", "modify")
                                op_cn = {"create": "新建", "modify": "修改", "delete": "删除"}.get(op, op)
                                lines.append(f"- [{op_cn}] {c.get('file_path', c.get('abs_path', ''))}")
                            content_to_save = "\n".join(lines)

                        if content_to_save or has_file_changes:
                            db.add_message(
                                conversation_id=req.conversation_id,
                                role="assistant",
                                content=content_to_save,
                                reasoning=total_reasoning if total_reasoning else None,
                                model_used=model_cfg.get("model_id") or model_cfg["id"],
                                tokens_used=cost_data.get("total_tokens", 0),
                                run_id=run_id,
                                agent_trace=agent_trace_snapshot,
                            )
                            # 异步后处理 — 通过 PostProcessManager 统一管理
                            # （超时保护 + 生命周期管理 + 配置化启停）
                            try:
                                existing_msgs = db.get_messages(req.conversation_id, limit=1000)
                                turn_index = max(0, len([m for m in existing_msgs if m.get("role") == "user"]) - 1)
                            except Exception:
                                turn_index = 0

                            from post_processor import get_post_process_manager
                            await get_post_process_manager().submit(
                                mode=req.mode,
                                conversation_id=req.conversation_id,
                                user_message=req.task,
                                assistant_content=total_content,
                                assistant_reasoning=total_reasoning,
                                model_id=model_cfg.get("model_id") or model_cfg["id"],
                                turn_index=turn_index,
                            )
                    except Exception as e:
                        logger.warning("done 事件处理失败: %s", e)

                    # 保存情景记忆（运行总结）— 仅成功完成时保存
                    # 对话型任务跳过记忆保存（"你是什么模型"这种问题不需要进记忆库污染后续检索）
                    if (
                        final_state == "completed"
                        and total_content
                        and memory_enabled
                        and not is_conversation_task
                    ):
                        try:
                            summary = memory_mgr.build_episodic_summary(
                                task=req.task,
                                tool_calls=run_trace_tool_calls,
                                final_content=total_content,
                                iterations=event.get("iterations", 0),
                            )
                            await memory_mgr.save_episodic(
                                mode=req.mode,
                                content=summary,
                                source_conv_id=req.conversation_id,
                                agent_run_id=run_id,
                                importance=0.6,
                            )
                            logger.info("已保存情景记忆 (run_id=%s)", run_id)
                        except Exception as e:
                            logger.warning("保存情景记忆失败: %s", e)

                    # ── 事后反思 + 策略演化（异步执行，不阻塞 SSE；超时保护防卡死） ──
                    # H12: 传递 cost_usd / total_tokens / state_history，
                    # 供 MetaReflection 做成本维度反思 + 状态转移异常检测，
                    # 并持久化供 EvolutionEngine 多维演化读取
                    reflect_task = asyncio.create_task(
                        asyncio.wait_for(
                            _run_reflection_and_evolution(
                                run_id=run_id,
                                task=req.task,
                                mode=req.mode,
                                final_state=final_state,
                                iterations=event.get("iterations", 0),
                                tool_calls=run_trace_tool_calls,
                                final_content=total_content,
                                cost_usd=float(cost_data.get("cost_usd", 0.0) or 0.0),
                                total_tokens=int(cost_data.get("total_tokens", 0) or 0),
                                state_history=event.get("state_history", []) or [],
                            ),
                            timeout=120.0,
                        )
                    )
                    reflect_task.add_done_callback(_task_exception_logger("reflection_evolution"))

                elif event.get("type") == "error":
                    # engine 报错时更新 agent_run 状态为 failed
                    try:
                        db.update_agent_run(
                            run_id=run_id,
                            status="failed",
                        )
                        # H13 修复 C1：error 分支也触发反思（失败运行不应错过反思）
                        # 原实现只在 done 事件分支触发反思，error 事件分支跳过，
                        # 导致 LLM 调用失败 / engine_producer 异常的 run 没有反思数据
                        reflect_task = asyncio.create_task(
                            asyncio.wait_for(
                                _run_reflection_and_evolution(
                                    run_id=run_id,
                                    task=req.task,
                                    mode=req.mode,
                                    final_state="failed",
                                    iterations=0,  # error 时迭代数未知
                                    tool_calls=run_trace_tool_calls,
                                    final_content=total_content,
                                ),
                                timeout=120.0,
                            )
                        )
                        reflect_task.add_done_callback(_task_exception_logger("reflection_error"))
                    except Exception:
                        pass

                # file_changes 已由 Driver._emit_done 注入，无需重复

                yield sse_event(event)

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Agent 运行异常 (event_stream)")
            # 异常时将 agent_run 状态标记为 failed
            try:
                db.update_agent_run(run_id=run_id, status="failed")
            except Exception:
                pass
            yield sse_event({"type": "error", "message": str(e)})
            yield "data: [DONE]\n\n"
        finally:
            heartbeat_task.cancel()
            engine_task.cancel()
            try:
                from agent.file_changes import persist_file_changes
                persist_file_changes(run_id)
            except Exception:
                pass
            try:
                from agent.file_changes import clear_file_changes
                clear_file_changes(run_id)
            except Exception:
                pass
            try:
                from agent.harness.saddle import cleanup_run_backups
                cleanup_run_backups(run_id)
            except Exception:
                pass
            # 清理 TelemetryBus 事件缓存（防止内存泄漏）
            try:
                harness.telemetry.clear()
            except Exception:
                pass
            # 清理 todo_store（防止内存泄漏）
            try:
                from agent.tools.builtin.todo_write import clear_todos
                clear_todos(run_id)
            except Exception:
                pass
            # 客户端断开时将 agent_run 标记为 failed（若仍为 running/init）
            try:
                run = db.get_agent_run(run_id)
                if run and run.get("status") in ("running", "init"):
                    db.update_agent_run(run_id=run_id, status="failed")
            except Exception:
                pass

    # H8 修复：fire-and-forget 任务的异常日志回调
    def _task_exception_logger(name: str):
        def _cb(task: asyncio.Task):
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                logger.warning("后台任务 [%s] 异常: %s", name, exc, exc_info=exc)
        return _cb

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/agent/runs")
async def list_agent_runs(conversation_id: Optional[str] = None, limit: int = Query(20, ge=1, le=500)):
    """查询 Agent 运行历史"""
    runs = db.get_agent_runs(conversation_id=conversation_id, limit=limit)
    return {"runs": runs}


@router.get("/agent/runs/{run_id}")
async def get_agent_run_detail(run_id: str):
    """查询单次 Agent 运行详情（含工具调用日志）"""
    run = db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent 运行不存在")
    tool_calls = db.get_tool_calls(run_id)
    return {"run": run, "tool_calls": tool_calls}


@router.get("/agent/tools")
async def list_tools(mode: str = "ssh"):
    """查询当前模式可用的工具列表（含启用状态）"""
    import database as db
    # 获取所有工具类的元信息（包括未注册的，用于显示被禁用的工具）
    from agent.tools.builtin import get_all_tool_classes
    all_tools_info = get_all_tool_classes(mode)

    tools = []
    disabled_list = db.list_disabled_tools(mode)
    for tool_cls in all_tools_info:
        # 实例化以读取 name/description/parameters（这些可能是实例属性或类属性）
        instance = tool_cls()
        tools.append({
            "name": instance.name,
            "description": instance.description,
            "parameters": instance.parameters,
            "risk_level": instance.meta.risk_level,
            "mode_scope": instance.meta.mode_scope,
            "enabled": instance.name not in disabled_list,
            # 透传工具元数据，供前端展示
            "timeout": instance.meta.timeout,
            "cacheable": instance.meta.cacheable,
            "reversible": instance.meta.reversible,
        })
    return {"mode": mode, "tools": tools}


class UpdateToolEnabledRequest(BaseModel):
    enabled: bool


@router.patch("/agent/tools/{tool_name}/enabled")
async def update_tool_enabled(tool_name: str, req: UpdateToolEnabledRequest, mode: str = "all"):
    """更新工具启用状态（默认 mode=all 全局开关）"""
    import database as db
    result = db.upsert_tool_config(tool_name, mode, req.enabled)
    return result


# ── Agent 权限管理 ──


class PermissionGrantRequest(BaseModel):
    tool: str = Field(..., description="工具名")
    scope: str = Field("always", description="授权范围：always（永久）/ session（会话）")


@router.post("/agent/permission/grant")
async def grant_tool_permission(req: PermissionGrantRequest):
    """授权工具

    scope=always: 写入 ~/.ckunagentic/allowlist.txt，所有后续 run 放行
    scope=session: 进程级单例 _confirmed_session，跨 run 共享，进程重启失效（H13 修复 A5）
    """
    from agent.harness.reins import get_global_permission_gate
    if req.scope not in ("always", "session"):
        raise HTTPException(status_code=400, detail="scope 必须是 always 或 session")

    # H13 修复 A5：session scope 改用进程级单例（原 501 限制移除）
    pg = await get_global_permission_gate()
    pg.grant(req.tool, scope=req.scope)
    return {"tool": req.tool, "scope": req.scope, "granted": True}


@router.post("/agent/permission/revoke")
async def revoke_tool_permission(tool: str):
    """撤销工具授权（从永久 allowlist 和 session scope 移除）"""
    from agent.harness.reins import get_global_permission_gate
    pg = await get_global_permission_gate()
    pg.revoke(tool)
    return {"tool": tool, "granted": False}


@router.get("/agent/permission/list")
async def list_tool_permissions():
    """查询已永久授权的工具列表"""
    from agent.harness.reins import get_global_permission_gate
    pg = await get_global_permission_gate()
    return {"tools": sorted(pg._confirmed_permanent)}


# ── Agent 记忆管理 ──


@router.get("/agent/memories")
async def list_agent_memories(
    mode: Optional[str] = None,
    mem_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    """查询 Agent 三层记忆列表（mode 可选，不传则返回全部模式）"""
    rows = db.get_agent_memories(mode=mode, mem_type=mem_type, limit=limit)
    for r in rows:
        r.pop("embedding", None)
    return {"memories": rows, "count": len(rows)}


@router.delete("/agent/memories/{memory_id}")
async def delete_agent_memory(memory_id: str):
    """删除单条 Agent 记忆"""
    ok = db.delete_agent_memory(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": True, "id": memory_id}


@router.post("/agent/memories/prune")
async def prune_agent_memories(
    mode: Optional[str] = None,
    mem_type: Optional[str] = None,
    max_count: int = Query(200, ge=1, le=10000),
    clear_all: bool = Query(False),
):
    """清理最旧最少使用的 Agent 记忆

    - clear_all=True: 删除所有匹配的记忆（忽略 max_count）
    - clear_all=False: 保留最近 max_count 条，删除其余
    """
    if clear_all:
        deleted = db.clear_all_memories(mode=mode, mem_type=mem_type)
    else:
        deleted = db.prune_agent_memories(mode=mode, mem_type=mem_type, max_count=max_count)
    return {"deleted": deleted}


# ── 意识 tick ──


@router.post("/agent/tick")
async def trigger_consciousness_tick():
    """手动触发一次意识 tick（用于调试/UI 集成）"""
    tick = get_global_tick()
    result = await tick.trigger_now()
    return {"tick": result}


@router.get("/agent/tick/status")
async def consciousness_tick_status():
    """查询意识 tick 状态"""
    tick = get_global_tick()
    return {
        "running": tick._task is not None and not tick._task.done(),
        "interval_seconds": tick.config.interval_seconds,
        "last_tick": tick.last_tick_result,
    }


# ── 策略（反思 + 演化） ──


@router.get("/agent/strategies")
async def list_agent_strategies(
    mode: Optional[str] = None,
    strategy_type: Optional[str] = None,
    limit: int = 50,
):
    """查询 Agent 策略列表（reflection / evolved_config）"""
    rows = db.get_agent_strategies(
        mode=mode, strategy_type=strategy_type, limit=limit
    )
    return {"strategies": rows, "count": len(rows)}


@router.get("/agent/strategies/{strategy_id}")
async def get_agent_strategy_detail(strategy_id: str):
    """查询单条策略详情"""
    s = db.get_agent_strategy(strategy_id)
    if not s:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"strategy": s}


@router.patch("/agent/strategies/{strategy_id}/enabled")
async def toggle_strategy_enabled(strategy_id: str, enabled: int = 1):
    """启用/禁用单条策略"""
    ok = db.update_agent_strategy_enabled(strategy_id, enabled=int(enabled))
    if not ok:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"updated": True, "id": strategy_id, "enabled": int(enabled)}


@router.delete("/agent/strategies/{strategy_id}")
async def delete_agent_strategy_route(strategy_id: str):
    """删除单条策略"""
    ok = db.delete_agent_strategy(strategy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"deleted": True, "id": strategy_id}


@router.post("/agent/evolve")
async def trigger_evolution(mode: str = "ssh"):
    """
    手动触发策略演化
    基于近期反思结果演化 Agent 配置，返回演化后的配置
    """
    reflection_mgr = MetaReflection()
    evolution_mgr = EvolutionEngine()

    recent = reflection_mgr.load_recent_reflections(mode, limit=10)
    if not recent:
        return {
            "evolved": False,
            "reason": "无反思策略可演化，请先运行若干次 Agent 任务",
            "reflections_count": 0,
        }

    evolved = evolution_mgr.evolve(mode=mode, recent_reflections=recent)
    saved_id = None
    if evolved.adjustments:
        saved_id = evolution_mgr.save_evolved_config(evolved, mode=mode)

    return {
        "evolved": bool(evolved.adjustments),
        "saved_id": saved_id,
        "reflections_count": len(recent),
        "config": {
            "max_iterations": evolved.max_iterations,
            "temperature": evolved.temperature,
            "enable_thinking": evolved.enable_thinking,
            "evolved_from_runs": evolved.evolved_from_runs,
            "avg_score": evolved.avg_score,
        },
        "adjustments": evolved.adjustments,
    }


# ── Trace Viewer + 成本追踪 ──


@router.get("/agent/runs/{run_id}/trace")
async def get_agent_run_trace(run_id: str):
    """
    获取 Agent 运行完整轨迹（供 Trace Viewer 可视化）
    包含: run 元信息 + 工具调用时间线 + 状态历史 + 成本统计
    """
    run = db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent 运行不存在")

    tool_calls = db.get_tool_calls(run_id)

    # 解析 state_history
    state_history = []
    sh_raw = run.get("state_history")
    if sh_raw:
        try:
            state_history = json.loads(sh_raw) if isinstance(sh_raw, str) else sh_raw
        except (json.JSONDecodeError, TypeError):
            state_history = []

    # 计算成本明细
    from agent.cost_tracker import get_model_pricing
    model_used = run.get("model_used") or ""
    in_price, out_price = get_model_pricing(model_used)

    # 文件变更清单（C 部分新增）
    from agent.file_changes import get_file_changes_summary
    file_changes = get_file_changes_summary(run_id)

    return {
        "run": {
            "id": run.get("id"),
            "conversation_id": run.get("conversation_id"),
            "task": run.get("task"),
            "mode": run.get("mode"),
            "model_used": model_used,
            "status": run.get("status"),
            "iteration": run.get("iteration"),
            "max_iterations": run.get("max_iterations"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
        },
        "cost": {
            "prompt_tokens": run.get("prompt_tokens", 0),
            "completion_tokens": run.get("completion_tokens", 0),
            "total_tokens": run.get("total_tokens", 0),
            "cost_usd": run.get("cost_usd", 0.0),
            "duration_ms": run.get("duration_ms", 0),
            "tool_call_count": run.get("tool_call_count", 0),
            "pricing": {
                "input_per_1m": in_price,
                "output_per_1m": out_price,
                "model": model_used,
            },
        },
        "state_history": state_history,
        "tool_calls": [
            {
                "id": tc.get("id"),
                "iteration": tc.get("iteration"),
                "tool_name": tc.get("tool_name"),
                "arguments": tc.get("arguments"),
                "result": tc.get("result"),
                "success": bool(tc.get("success", 1)),
                "duration_ms": tc.get("duration_ms"),
                "created_at": tc.get("created_at"),
            }
            for tc in tool_calls
        ],
        "file_changes": file_changes,
    }


@router.get("/agent/runs/{run_id}/diff")
async def get_run_file_diff(run_id: str, abs_path: str = Query(..., description="文件绝对路径")):
    """
    获取某次 Agent 运行中某文件的具体 unified diff

    前端 ChangeReviewCard 点击文件时调用此接口，展示具体改动内容。
    """
    from agent.file_changes import get_file_diff
    diff = get_file_diff(run_id, abs_path)
    if not diff:
        return {"ok": False, "message": "未找到该文件的变更记录（可能已被清理）"}
    return {"ok": True, "diff": diff}


def _scan_run_backups(run_id: str) -> dict[str, str]:
    """扫描某次运行的磁盘备份，返回 abs_path -> backup_path 映射"""
    from agent.harness.saddle import _get_backup_root
    import os
    backup_root = _get_backup_root()
    run_dir = os.path.join(backup_root, run_id)
    if not os.path.isdir(run_dir):
        return {}
    mapping: dict[str, str] = {}
    for name in os.listdir(run_dir):
        if name.endswith(".meta"):
            continue
        meta_path = os.path.join(run_dir, name + ".meta")
        if not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                abs_path = f.read().strip()
            if abs_path:
                mapping[abs_path] = os.path.join(run_dir, name)
        except Exception:
            continue
    return mapping


@router.post("/agent/runs/{run_id}/undo")
async def undo_agent_run_changes(run_id: str):
    """
    撤销某次 Agent 运行的所有文件变更

    - 根据 agent_file_changes 表判断 create/modify/delete
    - 优先使用磁盘备份恢复原始内容
    - 清理该 run 的磁盘备份并删除 DB 记录
    """
    run = db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent 运行不存在")

    from agent.file_changes import load_file_changes, clear_file_changes
    changes = load_file_changes(run_id) or db.get_agent_file_changes(run_id)
    if not changes:
        return {"undone": False, "reason": "无文件变更记录", "restored": [], "deleted": []}

    backups = _scan_run_backups(run_id)
    restored: list[str] = []
    deleted: list[str] = []
    failed: list[str] = []

    import os
    import shutil

    for c in changes:
        abs_path = c.get("abs_path") or c.get("file_path", "")
        operation = c.get("operation", "modify")
        if not abs_path:
            continue
        try:
            if operation == "create":
                # 新建的文件 → 删除
                if os.path.exists(abs_path):
                    os.remove(abs_path)
                deleted.append(abs_path)
            elif operation == "delete":
                # 删除的文件 → 从备份恢复
                backup = backups.get(abs_path)
                if backup and os.path.exists(backup):
                    os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
                    shutil.copy2(backup, abs_path)
                    restored.append(abs_path)
                elif os.path.exists(abs_path):
                    # 备份丢失但文件仍在，跳过
                    restored.append(abs_path)
                else:
                    failed.append(abs_path)
            else:
                # modify → 从备份恢复
                backup = backups.get(abs_path)
                if backup and os.path.exists(backup):
                    os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
                    shutil.copy2(backup, abs_path)
                    restored.append(abs_path)
                elif not os.path.exists(abs_path):
                    failed.append(abs_path)
                else:
                    # 备份丢失但文件仍在，无法回滚到修改前，记录为失败
                    failed.append(abs_path)
        except Exception as e:
            logger.warning("撤销文件变更失败 %s: %s", abs_path, e)
            failed.append(abs_path)

    # 清理备份和 DB 记录
    try:
        from agent.harness.saddle import cleanup_run_backups
        cleanup_run_backups(run_id)
    except Exception:
        pass
    try:
        db.delete_agent_file_changes(run_id)
        clear_file_changes(run_id)
    except Exception:
        pass

    return {
        "undone": len(failed) == 0,
        "restored": restored,
        "deleted": deleted,
        "failed": failed,
    }


@router.post("/agent/runs/{run_id}/keep")
async def keep_agent_run_changes(run_id: str):
    """
    保留某次 Agent 运行的所有文件变更

    - 标记 DB 记录为已接受
    - 清理该 run 的磁盘备份
    """
    run = db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent 运行不存在")

    try:
        db.accept_agent_file_changes(run_id)
    except Exception as e:
        logger.warning("标记文件变更为已接受失败: %s", e)

    try:
        from agent.harness.saddle import cleanup_run_backups
        cleanup_run_backups(run_id)
    except Exception:
        pass
    try:
        from agent.file_changes import clear_file_changes
        clear_file_changes(run_id)
    except Exception:
        pass

    return {"kept": True, "run_id": run_id}


@router.get("/agent/stats")
async def get_agent_cost_stats(mode: Optional[str] = None, days: int = Query(7, ge=1, le=365)):
    """
    获取 Agent 成本/耗时统计聚合

    Args:
        mode: 模式过滤（work/ide/ssh，None=所有）
        days: 最近 N 天（默认 7）
    """
    stats = db.get_agent_run_stats(mode=mode, days=days)
    # 兜底字段（旧库可能返回 None）
    for k in ("total_tokens", "prompt_tokens", "completion_tokens",
              "total_duration_ms", "total_tool_calls"):
        if stats.get(k) is None:
            stats[k] = 0
    for k in ("total_cost_usd", "avg_cost_usd", "avg_iterations"):
        if stats.get(k) is None:
            stats[k] = 0.0
    return {
        "filter": {"mode": mode, "days": days},
        "stats": stats,
    }
