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
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

import database as db
from routes.terminal import _get_model_config
from sse_utils import sse_event
from agent.tools import create_registry_for_mode, ToolRegistry
from agent.memory import get_memory_manager
from agent.consciousness_tick import get_global_tick
from agent.reflection import MetaReflection, RunTrace
from agent.evolution import EvolutionEngine
from agent.cost_tracker import CostTracker
from agent.pending_delivery import PendingDeliveries
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
    # 审查 M-13：反思/演化全程是同步 DB 读写，整块放线程池执行，
    # 避免后台任务在事件循环线程里阻塞其他请求
    def _work() -> None:
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

    try:
        await asyncio.to_thread(_work)
    except Exception as e:
        logger.warning("反思/演化失败: %s", e, exc_info=True)


# ── Helper Functions ──

def _build_agent_system_prompt(
    req: "AgentRunRequest",
    reflection_injection: str,
    is_conversation_task: bool,
    host_profile_text: str = "",
    is_change: Optional[bool] = None,
) -> str:
    """构建 Agent 系统提示

    拼装顺序：时间 → 模式角色 → 目标机环境画像 → 用户 prompt → 反思
              → 运维方法论 → 工具/输出引导

    Args:
        host_profile_text: 目标机环境画像（agent.host_profile 产出）；空串不注入
        is_change: 任务是否可能改变目标机状态。None 表示未做意图识别，
            此时按"变更"处理 —— 方向性取舍见下方注释。
    """
    from prompts import MODE_PROMPTS, OPS_METHOD_CHANGE, OPS_METHOD_READONLY

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
    # 目标机环境事实：紧跟角色设定，使"这是台什么机器"先于任何命令生成进入上下文。
    # 无 SSH 会话时该文案会显式声明"运维工具不在本机执行"，避免模型误以为可本地动手。
    # 对话型任务不注入（不调工具，环境事实无意义）——即便调用方传了也在此拦掉。
    if host_profile_text and not is_conversation_task:
        parts.append(host_profile_text)
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
        # 运维方法论：变更类任务与只读任务的处置方式不同（见 prompts.py）。
        # is_change=None（未做意图识别）时按"变更"处理 —— 两条误判路径代价不对称：
        # 对只读任务多说一段影响说明，顶多啰嗦；对变更任务注入"本任务不改变目标机状态"
        # 则是给出错误前提。OPS_METHOD_CHANGE 第 0 条本身要求模型在纯只读时自行降级。
        parts.append(
            OPS_METHOD_READONLY if is_change is False else OPS_METHOD_CHANGE
        )
        # 工具使用效率指引：优先专用运维工具，减少 shell 拼命令与重复读取
        parts.append(
            "工具使用效率：优先使用专用工具而非 shell_exec 拼命令——读文件用 file_read、"
            "改文件用 file_edit、搜内容用 file_grep、找文件用 file_glob、看日志用 log_read、"
            "查进程用 process_inspect、看资源用 sys_overview、探端口用 network_probe、"
            "管服务用 service_manage。基于返回内容直接推理，避免短时间内重复读取同一内容；"
            "查看大文件的不同位置时优先指定 offset/limit 读取相关片段。"
        )
        # 长任务指引：>60s 的任务必须走后台，禁止 shell_exec 同步死等
        parts.append(
            "长任务规则：预计执行超过 60 秒的任务（大文件传输、构建、批量巡检、等待类操作）"
            "必须用 job_run(target='remote') 后台启动，再用 job_output(job_id, wait=10~30) "
            "轮询输出，需要停止时用 job_kill。不要用 shell_exec 同步等待（超时上限 120 秒）。"
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
    # E 修复：mode 白名单校验（Pydantic Literal，非法值 → 422），
    # 防止任意 mode 串绕过注册表/权限边界
    mode: Literal["ssh"] = "ssh"
    system_prompt: Optional[str] = None
    ssh_session_id: Optional[str] = None
    # 目标环境标记（test/prod）— 驱动生产环境高风险命令的审计与审批强化
    environment: Literal["test", "prod"] = "test"
    # 可见终端模式：True 时 shell_exec 命令交付到可见终端（前端 PTY/SSH）而非 headless 执行
    visible_terminal: bool = False
    enable_thinking: bool = False
    enable_tools: bool = True
    # E 修复：max_iterations 边界校验（1-500），防 LLM/恶意请求传 0 或天文数字
    max_iterations: int = Field(100, ge=1, le=500)
    skill_ids: list[str] = Field(default_factory=list)
    # 是否启用触发词自动技能匹配（前端"自动技能匹配"开关，默认开启）
    auto_skill_match: bool = True
    # 历史上下文条数（前端"上下文窗口"设置，1-100）
    context_window: int = Field(20, ge=1, le=100)
    # 意图识别 + 思考预算控制（默认开启）
    # 关闭时降级为旧行为：无 thinking 预算限制、无规划阶段、无引导语注入
    enable_intention_guide: bool = True
    # 动态步数预算开关（可由前端设置面板控制）
    enable_dynamic_budget: bool = True
    # C-review M10：成本/token 硬熔断（None=内置默认 $2/3M tokens；0=禁用；>0=自定义）
    max_cost_usd: Optional[float] = Field(None, ge=0, description="累计成本上限（USD），超出即终止 run")
    max_total_tokens: Optional[int] = Field(None, ge=0, description="累计 token 上限，超出即终止 run")


# ── Routes ──

# C-review MINOR-12：同会话并发 run 防护。
# 原实现同一 conversation 可并发多个 run：消息交错写入、授权互相消费、
# 备份/文件互覆。这里用进程级 {conversation_id: run_id} 标记进行中的 run，
# 请求时原子地 check-and-set，事件流 finally 中释放。
_in_flight_runs: dict[str, str] = {}
_in_flight_lock = asyncio.Lock()


async def _acquire_run_slot(conversation_id: str, run_id: str) -> Optional[str]:
    """原子地为 conversation 占位。返回 None 表示成功占位；返回已有 run_id 表示冲突。

    自愈：占位者的 DB 行已存在且处于终态（completed/failed — cleanup finally
    被取消打断的陈旧残留）时，记日志并接管占位，避免会话被永久锁死。
    注意：DB 行缺失不算陈旧（占位先于 create_agent_run，新 run 可能有毫秒级
    窗口还没有行）— 误接管活 run 的代价高于多等一次。
    """
    async with _in_flight_lock:
        existing = _in_flight_runs.get(conversation_id)
        if existing:
            stale = False
            status_text = ""
            try:
                row = await asyncio.to_thread(db.get_agent_run, existing)
                if row is not None:
                    status_text = row.get("status", "")
                    stale = row.get("status") in ("completed", "failed")
            except Exception:
                stale = False
            if not stale:
                return existing
            logger.warning(
                "[agent] 释放陈旧 run 占位: conversation=%s stale_run=%s status=%s",
                conversation_id, existing, status_text,
            )
            del _in_flight_runs[conversation_id]
        _in_flight_runs[conversation_id] = run_id
        return None


def _release_run_slot_sync(conversation_id: str, run_id: str) -> None:
    """同步释放占位（无 await，取消安全）。

    仅当当前占位者仍是本 run 时删除，避免误删后续 run 的占位。
    asyncio 单线程事件循环内无 await 点 = 无交错可能，dict 操作等价原子。
    故障根因修复：客户端断连触发任务 cancel 时，finally 里带 await 的
    释放会被 CancelledError（BaseException）打断 → 占位永久残留。
    """
    if _in_flight_runs.get(conversation_id) == run_id:
        del _in_flight_runs[conversation_id]


async def _release_run_slot(conversation_id: str, run_id: str) -> None:
    """释放占位（兼容旧调用；实际为无 await 的同步操作）"""
    _release_run_slot_sync(conversation_id, run_id)


_AGENT_RUNS_PRUNE_INTERVAL_S = 6 * 3600  # agent_runs/tool_calls 保留清理节流（每 6h）
_last_agent_runs_prune_at = 0.0


async def _maybe_prune_agent_runs() -> None:
    """低频清理 agent_runs（连带级联 tool_calls），每 6h 最多一次，best-effort。

    放在 SSE 事件流 finally：run 结束是低频活动点，空转不碰 DB；to_thread
    避免同步 sqlite 占用事件循环；shield 保证 finally 被取消（断连）时清理仍跑完。
    参照 schedule_engine._maybe_prune_runs 的节流形态。
    """
    global _last_agent_runs_prune_at
    now = time.monotonic()
    if now - _last_agent_runs_prune_at < _AGENT_RUNS_PRUNE_INTERVAL_S:
        return
    _last_agent_runs_prune_at = now
    try:
        await asyncio.shield(asyncio.to_thread(db.prune_agent_runs))
    except Exception:
        logger.warning("agent_runs 保留清理失败（best-effort，不阻塞）", exc_info=True)
    # 会话保留清理：用户数据，仅当【显式开启】才执行（默认关闭 → 不碰）
    try:
        retention = await asyncio.shield(asyncio.to_thread(_get_conv_retention_setting))
        if retention.get("enabled"):
            await asyncio.shield(asyncio.to_thread(db.prune_conversations, retention.get("keep_count", 1000)))
    except Exception:
        logger.warning("会话保留清理失败（best-effort，不阻塞）", exc_info=True)


def _get_conv_retention_setting() -> dict:
    """读取会话保留设置（默认关闭）。与 routes/settings.py 的 key 保持一致。"""
    enabled = (db.get_app_setting("conv_retention.enabled", "false") or "false").lower() == "true"
    raw = db.get_app_setting("conv_retention.keep_count")
    try:
        keep = int(raw) if raw is not None else 1000
    except (ValueError, TypeError):
        keep = 1000
    return {"enabled": enabled, "keep_count": keep}


_startup_reconciled = False


async def _reconcile_stale_runs_once() -> None:
    """C-review MINOR-12：启动对账（懒执行，进程内仅一次）。

    崩溃/重启后内存防护丢失，崩溃时进行中的 run 在 DB 里永久滞留 'running'。
    首个 Agent 请求时把它们标记 failed。用懒执行而非 on_event 注册，
    避免与 main.py 的 lifespan 钩子相互干扰（main.py 由并行开发维护）。
    """
    global _startup_reconciled
    if _startup_reconciled:
        return
    _startup_reconciled = True
    try:
        n = await asyncio.to_thread(db.mark_stale_agent_runs_failed)
        if n:
            logger.warning("启动对账: %d 条滞留 running 的 agent_runs 已标记 failed", n)
    except Exception as e:
        logger.warning("启动对账失败（不影响本次运行）: %s", e)


@router.post("/agent/run")
async def agent_run(req: AgentRunRequest, request: Request):
    """启动 Agent — SSE 流式返回运行过程"""
    # C-review MINOR-12：进程启动后首次请求做滞留 run 对账
    await _reconcile_stale_runs_once()
    # ── 按 mode 选择默认模型（兜底）──
    # 审查 M-13：db 同步调用放线程池，避免阻塞事件循环（卡住所有并发请求/心跳）
    effective_model_id = req.model_id
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, req.mode)
        if default_model:
            # 审查外热修：同 chat.py — 优先行主键 id（原优先 LLM 模型名会 404 模型不存在）
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, None)
        if default_model:
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        raise HTTPException(status_code=400, detail=f"未找到 mode={req.mode} 的默认模型，请先配置模型")

    req.model_id = effective_model_id

    # 获取模型配置
    model_cfg = await asyncio.to_thread(_get_model_config, req.model_id)
    if not model_cfg:
        raise HTTPException(status_code=404, detail=f"模型不存在: {req.model_id}")

    # 懒启动后台意识 tick — 仅在首次 Agent 运行时触发，
    # 避免纯聊天用户产生空 DB 查询开销
    get_global_tick().ensure_started()

    # ── 按 mode 过滤显式 skill_ids ──
    if req.skill_ids:
        disabled_skill_ids = set(await asyncio.to_thread(db.list_disabled_skills_for_mode, req.mode))
        original_skill_ids = req.skill_ids
        req.skill_ids = [sid for sid in req.skill_ids if sid not in disabled_skill_ids]
        if len(req.skill_ids) != len(original_skill_ids):
            logger.info(
                "Agent 显式 skill_ids 按 mode=%s 过滤: %d -> %d",
                req.mode, len(original_skill_ids), len(req.skill_ids),
            )

    # 获取历史消息（条数来自前端"上下文窗口"设置）
    history = await asyncio.to_thread(db.get_recent_messages, req.conversation_id, limit=req.context_window)

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
    mem_settings = await asyncio.to_thread(db.get_memory_settings)
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
            _fallback_mems = await asyncio.to_thread(db.get_relevant_memories, req.mode, limit=5)
            memory_contents = [m["content"] for m in _fallback_mems]

    # ── 加载历史反思 + 演化配置 ──
    reflection_mgr = MetaReflection()
    evolution_mgr = EvolutionEngine()

    # 加载近 3 条反思策略，注入 system prompt
    try:
        # 审查 M-13：反思策略读取也是同步 DB 查询
        recent_reflections = await asyncio.to_thread(
            reflection_mgr.load_recent_reflections, req.mode, limit=3,
        )
        reflection_injection = reflection_mgr.build_prompt_injection(recent_reflections)
    except Exception as e:
        logger.warning("加载反思策略失败（降级为无反思）: %s", e)
        reflection_injection = ""

    # ── 目标机环境画像（只读探测，失败静默降级）──
    # 一条只读、免审的探测命令（ops_obs.host_profile_cmd ↔ 远端 host_profile 模板），
    # 把"这台机器是什么环境"沉淀为已知事实，避免模型凭训练数据印象对 Ubuntu 发 yum、
    # 对非 systemd 机发 systemctl。探测失败 / 超时 / 目标机未部署该模板一律降级为
    # "环境未知"文案，绝不影响 run（见 agent/host_profile.py 硬约束）。
    # 对话型任务跳过：纯寒暄不需要一次远程往返，也不需要环境事实。
    # 无 SSH 会话时不探测，直接返回"未连接主机"文案。
    # 代价：setup 期多一次 await（超时上限 10s，实际为亚秒级只读命令）；setup 本身
    # 已含记忆检索（上限 30s）等阻塞项，故不额外改变首字节时序。
    host_profile_text = ""
    if not is_conversation_task:
        from agent.harness.contracts import RunContext as _RunContext
        from agent.host_profile import ensure_host_profile

        _host_ctx = _RunContext.new(
            task=req.task or "",
            mode=req.mode,
            ssh_session_id=req.ssh_session_id or "",
        )
        host_profile_text = await ensure_host_profile(_host_ctx)

    # M3 修复：提取 system prompt 构建为独立函数
    final_system_prompt = _build_agent_system_prompt(
        req,
        reflection_injection,
        is_conversation_task,
        host_profile_text=host_profile_text,
        # pre_intention 为 None 表示本次未做意图识别（enable_intention_guide=False）
        is_change=pre_intention.is_change if pre_intention else None,
    )

    # 应用演化配置（仅覆盖用户未显式传入的字段）
    # M3 修复：提取演化配置解析为独立函数
    # 审查 M-16：传入 prompt 文本（safe_max_tokens 内部用 estimate_tokens 估算，
    # 不再按 1.5 token/字符高估导致长对话输出被截到 256）
    _prompt_text = "\n".join(
        [req.task or "", final_system_prompt or ""]
        + [m.get("content", "") for m in history if isinstance(m.get("content", ""), str)]
        + [m for m in memory_contents if isinstance(m, str)]
    )
    _safe_max = safe_max_tokens(
        model_cfg.get("max_tokens"),
        _prompt_text,
        model_cfg.get("max_output_tokens"),
        model_cfg.get("reasoning_tokens"),
    )
    _evo = await asyncio.to_thread(_resolve_evolution_config, req, model_cfg, _safe_max, evolution_mgr)
    effective_thinking = _evo["thinking"]
    effective_max_iter = _evo["max_iter"]
    effective_temperature = _evo["temperature"]
    effective_max_tokens = _evo["max_tokens"]

    # 生成 run_id（12 位 hex：审查 MINOR 修复，8 位在数万 run 量级下生日碰撞显著，
    # 碰撞会触发 create_agent_run 主键冲突 500；12 位 hex 碰撞概率可忽略）
    import uuid as _uuid
    run_id = _uuid.uuid4().hex[:12]

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
    # 对话型任务强制禁用工具 schema：用户问"你是什么模型"时不应看到 shell_exec/web_search 等工具
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

    # 审查 MINOR 修复：校验会话存在（对比 chat.py 的 404 路径）。
    # 否则坏 conversation_id 在 FK ON 下直接 sqlite3.IntegrityError → 500
    if not await asyncio.to_thread(db.get_conversation, req.conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")

    # C-review MINOR-12：同会话并发防护 — 原子 check-and-set，冲突返回 409
    # （前端 streamAgentRun 对非 200 走 onError(detail)，用户可见提示）
    _conflict = await _acquire_run_slot(req.conversation_id, run_id)
    if _conflict:
        raise HTTPException(
            status_code=409,
            detail=f"该会话已有运行中的 Agent 任务（run_id={_conflict}），请等待其结束或终止后再试",
        )
    try:
        # 持久化 agent_run 记录（审查 M-13：放线程池）
        await asyncio.to_thread(
            db.create_agent_run,
            run_id=run_id,
            conversation_id=req.conversation_id,
            task=req.task,
            max_iterations=req.max_iterations,
            mode=req.mode,
            model_used=model_cfg.get("model_id") or model_cfg["id"],
        )

        # 保存用户消息到 db（修复：原 Agent 模式未保存用户消息，导致 post_process 的标题生成检查 len(recent)==2 失败，
        # 且历史会话切换时丢失用户问题）
        await asyncio.to_thread(
            db.add_message,
            req.conversation_id,
            "user",
            req.task,
            model_used=model_cfg.get("model_id") or model_cfg["id"],
        )
        await asyncio.to_thread(db.touch_conversation, req.conversation_id)
        await asyncio.to_thread(db.update_conversation_status, req.conversation_id, "in_progress")
    except Exception:
        # 占位已得但落库失败 → 释放占位，避免该会话被永久锁死
        await _release_run_slot(req.conversation_id, run_id)
        raise

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
                    # 推理/思考内容的 token 预算上限（来自模型配置，作为意图识别预算的上限）
                    max_reasoning_tokens=model_cfg.get("reasoning_tokens"),
                    enable_thinking=effective_thinking,
                    enable_tools=effective_enable_tools,
                    enable_intention_guide=req.enable_intention_guide,
                    enable_dynamic_budget=req.enable_dynamic_budget,
                    # C-review M10：成本/token 硬熔断
                    max_cost_usd=req.max_cost_usd,
                    max_total_tokens=req.max_total_tokens,
                )
                async for trace_event in harness.run(
                    task=req.task,
                    mode=req.mode,
                    conversation_id=req.conversation_id,
                    ssh_session_id=req.ssh_session_id,
                    environment=req.environment,
                    enable_visible_terminal=req.visible_terminal,
                    system_prompt=final_system_prompt,
                    history=history,
                    memories=memory_contents,
                    config=harness_config,
                    run_id=run_id,
                    skill_ids=req.skill_ids,
                    auto_skill_match=req.auto_skill_match,
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
        # B-8 修复：追踪 run 最终状态（done 事件前保持 None），
        # finally 中仅对 completed 的 run 清理备份
        final_state: Optional[str] = None

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
                    # 记录工具调用开始（审查 M-13：db 放线程池）
                    tc_id = await asyncio.to_thread(
                        db.add_tool_call,
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
                        # C-review NIT：trace 回填按 tc_id 精确匹配（原 (tool, iteration)
                        # 反向匹配在同轮同工具多次调用时张冠李戴）
                        "tc_id": tc_id,
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
                            await asyncio.to_thread(
                                db.update_tool_call,
                                tool_call_id=tc_id,
                                result=result_str[:5000] + ("\n...[truncated]" if truncated else ""),
                                success=event.get("success", True),
                                duration_ms=event.get("duration_ms", 0),
                            )
                        except Exception as e:
                            logger.warning("更新 tool_call 结果失败: %s", e)
                        # 回写 run_trace_tool_calls（用于 agent_trace_snapshot 持久化）
                        # C-review NIT：按 tc_id 精确匹配 — 原 (tool, iteration) 反向
                        # 匹配在同轮同工具多次调用时张冠李戴
                        for tc_entry in reversed(run_trace_tool_calls):
                            if tc_entry.get("tc_id") == tc_id and "result" not in tc_entry:
                                tc_entry["result"] = event.get("result", "")
                                tc_entry["success"] = event.get("success", True)
                                break
                elif event.get("type") == "done":
                    # 更新 agent_run 状态
                    final_state = event.get("state", "completed")
                    # P7: 提取成本快照
                    cost_data = event.get("cost") or {}
                    try:
                        await asyncio.to_thread(
                            db.update_agent_run,
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
                        # 执行过程记录丢失，用户完全看不到 Agent 做了什么
                        agent_trace_snapshot = {
                            # E 修复：保留原始 final_state（max_reached 等终态不再被
                            # 归一成 "completed"，前端按 currentState/status 自行展示）
                            "status": final_state,
                            "currentState": final_state,
                            "iterations": event.get("iterations", 0),
                            "duration": round((cost_data.get("duration_ms", 0) or 0) / 1000, 1),
                            "toolCalls": run_trace_tool_calls,
                            "reasoning": total_reasoning or "",
                            "lastRunId": run_id,
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

                        content_to_save = total_content

                        if content_to_save:
                            await asyncio.to_thread(
                                db.add_message,
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
                                # 审查 MINOR 修复：COUNT 查询替代 get_messages(limit=1000) 全量拉取
                                _n_user = await asyncio.to_thread(
                                    db.count_messages_by_role, req.conversation_id, "user",
                                )
                                turn_index = max(0, _n_user - 1)
                            except Exception:
                                turn_index = 0

                            from post_processor import get_post_process_manager
                            await get_post_process_manager().submit(
                                mode=req.mode,
                                conversation_id=req.conversation_id,
                                user_message=req.task,
                                assistant_content=total_content,
                                assistant_reasoning=total_reasoning,
                                # 审查外热修：后处理契约是行主键 id — 原传 LLM 模型名
                                # （model_id 列）导致 get_model_config 按 id 查落空，
                                # 记忆/标题/技能提取被静默跳过（unknown model_id 告警）
                                model_id=model_cfg.get("id"),
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
                        await asyncio.to_thread(
                            db.update_agent_run,
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

                yield sse_event(event)

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Agent 运行异常 (event_stream)")
            # 异常时将 agent_run 状态标记为 failed
            try:
                await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="failed")
            except Exception:
                pass
            yield sse_event({"type": "error", "message": str(e)})
            yield "data: [DONE]\n\n"
        finally:
            # ── 抗取消清理 ─────────────────────────────────────────────
            # 故障根因：客户端断连 → 响应任务被 cancel → finally 里第一个
            # await 抛 CancelledError（BaseException，except Exception 接不住）
            # → 其后的占位释放/会话复位全部跳过 → run 在 DB 里已 completed，
            # 但内存占位残留，新请求永远报"已有运行中的 Agent 任务"。
            # 对策：1) 占位用无 await 的同步操作最先释放（取消也丢不掉）；
            #      2) 其余清理全部 except BaseException；
            #      3) 关键异步清理用 shield（外层被取消时内层任务继续跑完）。
            _release_run_slot_sync(req.conversation_id, run_id)
            try:
                heartbeat_task.cancel()
                engine_task.cancel()
            except BaseException:
                pass
            # 清理 TelemetryBus 事件缓存（防止内存泄漏）
            try:
                harness.telemetry.clear()
            except BaseException:
                pass
            # 清理 todo_store（防止内存泄漏）
            try:
                from agent.tools.builtin.todo_write import clear_todos
                clear_todos(run_id)
            except BaseException:
                pass
            # P2 修复：run 取消/断连时清理该 run 名下的远程后台任务
            # （job_run target=remote 的进程脱离会话运行，不杀会永久残留；
            #  shield 保证断连 cancel 时清理任务仍会在后台跑完）
            try:
                from agent.tools.builtin.ops_job import kill_remote_jobs_for_run
                await asyncio.shield(kill_remote_jobs_for_run(run_id))
            except BaseException:
                logger.warning("取消联动清理远程任务失败 (run_id=%s)", run_id, exc_info=True)
            # 客户端断开时将 agent_run 标记为 failed（若仍为 running/init）
            try:
                run = await asyncio.shield(asyncio.to_thread(db.get_agent_run, run_id))
                if run and run.get("status") in ("running", "init"):
                    await asyncio.shield(
                        asyncio.to_thread(db.update_agent_run, run_id=run_id, status="failed")
                    )
            except BaseException:
                pass
            # 修复：Agent 启动时将会话状态设为 in_progress，结束时须重置回
            # completed — 若不复位，会话永久滞留 in_progress。
            # （占位释放已提到最前的同步操作；此步为对称的 DB 侧复位）
            try:
                cur_status = await asyncio.shield(
                    asyncio.to_thread(db.get_conversation, req.conversation_id)
                )
                if cur_status and cur_status.get("status") == "in_progress":
                    await asyncio.shield(
                        asyncio.to_thread(
                            db.update_conversation_status, req.conversation_id, "completed"
                        )
                    )
            except BaseException:
                pass
            # 低频清理 agent_runs/tool_calls（每 6h 节流，best-effort）
            try:
                await _maybe_prune_agent_runs()
            except BaseException:
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


class ToolResultRequest(BaseModel):
    """可见终端执行结果回传 — 前端在把命令写入可见终端并捕获输出后调用"""
    tool_id: str
    output: str = ""
    success: bool = False
    exit_code: Optional[int] = None


@router.post("/agent/tool-result")
async def agent_tool_result(req: ToolResultRequest):
    """回传可见终端命令执行结果，恢复挂起的 Agent 循环。

    - 200：成功恢复对应的待交付命令
    - 404：tool_id 未知（可能已超时清除/会话结束）
    """
    ok = PendingDeliveries.resume(
        req.tool_id,
        output=req.output,
        success=req.success,
        exit_code=req.exit_code,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"tool_id 未知: {req.tool_id}")
    return {"status": "ok", "tool_id": req.tool_id}


@router.get("/agent/tools")
def list_tools(mode: str = "ssh"):
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
def update_tool_enabled(tool_name: str, req: UpdateToolEnabledRequest, mode: str = "all"):
    """更新工具启用状态（默认 mode=all 全局开关）"""
    import database as db
    result = db.upsert_tool_config(tool_name, mode, req.enabled)
    return result


# ── Agent 权限管理 ──


class PermissionGrantRequest(BaseModel):
    tool: str = Field(..., description="工具名")
    # 审查 M-3：默认 scope 从 "always" 收紧为 "once"（仅放行下一次调用）。
    # 原默认 always 会在用户点一次后永久免审批。
    scope: str = Field("once", description="授权范围：once（仅本次）/ session（会话）/ always（永久）")
    # 命令执行工具（shell_exec）时传入被批准的具体命令，
    # 授权绑定到命令哈希（同命令免审批，其他 dangerous 命令仍需审批）
    command: Optional[str] = Field(None, description="命令执行工具的具体命令（用于命令级绑定）")
    # C-review M11：session 级授权绑定的会话 ID — 防跨会话授权泄漏
    conversation_id: str = Field("", description="会话 ID（session 授权按会话键控）")
    # P1 竞态修复：once 授权绑定的运行 ID — 运行终止时按 run_id 清理
    run_id: str = Field("", description="运行 ID（once 授权按 run_id 隔离，终态自动清理）")


@router.post("/agent/permission/grant")
def grant_tool_permission(req: PermissionGrantRequest):
    """授权工具

    scope=once: 仅放行下一次相同工具（命令工具为相同命令）调用
    scope=session: 进程级单例 _confirmed_session，跨 run 共享，进程重启失效（H13 修复 A5）；
      C-review M11：按 conversation_id 键控，只对发起会话的 run 生效
    scope=always: 写入 ~/.ckunagentic/allowlist.txt，所有后续 run 放行
      （命令工具写入的是 `tool:cmdhash` 键，不覆盖其他命令 — 审查 M-3）
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    if req.scope not in ("once", "session", "always"):
        raise HTTPException(status_code=400, detail="scope 必须是 once、session 或 always")
    # once 授权严格按 run 隔离（run 终态自动清理）；缺失 run_id 的授权
    # 无法被任何 run 消费，直接拒绝（原实现落空 run_id 全局池，有跨 run 竞态）
    if req.scope == "once" and not req.run_id:
        raise HTTPException(status_code=400, detail="scope=once 授权必须携带 run_id")

    # H13 修复 A5：session scope 改用进程级单例（原 501 限制移除）
    pg = _get_global_permission_gate_sync()
    try:
        grant_key = pg.grant(req.tool, scope=req.scope, command=req.command,
                             conversation_id=req.conversation_id, run_id=req.run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # C-review C2 修复：批准动作落审计链（原实现只写内存/allowlist，
    # 事后无法证明"谁批准了什么"，等保审计目标落空）
    try:
        from agent.audit import append_record
        append_record(
            action=f"permission_grant:{req.tool}",
            conclusion="approved",
            target_type="tool",
            target_id=grant_key,
            command_raw=req.command or "",
            result=f"scope={req.scope},conversation={req.conversation_id or '-'}",
        )
    except Exception:
        logger.warning("grant 审计记录失败（不阻塞）")
    return {"tool": req.tool, "scope": req.scope, "grant_key": grant_key, "granted": True}


@router.post("/agent/permission/revoke")
def revoke_tool_permission(tool: str, command: Optional[str] = None,
                           conversation_id: Optional[str] = None):
    """撤销工具授权（从永久 allowlist 和 session scope 移除）

    提供 command 时只撤销该命令的绑定授权；否则撤销该工具全部授权。
    C-review M11：提供 conversation_id 时只撤该会话的 session 授权，
    否则（默认）撤所有会话。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    pg.revoke(tool, command=command, conversation_id=conversation_id)
    # C-review C2 修复：撤销动作同样落审计链
    try:
        from agent.audit import append_record
        append_record(
            action=f"permission_revoke:{tool}",
            conclusion="revoked",
            target_type="tool",
            target_id=command or tool,
            command_raw=command or "",
        )
    except Exception:
        logger.warning("revoke 审计记录失败（不阻塞）")
    return {"tool": tool, "granted": False}


@router.get("/agent/permission/list")
def list_tool_permissions():
    """查询已永久授权的工具列表"""
    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    return {"tools": sorted(pg._confirmed_permanent)}


class AllowPermissionRequest(BaseModel):
    """手动登记永久授权（免去先触发审批弹窗再点"始终允许"的路径）"""
    tool: str = Field(..., min_length=1, max_length=64, description="工具名")
    command: Optional[str] = Field(None, max_length=4096,
                                   description="命令执行工具的绑定命令；省略 = 工具级授权")


@router.post("/agent/permission/allow")
def allow_tool_permission(req: AllowPermissionRequest):
    """手动写入永久 allowlist（与审批"始终允许"同源，落 allowlist 文件 + 审计）

    安全边界：仅登记，不放宽任何判定 — 命令工具写入的是 `tool:cmdhash`
    命令级绑定键（审查 M-3 语义），hardline 底线命令在 FilesystemGuard
    层无条件阻断，allowlist 无法放行。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    grant_key = pg.grant(req.tool, scope="always", command=req.command)
    try:
        from agent.audit import append_record
        append_record(
            action=f"permission_allow_manual:{req.tool}",
            conclusion="granted",
            target_type="tool",
            target_id=req.command or req.tool,
            command_raw=req.command or "",
        )
    except Exception:
        logger.warning("allow 审计记录失败（不阻塞）")
    return {"tool": req.tool, "grant_key": grant_key, "granted": True}


class SafetyClassifyRequest(BaseModel):
    """批量下发等「人主导通道」的执行前判定请求"""
    commands: list[str] = Field(..., min_length=1, max_length=50,
                                description="待执行命令原文，1-50 条")

    @field_validator("commands")
    @classmethod
    def _each_within_limit(cls, v: list[str]) -> list[str]:
        for i, c in enumerate(v):
            if len(c) > 4096:
                raise ValueError(f"第 {i + 1} 条命令超过 4096 字符")
        return v


@router.post("/agent/safety/classify")
def classify_safety_commands(req: SafetyClassifyRequest):
    """命令风险判定的唯一来源（safety.classify_command）对外暴露，只读无状态。

    为什么要这个端点：批量下发是「一个人的动作 × N 台机器」的放大点，此前它只在
    渲染层自带一份词表来弹确认（bulkCommandDiff 的 DANGEROUS_PATTERNS）—— 那是
    第四份规则表，与后端 HARDLINE/DANGEROUS 两套口径各写各的，且 mkfs、rm -rf /
    在那条路上只是"多问一句"，点确认照样打到 N 台上。前端自此不再自带规则。

    口径与 shell_exec 完全一致：模型自由文本 → classify_command（含「未入只读白
    名单即审批」）。不区分 constructed 模板口径，因为这里的输入就是人敲的原文。

    本端点只判定，不放行任何操作：不写审计、不建 run、不碰 allowlist。真正的阻断
    发生在调用方（批量下发据 hardline 直接中止）。
    """
    from agent.harness.command_explain import explain_for_reason
    from agent.harness.safety import classify_command, get_normalized_for_display
    results = []
    for raw in req.commands:
        level, reason = classify_command(raw)
        display = get_normalized_for_display(raw)
        expl = explain_for_reason(reason or "")
        results.append({
            "command": raw,
            "level": level,
            "reason": reason or "",
            "normalized": display,
            # 中文后果说明：dangerous 的确认框给人看得懂的理由，而不是英文 reason
            "explain": expl.text,
        })
    return {"results": results}


class DenyCommandRequest(BaseModel):
    """visible_terminal 拒绝命令请求 — 标记某 run 下某命令被拒绝"""
    tool: str = Field(..., description="工具名（通常为 shell_exec）")
    command: str = Field(..., description="被拒绝的命令")
    run_id: str = Field("", description="运行 ID（拒绝记录按 run_id 隔离）")


@router.post("/agent/permission/deny-command")
def deny_command(req: DenyCommandRequest):
    """标记某 run 下某命令被用户拒绝

    visible_terminal 拒绝时调用：后续该 run 再次请求此命令时 PermissionGate
    check_permission 直接返回 False，不会再次触发 permission_needed 卡片。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    if not req.run_id:
        raise HTTPException(status_code=400, detail="deny_command 必须携带 run_id（拒绝记录按 run 隔离）")
    pg = _get_global_permission_gate_sync()
    try:
        denied_key = pg.deny_command(req.tool, req.command, req.run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 审计记录
    try:
        from agent.audit import append_record
        append_record(
            action=f"permission_deny_command:{req.tool}",
            conclusion="denied",
            target_type="tool",
            target_id=denied_key,
            command_raw=req.command,
            result=f"run={req.run_id or '-'}",
        )
    except Exception:
        logger.warning("deny_command 审计记录失败（不阻塞）")
    return {"tool": req.tool, "command": req.command,
            "run_id": req.run_id, "denied_key": denied_key}


@router.post("/agent/permission/release-run/{run_id}")
def release_run_permissions(run_id: str):
    """手动清理某次 run 的全部 once 授权（前端显式终止或超时时调用）

    ReflectionRein 在 AGENT_COMPLETE / AGENT_FAILED 时自动调用此逻辑，
    本接口作为兜底：前端在用户点击"停止"或 run 被强制终止时主动释放。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    released = pg.cleanup_run_once(run_id)
    try:
        from agent.audit import append_record
        append_record(
            action=f"permission_release_run:{run_id}",
            conclusion="released",
            target_type="run",
            target_id=run_id,
            result=f"released={released}",
        )
    except Exception:
        logger.warning("release_run 审计记录失败（不阻塞）")
    return {"run_id": run_id, "released": released}


# ── 澄清向导（Clarification Wizard）──

# P0-3: custom_text 最大长度限制，防止 prompt injection
_CUSTOM_TEXT_MAX_LEN = 500


class ClarificationAnswerRequest(BaseModel):
    """用户回答澄清问题的请求体（带防御性校验）"""
    run_id: str = Field(..., description="运行 ID", max_length=128)
    answers: dict = Field(
        default_factory=dict,
        description="答案字典 {step_index(key str): {choice: 'A'|'B'|'C'|'custom', custom_text?: str}}",
    )

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, v: dict) -> dict:
        """P0-3: 校验 answers 结构，拒绝畸形 step_idx 与超长 custom_text"""
        if not isinstance(v, dict):
            raise ValueError("answers 必须是 dict")
        if len(v) > 32:
            raise ValueError(f"answers 步数过多（>{32}）")
        sanitized: dict[str, dict] = {}
        for key, item in v.items():
            # step_idx 必须是可解析的 0-based 整数
            try:
                idx = int(key)
                if idx < 0:
                    raise ValueError
            except (ValueError, TypeError):
                logger.warning("非法 clarification step_idx: %r（已跳过）", key)
                continue
            if not isinstance(item, dict):
                continue
            choice = item.get("choice", "")
            if choice not in ("A", "B", "C", "custom"):
                logger.warning("非法 clarification choice: %r（已跳过）", choice)
                continue
            cleaned: dict[str, str] = {"choice": choice}
            if choice == "custom":
                txt = str(item.get("custom_text", ""))
                if len(txt) > _CUSTOM_TEXT_MAX_LEN:
                    txt = txt[:_CUSTOM_TEXT_MAX_LEN]
                cleaned["custom_text"] = txt
            sanitized[str(idx)] = cleaned
        return sanitized


@router.post("/agent/clarification/answer")
def submit_clarification_answer(req: ClarificationAnswerRequest):
    """用户回答完澄清问题后调用 — 写入答案到 pending 存储

    前端 ClarifyingWizard 在用户答完所有步（或点击"完成"）后提交。
    下一轮 PRE_TOOL_CALL 时 ClarificationInterceptor 读到 _CLARIFICATION_DONE
    即注入用户答案上下文、发 clarification_done 事件、放行工具执行。
    """
    from agent.harness.reins import record_clarification_answers
    # answers 已由 field_validator 清洗：step_idx 合法、choice 受控、custom_text 截断
    # P1-6: 统一入口维护 done+context 配对与时间戳（TTL 可回收孤儿答案）
    record_clarification_answers(req.run_id, req.answers)
    logger.info("clarification answer: run_id=%s steps=%d", req.run_id, len(req.answers))
    # 审计
    try:
        from agent.audit import append_record
        append_record(
            action="clarification_answer",
            conclusion="answered",
            target_type="run",
            target_id=req.run_id,
            result=f"steps={len(req.answers)}",
        )
    except Exception:
        logger.warning("clarification answer 审计记录失败（不阻塞）")
    return {"run_id": req.run_id, "status": "recorded", "steps": len(req.answers)}


@router.post("/agent/clarification/skip")
def skip_clarification(req: ClarificationAnswerRequest):
    """用户跳过所有剩余澄清步 — 写入空答案（空 dict 表示无约束放行）

    ClarificationInterceptor 在格式化空答案时输出"用户选择跳过澄清"提示，
    LLM 按原始命令执行，但仍在 PermissionGate 的权限控制之下。
    """
    from agent.harness.reins import record_clarification_answers
    record_clarification_answers(req.run_id, {})  # 空 answers → _format_answers 输出"跳过"
    logger.info("clarification skip: run_id=%s", req.run_id)
    try:
        from agent.audit import append_record
        append_record(
            action="clarification_skip",
            conclusion="skipped",
            target_type="run",
            target_id=req.run_id,
        )
    except Exception:
        logger.warning("clarification skip 审计记录失败（不阻塞）")
    return {"run_id": req.run_id, "status": "skipped"}


# ── Agent 记忆管理 ──


@router.get("/agent/memories")
def list_agent_memories(
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
def delete_agent_memory(memory_id: str):
    """删除单条 Agent 记忆"""
    ok = db.delete_agent_memory(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": True, "id": memory_id}


@router.post("/agent/memories/prune")
def prune_agent_memories(
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


@router.get("/agent/tick/status")
def consciousness_tick_status():
    """查询意识 tick 状态"""
    tick = get_global_tick()
    return {
        "running": tick._task is not None and not tick._task.done(),
        "interval_seconds": tick.config.interval_seconds,
        "last_tick": tick.last_tick_result,
    }


# ── 策略（反思 + 演化） ──


@router.get("/agent/strategies")
def list_agent_strategies(
    mode: Optional[str] = None,
    strategy_type: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    """查询 Agent 策略列表（reflection / evolved_config）"""
    rows = db.get_agent_strategies(
        mode=mode, strategy_type=strategy_type, limit=limit
    )
    return {"strategies": rows, "count": len(rows)}


@router.patch("/agent/strategies/{strategy_id}/enabled")
def toggle_strategy_enabled(strategy_id: str, enabled: int = 1):
    """启用/禁用单条策略"""
    ok = db.update_agent_strategy_enabled(strategy_id, enabled=int(enabled))
    if not ok:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"updated": True, "id": strategy_id, "enabled": int(enabled)}


@router.delete("/agent/strategies/{strategy_id}")
def delete_agent_strategy_route(strategy_id: str):
    """删除单条策略"""
    ok = db.delete_agent_strategy(strategy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"deleted": True, "id": strategy_id}


# ── Trace Viewer + 成本追踪 ──


@router.get("/agent/runs/{run_id}/trace")
def get_agent_run_trace(run_id: str):
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
    }


