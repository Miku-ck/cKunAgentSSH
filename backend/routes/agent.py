"""
Agent 路由 — SSE 流式运行 + 治理管理端点

运行与交付：
  POST  /agent/run                          — 启动 Agent，SSE 流式返回运行过程
  POST  /agent/tool-result                  — 可见终端执行模式回传命令结果
  GET   /agent/runs/{run_id}/trace          — 回放单次运行的完整轨迹
工具：
  GET   /agent/tools                        — 当前可用工具列表
  PATCH /agent/tools/{tool_name}/enabled    — 启停某工具
审批授权：
  POST  /agent/permission/grant|revoke|allow|deny-command|release-run/{run_id}
  GET   /agent/permission/list              — 永久授权键列表
  POST  /agent/safety/classify              — 命令内容级高危判定
澄清向导：
  POST  /agent/clarification/answer|skip
记忆：
  GET   /agent/memories · DELETE /agent/memories/{id} · POST /agent/memories/prune
意识 tick 与演化策略：
  GET   /agent/tick/status                  — 意识循环运行状态
  GET   /agent/strategies · PATCH .../{id}/enabled · DELETE .../{id}
"""
import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import database as db
from routes.terminal import _get_model_config
# 记忆域已拆至 routes/agent_memory.py（P2 单文件单域）。此处 re-export 保持
# `routes.agent.<符号>` 的既有调用方（含后端测试）不变；端点路径与契约不变。
from routes.agent_memory import (  # noqa: F401  (re-export)
    BlockPutRequest,
    MemoryCreateRequest,
    MemoryEditRequest,
    clear_agent_memories,
    create_agent_memory,
    delete_agent_memory,
    edit_agent_memory,
    list_agent_memories,
    list_memory_audit,
    list_memory_blocks,
    list_memory_jobs,
    memory_block_history,
    memory_stats,
    put_memory_block,
    retry_memory_job,
)
from sse_utils import sse_event
# re-export：权限/澄清等拆分模块与测试按 routes.agent.append_record_async 取用
# 并对其 monkeypatch，删掉这行会让既有测试的 monkeypatch 直接 AttributeError。
from agent.audit import append_record_async  # noqa: F401  (re-export)
from agent import audit
from agent.environment_policy import EnvironmentResolution, resolve_run_environment
from agent.memory import mstore
from agent.memory import budget as memory_budget
from agent.memory import reconcile as memory_reconcile
from agent.memory import retriever as memory_retriever
from agent.memory.embedding import get_embedding_client
from agent.consciousness_tick import get_global_tick
from agent.pending_delivery import PendingDeliveries
from prompts import _current_time_str, SSH_MODE
from safe_tokens import safe_max_tokens

if TYPE_CHECKING:
    from agent.evolution import EvolutionEngine

# 执行栈（Harness 框架 / 工具注册表 / 反思演化 / 成本追踪）一律按需导入。
# 这条链（agent.harness → driver → reins → safety，外加 agent.tools /
# agent.reflection / agent.evolution / agent.cost_tracker；后两者又会传递性
# 拉回 agent.harness.harness）实测占导入期 70ms 以上，只有真正发起一次 run，
# 或走结构化 / 提交信息生成这类纯推理端点时才需要，不该压在后端启动关键路径上。
# 与下方大量 `from agent.harness.reins import ...` 的函数内局部导入同一约定。

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])

HEARTBEAT_INTERVAL = 15


async def _run_reflection_and_evolution(
    run_id: str,
    task: str,
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
        # 按需导入执行栈（见文件头说明）；本函数跑在线程池里，导入只发生一次
        from agent.evolution import EvolutionEngine
        from agent.reflection import MetaReflection, RunTrace

        reflection_mgr = MetaReflection()
        evolution_mgr = EvolutionEngine()

        # 构建 RunTrace（含成本/状态历史）
        trace = RunTrace(
            run_id=run_id,
            task=task,
            mode=SSH_MODE,
            final_state=final_state,
            iterations=iterations,
            tool_calls=tool_calls,
            cost_usd=cost_usd,
            total_tokens=total_tokens,
            state_history=state_history or [],
        )
        result = reflection_mgr.reflect(trace)
        reflection_mgr.save_reflection(
            result, mode=SSH_MODE, task=task,
            cost_usd=cost_usd, iterations=iterations, total_tokens=total_tokens,
        )
        logger.info(
            "反思完成 (run_id=%s, score=%.2f, cost=$%.3f): %s",
            run_id, result.score, cost_usd, result.summary,
        )

        # 加载近 10 条反思，触发演化 — 用未过滤的"最近"视图，不是高分注入视图
        recent = reflection_mgr.load_reflections_for_evolution(SSH_MODE, limit=10)
        if len(recent) >= 3:  # 至少 3 条反思才演化
            evolved = evolution_mgr.evolve(mode=SSH_MODE, recent_reflections=recent)
            if evolved.adjustments:
                evolution_mgr.save_evolved_config(evolved, mode=SSH_MODE)
                logger.info(
                    "策略演化完成 (mode=%s, adjustments=%d): %s",
                    SSH_MODE, len(evolved.adjustments), "; ".join(evolved.adjustments),
                )

    try:
        await asyncio.to_thread(_work)
    except Exception as e:
        # 反思/演化是 Agent 自我迭代核心链路；失败会让策略静默降级、用户无感。
        # warning 会被中低频日志淹没，upgrade 到 error 才能让"策略停止进化"被看见。
        logger.error("反思/演化失败: %s", e, exc_info=True)


# ── Helper Functions ──

def _structure_aware_permission_mode(has_added_write_steps: bool, permission_mode: str) -> str:
    """结构化增强含新增写步骤时，auto 回落 normal（spec 2026-10-24 fail-closed）。

    这些写动作是增强"扩"出来的、用户没在原话里点名，不该被"模型代判"（auto）顺带
    放行。抽成纯函数是为了能被守卫测试钉住；plan 不受影响（比 normal 更严）。
    """
    if has_added_write_steps and permission_mode == "auto":
        logger.info("结构化增强含新增写步骤，auto 审批回落 normal（run 级 fail-closed）")
        return "normal"
    return permission_mode


def _build_agent_system_prompt(
    req: "AgentRunRequest",
    profile_text: str,
    is_conversation_task: bool,
    is_change: Optional[bool] = None,
    goal_text: str = "",
    identity_section: str = "",
) -> str:
    """构建 Agent 系统提示（通用 harness 新序，spec §5）

    拼装顺序：时间 → 任务性质提示（仅非对话）→ 中性基础身份 → 专家身份段
              → goal/criteria/当前 step（结构化 goal/plan 注入段）→ 画像块
              → 用户 system_prompt
              → [对话引导 | 通用工具纪律 + 记忆/拆分/子Agent/输出格式]

    identity_section: 专家身份段（build_identity_section(policy) 的结果），空串不追加任何字节。
        紧随基础身份注入（身份层放最靠前以获得最高注意力），不再末尾追加。
    is_change: 任务是否可能改变状态。None=未做意图识别，提示文案按「未判定（按变更对待）」，
        不对称取舍保留（运维包选择规则消费该提示）。
    goal_text: 结构化 goal/plan 注入段（routes._goal_plan_setup.load_goal_plan_state
        渲染），空串不追加；纯对话任务由调用方不传（保持旧判据）。
    """
    from prompts import BASE_IDENTITY_PROMPT, GENERAL_TOOL_DISCIPLINE

    time_injection = (
        f"[系统时间] 当前时间：{_current_time_str()}。"
        f"如用户询问日期/星期/时间，请以本信息为准，不要使用训练数据中的旧时间。"
    )
    parts = [time_injection]

    # 任务性质提示：通用元数据，前置在身份层之前；运维身份段的选择规则引用它。
    if not is_conversation_task:
        if is_change is False:
            nature = "只读"
        elif is_change is True:
            nature = "可能变更"
        else:
            nature = "未判定（按变更对待）"
        parts.append(f"[任务性质] {nature}")

    # 中性基础身份（领域无关，恒注入）+ 专家身份段（空串不加字节）
    parts.append(BASE_IDENTITY_PROMPT)
    if identity_section:
        parts.append(identity_section)

    # 结构化 goal/plan 注入段（_goal_plan_setup 渲染）：goal 是执行锚，
    # 空 = 会话无结构化 goal（不追加任何字节）
    if goal_text:
        parts.append(goal_text)
    if profile_text and not is_conversation_task:
        parts.append(profile_text)
    if req.system_prompt:
        parts.append(req.system_prompt)

    if is_conversation_task:
        parts.append(
            "本任务为纯对话型问题，请直接简洁回答，不要调用任何工具，不要做长篇规划。"
            "回答应基于你自身的身份和能力，避免引用无关上下文或记忆。"
        )
    else:
        parts.append(GENERAL_TOOL_DISCIPLINE)
        parts.append(
            "记忆使用：上下文里 [画像] 是常驻已知事实、[相关记忆] 是本轮检索到的历史经验，"
            "可直接引用无需再调工具。需要更多历史经验、或判断某做法在其他主机上的先例时用 "
            "memory_search；确认了值得长期保留的主机特性/操作教训时用 memory_add（进后台对账队列，"
            "本轮不生效）；用户给出持久偏好、或要修正 [画像] 里的既有内容时用 block_update；"
            "发现 [相关记忆] 里的事实已被证伪时用 memory_invalidate 作废它，不要留着互相矛盾的记忆。"
        )
        parts.append(
            "任务拆分：面对复杂任务（涉及多文件修改、多步骤验证、跨模块重构等），请先在首轮输出一个简短的"
            "子任务清单（用编号列表），然后逐个完成并验证。每完成一个子任务就明确标注"
            "'✓ 子任务 N 完成'，便于追踪进度。若某个子任务卡住，先跳过并继续后续子任务，"
            "最后统一汇报未完成项。这样即使迭代数耗尽，也能保证部分成果交付。"
        )
        parts.append(
            "子 Agent：你可以使用 spawn_subagent 工具把独立子任务（如代码审查、方案设计、文档生成、"
            "测试用例生成等）交给一个子 Agent 处理。子 Agent 是一次无工具的 LLM 推理调用，"
            "适合做可独立完成的推理类任务。传入清晰的 task 和 system_prompt（角色设定），"
            "可选 extra_context 提供代码片段等上下文。子 Agent 返回结果后，你负责整合并执行后续操作。"
            "合理使用子 Agent 可以避免主上下文被细节淹没，提高复杂任务的执行效率。"
        )
        parts.append(
            "输出格式：在工具调用迭代过程中保持输出简洁，避免把每一步的内心独白、英文碎碎念或执行日志写入正文。"
            "完成所有操作后，用结构化中文报告总结：先给总体结论，再用 ## 二级标题分节，每个修复点独立成段，"
            "代码用代码块包裹。标题、列表、代码块前后必须换行，确保 Markdown 渲染正确。"
        )

    return "\n\n".join(parts)



def _load_evolved_config(evolution_mgr: "EvolutionEngine") -> Optional[dict]:
    """读取最新演化配置，失败按「无演化配置」降级（调用方在 to_thread 中执行）"""
    try:
        return evolution_mgr.load_latest_evolved_config(SSH_MODE)
    except Exception as e:
        logger.warning("加载演化配置失败: %s", e)
        return None


def _resolve_thinking(req: "AgentRunRequest", evolved_cfg: Optional[dict]) -> bool:
    """enable_thinking 三态解析：None = 客户端未表态（交给演化配置），
    显式 True/False 一律以用户选择为准。

    必须在 safe_max_tokens 之前定稿 —— 推理预算只在 thinking 生效时占用输出空间
    （stirrups.call_llm 未开启时显式传 None），thinking 关闭还预留 8192 会把
    小上下文模型的输出削到 256 token，回答与工具调用 JSON 静默截断。
    """
    if req.enable_thinking is not None:
        return req.enable_thinking
    return bool((evolved_cfg or {}).get("enable_thinking", False))


def _resolve_evolution_config(
    req: "AgentRunRequest",
    model_cfg: dict,
    safe_max: int,
    evolved_cfg: Optional[dict],
    thinking: bool,
) -> dict:
    """解析演化配置，返回 effective thinking/max_iter/temperature/max_tokens

    evolved_cfg / thinking 由调用方加载并解析 —— safe_max_tokens 必须先知道
    thinking 是否生效才能决定预留多少推理预算，见下方调用点。

    演化值是**落库的历史数据**，读取侧必须重新校界：写侧的 Field(ge=1, le=500)
    只约束请求，管不住 agent_strategies 里的旧行/手改行；越界值会直接进了
    HarnessAgentConfig（迭代 0 = 一轮就收工，max_tokens 超窗 = LLM 400）。
    """
    max_iter = req.max_iterations
    temperature = model_cfg.get("temperature", 0.7)
    # 演化不改 top_p（agent_strategies 没有这一项），所以只取模型行的值；
    # 越界/脏行由 AgentConfig.__post_init__ 在唯一入口兜住，这里不再夹一遍。
    top_p = model_cfg.get("top_p", 0.9)
    max_tokens = safe_max
    if evolved_cfg:
        # 只有用户开着「动态步数预算」时，演化学到的迭代上限才接管。
        # 原实现无条件覆盖 —— 设置面板里的 max_iterations 与动态预算开关双双失效
        # （下面那句老注释「用户值作为兜底」写的正是相反的语义）。
        evolved_iter = evolved_cfg.get("max_iterations")
        if req.enable_dynamic_budget and isinstance(evolved_iter, (int, float)) and not isinstance(evolved_iter, bool):
            v = int(evolved_iter)
            # 越界即弃用（不是夹到边界）：演化写入时自己限在 5-300，读到区间外的值
            # 说明这行数据来自旧版本/手改，夹成 1 会让 run 一轮收工，比不接管更糟
            if 1 <= v <= 500:
                max_iter = v
        evolved_temp = evolved_cfg.get("temperature")
        if isinstance(evolved_temp, (int, float)) and not isinstance(evolved_temp, bool):
            t = float(evolved_temp)
            # 演化温度按 mode 存、跨模型共用 —— 只保证落在可用区间
            if 0.0 <= t <= 2.0:
                temperature = t
        evolved_max_tokens = evolved_cfg.get("max_tokens")
        if isinstance(evolved_max_tokens, int) and 0 < evolved_max_tokens < safe_max:
            max_tokens = evolved_max_tokens

    return {
        "thinking": thinking,
        "max_iter": max_iter,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }


# ── Request Models ──


class ImageRef(BaseModel):
    """随 run 下发的图片附件引用。图片本体已由 /chat/upload 落盘在
    uploads/<conversation_id>/ 下，这里只传身份（file_id + filename），
    不把 base64 塞进任务文本。"""
    file_id: str
    filename: str


# run 请求的图片上限：多模态请求体按 base64 膨胀 4/3，张数放开会直接顶爆上游
# 请求体上限与首 token 延迟；个人运维场景 4 张截图足以覆盖一次排障
_RUN_IMAGE_LIMIT = 4
_RUN_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024  # 解码后总字节上限（base64 前口径）
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _resolve_upload_copy(root: Path, conv: str, file_id: str, name: str) -> Optional[Path]:
    """按 file_id 定位上传副本：先查 run 所属会话目录，miss 则跨会话扫描。

    上传落点与 run 会话可能不一致 —— 附件通常在会话存在之前就挂上：当时
    后端会话还没建（或建失败），上传退回共享的 general 目录；run 却带着
    正式会话 id 来找。文本附件历来不受影响（内容直接注入），而图片/视频
    按物理路径定位，必须按 id 找而不是按会话键找。file_id 是 uuid4().hex，
    全局唯一且文件名安全，`<file_id>_*` 前缀匹配至多命中上传时的那一个副本。
    """
    direct = root / conv / f"{file_id}_{name}"
    if direct.is_file():
        return direct
    matches = sorted(root.glob(f"*/{file_id}_*"))
    return matches[0] if matches else None


async def _load_run_image_blocks(
    images: Optional[list[ImageRef]], conversation_id: str,
) -> Optional[list[dict]]:
    """把 run 请求的图片引用读成 OpenAI image_url 内容块（base64 data URL）。

    校验 fail-closed：file_id 必须 32 位 hex（上传侧 uuid4().hex 的形态）、
    文件名消毒与 /chat/upload 同一规则、后缀必须是图片类型、路径必须落在
    uploads 根下、副本必须仍存在 —— 任何一条不过直接 400，绝不静默降级
    （用户以为模型看得到图，实际丢了，这比报错更骗人）。
    """
    if not images:
        return None
    if len(images) > _RUN_IMAGE_LIMIT:
        raise HTTPException(
            status_code=400, detail=f"图片附件最多 {_RUN_IMAGE_LIMIT} 张")
    from routes.chat import _IMAGE_MIME, _sanitize_conv, _uploads_root

    safe_conv = _sanitize_conv(conversation_id) or "general"
    root = _uploads_root().resolve()
    blocks: list[dict] = []
    total_bytes = 0
    for ref in images:
        if not _FILE_ID_RE.fullmatch(ref.file_id or ""):
            raise HTTPException(
                status_code=400, detail=f"图片 file_id 形态非法: {(ref.file_id or '')[:16]}")
        name = (ref.filename or "").replace("\\", "/").split("/")[-1][:120].strip() or "unnamed"
        suffix = Path(name).suffix.lower()
        mime = _IMAGE_MIME.get(suffix)
        if mime is None:
            raise HTTPException(
                status_code=400, detail=f"附件 {name} 不是支持的图片类型")
        path = _resolve_upload_copy(root, safe_conv, ref.file_id, name)
        if path is None:
            raise HTTPException(
                status_code=400,
                detail=f"图片附件已失效（上传副本不存在，会话可能已被清理）: {name}")
        data = await asyncio.to_thread(path.read_bytes)
        total_bytes += len(data)
        if total_bytes > _RUN_IMAGE_TOTAL_BYTES:
            raise HTTPException(
                status_code=413, detail="图片附件总量超过 20MB 上限")
        b64 = await asyncio.to_thread(
            lambda d=data: base64.b64encode(d).decode("ascii"))
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    return blocks


async def _load_run_video_note(
    videos: Optional[list[ImageRef]], conversation_id: str,
) -> str:
    """把 run 请求的视频附件解析成任务文本后缀（模型无法直接观看视频）。

    与图片不同，视频没有 image_url 通道 —— 诚实注入的是「身份 + 存储路径 +
    分析手段」：Agent 有 shell 工具，可用 ffprobe/ffmpeg 检查时长/编码/分辨率
    等元数据。校验与图片同一套 fail-closed 口径（hex file_id / 消毒文件名 /
    路径落盘校验），任一不过直接 400。
    """
    if not videos:
        return ""
    if len(videos) > _RUN_IMAGE_LIMIT:
        raise HTTPException(
            status_code=400, detail=f"视频附件最多 {_RUN_IMAGE_LIMIT} 个")
    from routes.chat import ALLOWED_VIDEO_EXTS, _sanitize_conv, _uploads_root

    safe_conv = _sanitize_conv(conversation_id) or "general"
    root = _uploads_root().resolve()
    lines: list[str] = []
    for ref in videos:
        if not _FILE_ID_RE.fullmatch(ref.file_id or ""):
            raise HTTPException(
                status_code=400, detail=f"视频 file_id 形态非法: {(ref.file_id or '')[:16]}")
        name = (ref.filename or "").replace("\\", "/").split("/")[-1][:120].strip() or "unnamed"
        if Path(name).suffix.lower() not in ALLOWED_VIDEO_EXTS:
            raise HTTPException(
                status_code=400, detail=f"附件 {name} 不是支持的视频类型")
        path = _resolve_upload_copy(root, safe_conv, ref.file_id, name)
        if path is None:
            raise HTTPException(
                status_code=400,
                detail=f"视频附件已失效（上传副本不存在，会话可能已被清理）: {name}")
        size_mb = path.stat().st_size / (1024 * 1024)
        lines.append(f"- {name}（{size_mb:.1f}MB）\n  路径: {path}")
    return (
        "\n\n[视频附件] 用户随消息附带了视频文件。当前模型无法直接观看视频内容，"
        "如需了解可使用 shell 工具运行 ffprobe/ffmpeg 检查元数据"
        "（时长/编码/分辨率/音轨等），例如：ffprobe -v error -show_format -show_streams <路径>\n"
        + "\n".join(lines)
        + "\n若视频与本次任务无关请忽略本节。"
    )


class AgentRunRequest(BaseModel):
    """Agent 运行请求"""
    task: str = Field(..., min_length=1, max_length=50000)
    conversation_id: str
    model_id: str = ""
    system_prompt: Optional[str] = None
    ssh_session_id: Optional[str] = None
    # 目标环境标记（test/prod）— 驱动生产环境高风险命令的审计与审批强化
    environment: Literal["test", "prod"] = "test"
    # 可见终端模式：True 时 shell_exec 命令交付到可见终端（前端 PTY/SSH）而非 headless 执行
    visible_terminal: bool = False
    # 深度思考开关：None = 客户端未表态（交给演化配置），True/False = 用户显式选择。
    # 原实现是 bool = False，无法区分「没选」与「选了关」，导致演化一旦学会
    # 开启 thinking，用户就再也关不掉（前端始终显式发送该字段时才走 None 分支）。
    enable_thinking: Optional[bool] = None
    enable_tools: bool = True
    # E 修复：max_iterations 边界校验（1-500），防 LLM/恶意请求传 0 或天文数字
    max_iterations: int = Field(100, ge=1, le=500)
    skill_ids: list[str] = Field(default_factory=list)
    # 逐 run 插件收紧（用户 2026-11-16）：None = 客户端未表态（继承全部已授权
    # 插件，与引入前行为逐字节等价）；list（可空）= 本次只装这些，与专家允许集
    # 再求交。授权本身仍在设置同意流（enabled+consented 硬门槛不动），这里只是
    # 逐 run 收紧 —— 空数组是「本次一个插件都不带」的合法表态，不能用 falsy 判断。
    selected_plugin_ids: Optional[list[str]] = None
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
    # 权限模式。值域用 Literal 校验，与 mode / environment 同惯例：
    # 非法值 → 422，不会静默退化成普通模式（那会让"以为开了"与"实际可写"
    # 之间出现落差）。默认 "normal" —— 既有前端不传该字段时行为完全不变。
    # "plan" 时 reins.plan_mode 强制只读：写工具与写命令一律拒绝，只允许调研
    # 与产出计划本身（对齐 Claude Code 的 permission modes）。
    # "auto" 时 reins.auto_approve 在弹审批卡前先让模型裁决一次：ALLOW 放行
    # （审计记 auto_allowed），超时/异常/存疑一律照旧弹卡问用户（fail-closed）。
    # prod 预授权强化与 hardline 阻断不受该档影响。
    permission_mode: Literal["normal", "auto", "plan"] = "normal"
    # ── 长任务结构化（spec 2026-10-24-task-structuring）：三个可选字段均向后兼容 ──
    # enhanced：本次下达的 task 是否为「已采用」的结构化稿（task 即 structured_text）。
    # source_raw_text：结构化前的原文，落进用户消息的 agent_trace JSON 列（无 schema
    #   迁移），让审计能同时看到"实际下达的结构稿"与"用户原文"—— 对应审计诚实性。
    # enhanced_has_added_write_steps：结构稿含"新增写步骤"。为真时后端把本次 run 的
    #   auto 审批 fail-closed 回落 normal —— 增强扩出来的写动作不该被模型代判顺带放行。
    enhanced: bool = False
    source_raw_text: Optional[str] = Field(default=None, max_length=50000)
    enhanced_has_added_write_steps: bool = False
    # 视觉多模态：随消息下发的图片附件（本体在 uploads/，见 ImageRef）。
    # 不传 = 纯文本 run，与引入前完全一致。
    images: Optional[list[ImageRef]] = None
    # 视频附件（ImageRef 复用同形态：file_id + filename）。模型无法直接观看，
    # 后端把存储路径与 ffprobe 分析提示注入任务文本（见 _load_run_video_note）。
    videos: Optional[list[ImageRef]] = None
    # 专家（运行策略配置包）。None = 完全沿用现有行为；服务端按 id 读 DB，
    # 禁用/不存在/配置损坏一律报错，不静默回落成默认专家（否则用户以为
    # 在收紧，实际跑的是全权限默认档）。
    expert_id: Optional[str] = None


# ── Routes ──

# C-review MINOR-12：同会话并发 run 防护。
# 原实现同一 conversation 可并发多个 run：消息交错写入、授权互相消费、
# 备份/文件互覆。这里用进程级 {conversation_id: run_id} 标记进行中的 run，
# 请求时原子地 check-and-set，事件流 finally 中释放。
_in_flight_runs: dict[str, str] = {}
_in_flight_lock = asyncio.Lock()

# O1：全局并发闸 —— 上面的 _in_flight_runs 只保证「同会话单 run」，多会话并发
# 仍能同时打爆一个 LLM 端点。这里加进程级并发上限：拿不到槽位立即 429（不排队），
# 因为 SSE 排队期间前端停滞看门狗（90s）会误判黑洞连接；单用户桌面场景 4 个
# 并发已很宽裕，触发即明确告知重试，比静默排队更可预期。
_MAX_CONCURRENT_AGENT_RUNS_DEFAULT = 4
_global_run_count = 0


def _agent_run_concurrency_limit() -> int:
    """并发上限：app_settings 可配，脏值/越界回默认 4。"""
    try:
        raw = db.get_app_setting("agent.max_concurrent_runs")
        value = int(raw) if raw is not None and str(raw).strip() else _MAX_CONCURRENT_AGENT_RUNS_DEFAULT
    except Exception:
        return _MAX_CONCURRENT_AGENT_RUNS_DEFAULT
    return value if 1 <= value <= 32 else _MAX_CONCURRENT_AGENT_RUNS_DEFAULT


async def _acquire_global_run_slot() -> bool:
    """原子抢占一个全局 run 槽位；满则返回 False（调用方回 429）。"""
    global _global_run_count
    async with _in_flight_lock:
        if _global_run_count >= _agent_run_concurrency_limit():
            return False
        _global_run_count += 1
        return True


def _release_global_run_slot_sync() -> None:
    """释放全局 run 槽位（同步，供取消免疫的清理段调用）。"""
    global _global_run_count
    _global_run_count = max(0, _global_run_count - 1)


async def _acquire_run_slot(conversation_id: str, run_id: str) -> Optional[str]:
    """原子地为 conversation 占位。返回 None 表示成功占位；返回已有 run_id 表示冲突。

    自愈：占位者的 DB 行已存在且处于终态（completed/failed/human_help/max_reached —
    cleanup finally 被取消打断的陈旧残留）时，记日志并接管占位，避免会话被永久锁死。
    注意：DB 行缺失不算陈旧（占位先于 create_agent_run，新 run 可能有毫秒级
    窗口还没有行）— 误接管活 run 的代价高于多等一次。
    """
    # 终态集合按需导入（见文件头说明）
    from agent.harness.contracts import TERMINAL_RUN_STATES

    async with _in_flight_lock:
        existing = _in_flight_runs.get(conversation_id)
        if existing:
            stale = False
            status_text = ""
            try:
                row = await asyncio.to_thread(db.get_agent_run, existing)
                if row is not None:
                    status_text = row.get("status", "")
                    # 终态集合取自 contracts.TERMINAL_RUN_STATES（唯一源）。漏掉
                    # human_help / max_reached 时，这两种结束的 run 的内存占位永不
                    # 自愈 → 该会话永久报「已有运行中的 Agent 任务」
                    stale = row.get("status") in TERMINAL_RUN_STATES
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


async def persist_interrupted_run(
    *,
    conversation_id: str,
    run_id: str,
    total_content: str,
    total_reasoning: str,
    tool_calls: list[dict],
    iterations: int,
    model_used: Optional[str],
) -> bool:
    """S1-01：断连/中止的 run 补落一条承载轨迹的 assistant 消息，返回是否真的写了。

    原先整个落库区都在 SSE 的 done 分支里，而客户端断连时 engine_producer 在
    `is_disconnected()` 处 break → sentinel → 消费循环正常退出，**done 分支根本没跑到**：
    用户点「停止」后重载历史，只看到自己那句提问、半个回复都没有，而 tool_calls 行
    早已写进库（证据在、承载体没了）。文案与形状沿用 done 分支「无正文也要落一条
    承载轨迹」的同一口径。

    流式期间什么都没产出（无正文、无推理、无工具调用）时**不写** —— 那种 run 连
    「Agent 做过什么」都没有可展示的证据，插一条空消息等于把「我按了停止」伪造成
    一句助手发言。

    后处理（标题/记忆/技能提取）刻意不跑：它学的是「这一轮的完整答案」，而断连留下
    的是半截输出，喂进去会用残缺样本污染记忆库。这一半是取舍、不是遗漏。
    """
    if not (total_content or total_reasoning or tool_calls):
        return False
    snapshot = {
        "status": "failed",
        # 与真失败的区别只记在这里：终态确实没跑完，原因是被中断而非模型/工具报错
        "currentState": "interrupted",
        "iterations": iterations,
        "toolCalls": tool_calls,
        "reasoning": total_reasoning or "",
        "lastRunId": run_id,
        # costInfo 缺席 = 「没读到」而不是 0：断连路径拿不到 done 事件的统计，
        # 写全 0 会把实际已消耗的 token 报成没消耗（读侧 0 塌陷的同一族）。
        "errorMessage": None,
    }
    await asyncio.to_thread(
        db.add_message,
        conversation_id=conversation_id,
        role="assistant",
        content=total_content or "(AI 运行中断，未产生输出)",
        reasoning=total_reasoning if total_reasoning else None,
        model_used=model_used,
        run_id=run_id,
        agent_trace=snapshot,
    )
    return True


def _release_run_slot_sync(conversation_id: str, run_id: str) -> None:
    """同步释放占位（无 await，取消安全）。

    仅当当前占位者仍是本 run 时删除，避免误删后续 run 的占位。
    asyncio 单线程事件循环内无 await 点 = 无交错可能，dict 操作等价原子。
    故障根因修复：客户端断连触发任务 cancel 时，finally 里带 await 的
    释放会被 CancelledError（BaseException）打断 → 占位永久残留。
    """
    if _in_flight_runs.get(conversation_id) == run_id:
        del _in_flight_runs[conversation_id]



# ── run 级环境判定登记 ──────────────────────────────────────────────
# 审批端点（grant / deny-command）只有 run_id，没有 environment 字段 —— 原先它们
# 写审计时 environment 取默认值 "test"，于是对 prod 主机的授权在审计页的 prod
# 视图里**根本不存在**，而且 approve 记录一律显示成测试环境的审批。
# 这里把服务端判定结果（agent.environment_policy）按 run_id 登记，授权端点据此
# 写对的环境列与主机作用域；run 终态时移入有界墓碑（见下方 _RUN_ENV_GRAVE）。
_RUN_ENV: dict[str, EnvironmentResolution] = {}
# 已结束 run 的判定结果墓碑：审批卡不随 run 结束消失（用户可以在 run 终态后补点
# 「始终允许」），而 _RUN_ENV 在 event_stream 的 finally 里就被清掉了。不留墓碑的话，
# 晚点批准会拿不到 host → 落成全局条目 —— 在 prod 主机上批准的授权，只要多点几分钟
# 就变成对所有主机生效，主机维度被静默绕过。
# 有界（最近若干次 run）：只存判定小对象，超出按插入顺序淘汰。
_RUN_ENV_GRAVE: dict[str, EnvironmentResolution] = {}
_RUN_ENV_GRAVE_MAX = 256


def _remember_run_environment(run_id: str, resolution: "EnvironmentResolution") -> None:
    _RUN_ENV[run_id] = resolution


def _forget_run_environment(run_id: str) -> None:
    resolution = _RUN_ENV.pop(run_id, None)
    # host 为空（本地 run / 桥接不可用）没有保留价值：补批准的授权本来就落全局，
    # 与登记时的匹配口径一致。
    if resolution is None or not resolution.host:
        return
    _RUN_ENV_GRAVE[run_id] = resolution
    while len(_RUN_ENV_GRAVE) > _RUN_ENV_GRAVE_MAX:
        _RUN_ENV_GRAVE.pop(next(iter(_RUN_ENV_GRAVE)))


def _run_resolution(run_id: str) -> Optional["EnvironmentResolution"]:
    """该 run 的环境判定：进行中的登记优先，其次已结束 run 的墓碑。"""
    key = run_id or ""
    return _RUN_ENV.get(key) or _RUN_ENV_GRAVE.get(key)


def _run_environment(run_id: str) -> str:
    """该 run 的权威环境标签；未登记（旧 run / 测试直调）时回落默认值。"""
    resolution = _run_resolution(run_id)
    return resolution.effective if resolution else audit.DEFAULT_ENVIRONMENT


def _run_host(run_id: str) -> str:
    """该 run 的目标主机（服务端从连接配置反查）；未登记时返回空串。

    空串不是错误：它意味着「没核实过目标主机」，写入端据此落全局条目，
    与匹配端「host 为空只查全局」对称 —— 否则会出现「授权已生效但永不命中」
    的静默故障（用户反复弹卡而面板显示已授权）。

    查墓碑：run 终态后补点「始终允许」的卡片仍需落回原主机（见 _RUN_ENV_GRAVE）。
    """
    resolution = _run_resolution(run_id)
    return resolution.host if resolution else ""


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
        retention = await asyncio.shield(asyncio.to_thread(db.get_conv_retention))
        if retention.get("enabled"):
            pruned_ids = await asyncio.shield(
                asyncio.to_thread(db.prune_conversations, retention.get("keep_count", 1000)))
            # 被回收会话的 session 授权键随会话作废：这是与用户删除
            # （DELETE /chat/conversations/{cid}）并列的第二条会话终结路径，
            # 漏了它，保留清理删掉的会话照样把授权键留在进程内逐会话累积。
            # gate 状态操作留在事件循环线程（与 grant/deny 端点同一姿态），
            # DB 删除已在线程池完成。
            if pruned_ids:
                from agent.harness.reins import _get_global_permission_gate_sync
                gate = _get_global_permission_gate_sync()
                for cid in pruned_ids:
                    gate.cleanup_conversation_session(cid)
    except Exception:
        logger.warning("会话保留清理失败（best-effort，不阻塞）", exc_info=True)


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
        # 第一步：崩溃在「快照落库 → 状态改写」之间的 run 救回 waiting_confirmation
        # （先于 mark_stale —— 顺序反了会把这些 run 标成 failed）
        from db.run_checkpoints import promote_waiting_checkpoint_runs
        promoted = await asyncio.to_thread(promote_waiting_checkpoint_runs)
        if promoted:
            logger.warning(
                "启动对账: %d 条持待确认快照的滞留 run 已恢复为 waiting_confirmation",
                promoted)
        # 第二步：剩余滞留 running/init（无快照 = 真崩溃）标记 failed。
        # mark_stale 内部自带排除：持有待确认快照的 run 不会被误杀。
        n = await asyncio.to_thread(db.mark_stale_agent_runs_failed)
        if n:
            logger.warning("启动对账: %d 条滞留 running 的 agent_runs 已标记 failed", n)
    except Exception as e:
        logger.warning("启动对账失败（不影响本次运行）: %s", e)


async def _retrieve_memory_contents(
    embed_client, *, query: str, scopes: list[str], settings: dict,
) -> list[str]:
    """v2 混合检索注入：Retriever 渲染行 + token 预算，仅注入者计热度。

    兜底只在检索故障（超时/异常）时触发；空结果是相关性判定的合法结论
    （v1 P1 教训保留：故障降级按 recency 取 active，不做相关性判定）。
    """
    contents: list[str] = []
    timeout_s = settings["search_timeout_s"]
    retrieve_failed = False
    try:
        contents = await asyncio.wait_for(
            memory_retriever.retrieve_for_injection(
                query, scopes=scopes, embed_client=embed_client,
                budget_tokens=settings["inject_budget_tokens"],
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        retrieve_failed = True
        logger.warning("记忆检索超时（%ss），降级兜底", timeout_s)
    except Exception as e:
        retrieve_failed = True
        logger.warning("记忆检索失败，降级兜底: %s", e)

    if retrieve_failed and not contents:
        try:
            # 兜底要带上正常路径同两条约束（scopes + 注入预算）。原写法只传 limit：
            # 降级后别的没有，反而比正常注入更宽 —— host:B 的 run 会把 host:A 的经验
            # 当成事实塞进 prompt，且实测一条兜底就吃掉 1975 token（预算 1500）。
            contents = await asyncio.to_thread(
                mstore.recent_fallback_lines, 5, scopes,
                settings["inject_budget_tokens"],
            )
        except Exception as e:
            logger.warning("记忆检索降级兜底同样失败，放弃注入: %s", e)
    return contents


@router.post("/agent/run")
async def agent_run(req: AgentRunRequest, request: Request):
    """启动 Agent — SSE 流式返回运行过程"""
    # 执行栈按需导入（见文件头说明）：走到这里才真的要跑一次 run。
    # 导入落在函数作用域，下方 engine_producer 等嵌套函数经闭包可见。
    from agent.cost_tracker import CostTracker
    from agent.evolution import EvolutionEngine
    from agent.harness import Harness, HarnessConfig
    from agent.harness.contracts import AgentConfig as HarnessAgentConfig
    from agent.harness.llm_client import StreamLLMClient
    from agent.harness.telemetry import SSEAdapter
    from agent.tools import ToolRegistry, create_registry

    # C-review MINOR-12：进程启动后首次请求做滞留 run 对账
    await _reconcile_stale_runs_once()

    # ── 专家策略（可选）──
    # 只收紧六个运行面：模型 / 身份段 / 技能 / 工具 / 权限 / 迭代 / 记忆。
    # 未选专家时 policy.is_legacy 为真，下方每个干预点都不产生任何字节变化，
    # 守卫在 backend/tests/test_expert_policy_legacy_noop.py。
    from agent.expert_policy import (
        build_identity_section, build_snapshot, clamp_max_iterations,
        clamp_permission_mode, filter_tool_names, resolve_expert,
        resolve_memory_scopes, resolve_skill_ids,
    )
    expert_row = None
    if req.expert_id:
        expert_row = await asyncio.to_thread(db.get_expert, req.expert_id)
        if not expert_row:
            raise HTTPException(status_code=404, detail="专家不存在")
        if not expert_row.get("enabled"):
            raise HTTPException(status_code=400, detail="专家已被禁用")
    try:
        policy = resolve_expert(expert_row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"专家配置损坏: {exc}") from exc

    # 记忆范围在 setup 并行段与工具里必须是同一份口径；inherit 时不干预。
    _inherited_scopes = memory_retriever.context_scopes(req.ssh_session_id or "")
    final_memory_scopes = (
        _inherited_scopes if policy.memory_scope == "inherit"
        else resolve_memory_scopes(policy.memory_scope, req.ssh_session_id, _inherited_scopes)
    )

    # ── 按 SSH 模式选择默认模型（兜底）──
    # 审查 M-13：db 同步调用放线程池，避免阻塞事件循环（卡住所有并发请求/心跳）
    # 专家锁定模型时忽略前端选择（面板会显示「由专家指定」）；否则沿用原链路
    effective_model_id = policy.model_id_override or req.model_id
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, SSH_MODE)
        if default_model:
            # 审查外热修：优先行主键 id（原优先 LLM 模型名会 404 模型不存在）
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, None)
        if default_model:
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        raise HTTPException(status_code=400, detail="未找到默认模型，请先配置模型")

    req.model_id = effective_model_id

    model_cfg = await asyncio.to_thread(_get_model_config, req.model_id)
    if not model_cfg:
        raise HTTPException(status_code=404, detail=f"模型不存在: {req.model_id}")

    # 懒启动后台意识 tick — 仅在首次 Agent 运行时触发，
    # 避免纯聊天用户产生空 DB 查询开销
    get_global_tick().ensure_started()

    # ── setup 并行段一（Q-B4④）：互不依赖的读操作一次拉起，压首字节时延 ──
    # 依赖链钉死：记忆检索与画像（并行段二）依赖 mem_settings / embed_client /
    # is_conversation_task；本组内 history / skill 过滤 / 意图预识别 / 记忆设置 /
    # 会话目标 / 演化配置互不依赖（演化配置原在 prompt 构建后才读，但它只被
    # thinking/max_tokens 消费，提前无语义变化）。失败语义与串行一致：不可吞的
    # 异常按原串行顺序 re-raise（skills → history → mem_settings → goal →
    # evolved；skill 过滤失败尤其不得静默吞掉 —— 那会让被禁用的技能照常注入
    # prompt，原串行实现是 fail-fast）；只有 intention 保持内部降级不抛。
    async def _setup_history():
        # 获取历史消息（条数来自前端"上下文窗口"设置）
        return await asyncio.to_thread(
            db.get_recent_messages, req.conversation_id, limit=req.context_window)

    async def _setup_skills():
        # ── 按模式过滤显式 skill_ids；专家限定时再取允许集交集 ──
        # 专家即使前端没显式选技能也会预挂载自己的技能集，所以早退条件必须
        # 同时满足「前端没选」且「专家不干预」。
        if not req.skill_ids and policy.skill_ids is None:
            return
        disabled_skill_ids = set(await asyncio.to_thread(db.list_disabled_skills_for_mode, SSH_MODE))
        original_skill_ids = list(req.skill_ids or [])
        req.skill_ids = resolve_skill_ids(
            original_skill_ids, policy.skill_ids, disabled_skill_ids)
        if len(req.skill_ids) != len(original_skill_ids):
            logger.info(
                "Agent 显式 skill_ids 按 mode=%s 与专家策略过滤: %d -> %d",
                SSH_MODE, len(original_skill_ids), len(req.skill_ids),
            )

    _intention: dict = {"pre": None, "is_conv": False}

    async def _setup_intention():
        # ── 意图预识别：用于决定是否跳过记忆检索、是否禁用工具 schema ──
        # 对话型任务（问候/身份询问/能力询问）不需要记忆、不需要工具
        # 避免用户问"你是什么模型"时被无关记忆污染、被引导去调工具
        if not req.enable_intention_guide:
            return
        try:
            from agent.intention import classify as _classify_intention
            _intention["pre"] = _classify_intention(req.task)
            _intention["is_conv"] = (
                _intention["pre"].task_type.value == "conversation"
            )
        except Exception as e:
            logger.warning("意图预识别失败（降级为常规流程）: %s", e)

    async def _setup_mem_settings():
        # 读取记忆设置 — 关闭时跳过检索与保存
        ms = await asyncio.to_thread(db.get_memory_settings)
        # spec §8 统一预算：设置值只代表"想要多少"，窗口才代表"能给多少"，取小者。
        # 不夹住的话 8k 窗口的模型也会按 8000 token 注入记忆，压掉 history 与输出空间。
        _mem_budget = memory_budget.current(model_cfg.get("max_tokens"))
        ms["inject_budget_tokens"] = _mem_budget.injection
        ms["blocks_budget_tokens"] = _mem_budget.blocks
        return ms

    async def _setup_goal():
        # 结构化 goal/plan 装配（M1）：注入段 + 只读强制推导 + per-run 指针。
        # load_goal_plan_state 内部已降级（失败返空态、不抛），与旧
        # _load_active_goal_text 的「绝不拖垮 run」语义一致。
        from routes._goal_plan_setup import load_goal_plan_state
        return await asyncio.to_thread(load_goal_plan_state, req.conversation_id or "")

    evolution_mgr = EvolutionEngine()

    async def _setup_evolved():
        return await asyncio.to_thread(_load_evolved_config, evolution_mgr)

    _setup_results = await asyncio.gather(
        _setup_history(), _setup_skills(), _setup_intention(),
        _setup_mem_settings(), _setup_goal(), _setup_evolved(),
        return_exceptions=True,
    )
    # skills(1) 同样不可吞：过滤失败却继续跑 = 被禁用技能照常注入（fail-fast，
    # 与原串行一致）。按原串行顺序 re-raise：skills 先于 history。
    for _idx in (1, 0, 3, 4, 5):
        if isinstance(_setup_results[_idx], BaseException):
            raise _setup_results[_idx]
    history = _setup_results[0]
    pre_intention = _intention["pre"]
    is_conversation_task = _intention["is_conv"]
    mem_settings = _setup_results[3]
    memory_enabled = mem_settings["enabled"]
    goal_text = _setup_results[4]["injection"]
    gp_force_plan = bool(_setup_results[4]["force_plan"])
    _evolved_cfg = _setup_results[5]

    # embedding 客户端（与 run 模型同源，LRU 缓存单例；失败仅无向量，不造伪向量）
    embed_client = get_embedding_client(
        model_cfg["base_url"],
        model_cfg["api_key"],
        model_cfg.get("model_id") or model_cfg.get("name", ""),
    )

    # ── setup 并行段二（Q-B4④）：依赖 mem_settings/is_conversation_task 的两项 ──
    # 记忆检索（embedding + 混合检索）与目标机画像探测（SSH 往返，超时上限 10s）
    # 互不依赖，是 setup 里最贵的两段，并行拿回。
    async def _setup_memory_contents():
        # v2 混合检索：episodic/semantic/procedural 统一记忆，scope 硬过滤
        # 对话型任务跳过记忆检索（避免无关记忆污染简单对话）
        if not (memory_enabled and not is_conversation_task):
            return []
        return await _retrieve_memory_contents(
            embed_client, query=req.task,
            scopes=final_memory_scopes,
            settings=mem_settings,
        )

    async def _setup_profile():
        # ── 画像（常驻上下文）：目标机探测 → host-profile 块 → [画像] 段 ──
        # 探测是一条只读、免审命令（ops_obs.host_profile_cmd ↔ 远端 host_profile 模板），
        # 失败/超时/目标机未部署该模板一律降级为"环境未知"文案，绝不影响 run
        # （见 agent/host_profile.py 硬约束）。
        # 只有真实环境事实才写进画像块；降级文案是"本轮没探到"的说明而非机器事实，
        # 写进块会让一次失败常驻到下次成功，读起来像确凿结论 —— 故只在当轮直接注入。
        # 对话型任务跳过：纯寒暄不需要一次远程往返，也不需要环境事实。
        if is_conversation_task:
            return ""
        from agent.harness.contracts import RunContext as _RunContext
        from agent.host_profile import ensure_host_profile, is_known_profile
        from agent.memory import blocks as memory_blocks

        _host_ctx = _RunContext.new(
            task=req.task or "",
            ssh_session_id=req.ssh_session_id or "",
        )
        host_profile_text = await ensure_host_profile(_host_ctx)

        _profile_text = ""
        if memory_enabled:
            if req.ssh_session_id and is_known_profile(host_profile_text):
                try:
                    await memory_blocks.put(
                        memory_blocks.host_key(req.ssh_session_id),
                        host_profile_text, updated_by="probe",
                    )
                except Exception as e:
                    logger.warning("主机画像块写入失败（不影响 run）: %s", e)
                # 写完顺手裁剪：块键每次连接都是新的，不裁就只增不减，而每轮 run 的
                # render_for_context 会把这张表全量扫一遍（S14-06）
                try:
                    await memory_blocks.prune_host_profiles()
                except Exception as e:
                    logger.warning("主机画像块裁剪失败（不影响 run）: %s", e)
            try:
                _profile_text = await memory_blocks.render_for_context(
                    req.ssh_session_id or "", mem_settings["blocks_budget_tokens"],
                )
            except Exception as e:
                logger.warning("画像块渲染失败，降级为本轮探测文案: %s", e)
        # 记忆关闭时退回本轮探测文案；本轮未探到环境事实而块也为空时同样兜底，
        # 否则"工具不在本机执行""先探测再假设"这两条安全提示会整体消失。
        return _profile_text or host_profile_text

    _profile_results = await asyncio.gather(
        _setup_memory_contents(), _setup_profile(), return_exceptions=True)
    for _r in _profile_results:
        if isinstance(_r, BaseException):
            raise _r
    memory_contents: list[str] = _profile_results[0] or []
    profile_text = _profile_results[1] or ""

    # 批准门服务端推导（M1，取代 _paired_permission_mode）：存在等待批准 /
    # 已批准 / 执行中的结构化 plan 时，本次 run 强制只读档 —— HTTP 边界不信任
    # 客户端字段（前端只发审批档），PlanModeGuard 才是真正的强制。
    permission_mode = "plan" if gp_force_plan else req.permission_mode
    if gp_force_plan and req.permission_mode != "plan":
        logger.warning("会话存在待批准计划，permission_mode=%r 强制为 plan", req.permission_mode)
    # 结构化增强含新增写步骤时，auto 回落 normal（fail-closed，服务端不信任前端）。
    permission_mode = _structure_aware_permission_mode(
        req.enhanced_has_added_write_steps, permission_mode)
    # 专家是最后一道收紧：必须在配对与结构化回落之后，否则「最严格者」
    # 会算在已被降级的旧值上；也绝不把 plan 抬回 normal/auto。
    permission_mode = clamp_permission_mode(permission_mode, policy.permission_ceiling)

    # M3 修复：提取 system prompt 构建为独立函数
    # 专家身份段在组装器内紧随基础身份注入（spec §5），不再末尾追加。
    final_system_prompt = _build_agent_system_prompt(
        req,
        profile_text,
        is_conversation_task,
        is_change=pre_intention.is_change if pre_intention else None,
        goal_text=goal_text,
        identity_section=build_identity_section(policy),
    )

    # ── 插件工具装配（MCP）──
    # 只加载「已启用 + 已确认」且被专家允许的插件；单个插件失败只记 warning，
    # 不阻断整个 run。工具实例此刻先备好，注册放到 tool_registry 创建之后。
    from agent.run_assembly import (
        PLUGIN_PROMPT_SECTION, build_plugin_tools, register_plugin_tools,
    )
    plugin_tools, plugin_warnings = await build_plugin_tools(
        policy, selected_plugin_ids=req.selected_plugin_ids)
    for _warning in plugin_warnings:
        logger.warning("插件装配: %s", _warning)
    if plugin_tools:
        final_system_prompt = final_system_prompt + "\n\n" + PLUGIN_PROMPT_SECTION

    # 应用演化配置（仅覆盖用户未显式传入的字段）
    # M3 修复：提取演化配置解析为独立函数
    # 审查 M-16：传入 prompt 文本（safe_max_tokens 内部用 estimate_tokens 估算，
    # 不再按 1.5 token/字符高估导致长对话输出被截到 256）
    _prompt_text = "\n".join(
        [req.task or "", final_system_prompt or ""]
        + [m.get("content", "") for m in history if isinstance(m.get("content", ""), str)]
        + [m for m in memory_contents if isinstance(m, str)]
    )
    # 演化配置可能把 thinking 打开，所以 thinking 必须先于 safe_max_tokens 定稿
    # （Q-B4④：_evolved_cfg 已在 setup 并行段一提前取回，此处直接消费）
    thinking_on = _resolve_thinking(req, _evolved_cfg)
    _safe_max = safe_max_tokens(
        model_cfg.get("max_tokens"),
        _prompt_text,
        model_cfg.get("max_output_tokens"),
        model_cfg.get("reasoning_tokens") if thinking_on else None,
    )
    _evo = _resolve_evolution_config(req, model_cfg, _safe_max, _evolved_cfg, thinking_on)
    effective_thinking = _evo["thinking"]
    effective_max_iter = _evo["max_iter"]
    # 专家上限在演化配置之后取 min：动态预算同样不能突破它
    effective_max_iter = clamp_max_iterations(effective_max_iter, policy.max_iterations)
    effective_temperature = _evo["temperature"]
    effective_top_p = _evo["top_p"]
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
    tool_registry = create_registry() if effective_enable_tools else ToolRegistry()
    # 专家工具集与「已按全局/SSH 模式过滤后」的注册表求交：
    # 顺序上必须是 create_registry 之后，专家才无法复活被禁用的工具。
    if policy.tool_names is not None and effective_enable_tools:
        _allowed_tools = set(filter_tool_names(tool_registry.names(), policy.tool_names))
        for _tool in list(tool_registry.names()):
            if _tool not in _allowed_tools:
                tool_registry.unregister(_tool)
    # 插件工具：专家显式工具集对它们同样生效；命名冲突时整批不载入，
    # 绝不覆盖内置工具（register_plugin_tools 内部先查重再注册）。
    if plugin_tools and effective_enable_tools:
        if policy.tool_names is not None:
            plugin_tools = [t for t in plugin_tools if t.name in policy.tool_names]
        _conflicts = register_plugin_tools(tool_registry, plugin_tools)
        if _conflicts:
            logger.warning("插件工具名冲突，整批未载入: %s", _conflicts)
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

    # O1：全局并发闸 —— 拿不到槽位立即 429（不排队，避免前端 90s 看门狗误判
    # 黑洞连接）。前端 sse.ts 对非 200 走 onError(detail)，显示这条中文提示。
    if not await _acquire_global_run_slot():
        raise HTTPException(
            status_code=429,
            detail="同时运行的 Agent 任务过多，请等待当前任务结束后重试",
        )

    # C-review MINOR-12：同会话并发防护 — 原子 check-and-set，冲突返回 409
    # （前端 streamAgentRun 对非 200 走 onError(detail)，用户可见提示）
    _conflict = await _acquire_run_slot(req.conversation_id, run_id)
    if _conflict:
        _release_global_run_slot_sync()
        raise HTTPException(
            status_code=409,
            detail=f"该会话已有运行中的 Agent 任务（run_id={_conflict}），请等待其结束或终止后再试",
        )

    # 挂起语义配套守卫：挂起会释放内存占位（run task 已返回），DB 状态才是
    # 权威 —— 同会话存在 running/init/waiting_confirmation 的 run 时必须 409，
    # 否则新 run 会和「等待用户确认的旧 run」在同一会话里并发执行。
    # 不自动取消旧卡（用户没答的题不许被悄悄丢弃），前端引导先处理卡片或取消。
    try:
        _db_conflict = await asyncio.to_thread(
            db.get_agent_runs, conversation_id=req.conversation_id, limit=5,
        )
        _waiting = [
            r for r in (_db_conflict or [])
            if str(r.get("status") or "") in ("running", "init", "waiting_confirmation")
        ]
        if _waiting:
            _release_run_slot_sync(req.conversation_id, run_id)
            _release_global_run_slot_sync()
            _old = _waiting[0]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"该会话有一个等待确认/运行中的任务（run_id={_old.get('id')}，"
                    f"status={_old.get('status')}）。请先提交该任务的澄清答案或取消它，再发起新任务。"
                ),
            )
    except HTTPException:
        raise
    except Exception as e:
        # 对账查询失败不拦 run（降级为只靠内存占位），但要留痕
        logger.warning("同会话 DB 级并发守卫查询失败（放行）: %s", e)

    # P0-1：环境标签由服务端判定（见 agent.environment_policy）。
    # req.environment 只算一个「上限请求」：主机连接配置标了 prod 就不允许降级成
    # test —— 否则一次请求就能把生产强化的「危险命令仅预登记 allowlist」整条绕开。
    # 拿不到主机信息时不拦 run，但 source 会一路带到审批卡与审计记录（未核实）。
    env_resolution = await resolve_run_environment(
        req.environment, req.ssh_session_id or "")
    _remember_run_environment(run_id, env_resolution)
    # 图片附件：在落 run 记录之前解析（400 早退，不留孤儿 run 记录）。
    # 解析在 try: 之外、占位获取之后 —— 400/413 直抛会绕过 finally 清理，
    # 而 DB run 行尚不存在、自愈逻辑永不触发，占位泄漏 = 该会话永久 409。
    # 故此处失败必须显式释放占位与环境登记后再抛。
    try:
        image_blocks = await _load_run_image_blocks(req.images, req.conversation_id)
        # 视频附件：解析成任务文本后缀（只改三处"模型/审计看到的内容"，意图识别、
        # 记忆检索等仍用原始 task，避免注解污染意图判定）
        video_note = await _load_run_video_note(req.videos, req.conversation_id)
    except BaseException:
        _release_run_slot_sync(req.conversation_id, run_id)
        _release_global_run_slot_sync()
        _forget_run_environment(run_id)
        raise
    effective_task = req.task + video_note
    try:
        # 持久化 agent_run 记录（审查 M-13：放线程池）
        # 专家快照：只记最终生效值与策略元数据，绝不记 api_key / 插件 env
        import json as _json
        _expert_snapshot = build_snapshot(
            policy,
            model_id=model_cfg.get("model_id") or model_cfg["id"],
            permission_mode=permission_mode,
            max_iterations=effective_max_iter,
            tool_names=list(tool_registry.names()) if effective_enable_tools else [],
            skill_ids=list(req.skill_ids or []),
            memory_scopes=final_memory_scopes,
        )
        await asyncio.to_thread(
            db.create_agent_run,
            run_id=run_id,
            conversation_id=req.conversation_id,
            # 审计口径：带视频时实际下发的是 task + 视频提示，原样落库
            task=effective_task,
            # legacy 时保持原值，专家生效时记实际钳制后的迭代数
            max_iterations=req.max_iterations if policy.is_legacy else effective_max_iter,
            mode=SSH_MODE,
            model_used=model_cfg.get("model_id") or model_cfg["id"],
            expert_id=expert_row["id"] if expert_row else None,
            expert_name=policy.expert_name,
            expert_snapshot_json=(
                _json.dumps(_expert_snapshot, ensure_ascii=False) if expert_row else None
            ),
        )

        # 保存用户消息到 db（修复：原 Agent 模式未保存用户消息，导致 post_process 的标题生成检查 len(recent)==2 失败，
        # 且历史会话切换时丢失用户问题）
        # 结构化增强时把原文塞进 agent_trace（既有 JSON 列，不新增 schema）：
        # content 是实际下达的结构稿，agent_trace.task_structuring.raw_text 是被替换的原文，
        # 审计页可对照"用户以为发的是 A、实际下达的是结构化后的 B"。
        _user_msg_trace = None
        if req.enhanced and req.source_raw_text:
            _user_msg_trace = {
                "task_structuring": {
                    "enhanced": True,
                    "raw_text": req.source_raw_text,
                    "has_added_write_steps": req.enhanced_has_added_write_steps,
                }
            }
        await asyncio.to_thread(
            db.add_message,
            req.conversation_id,
            "user",
            effective_task,
            model_used=model_cfg.get("model_id") or model_cfg["id"],
            agent_trace=_user_msg_trace,
        )
        await asyncio.to_thread(db.touch_conversation, req.conversation_id)
        await asyncio.to_thread(db.update_conversation_status, req.conversation_id, "in_progress")
    except Exception:
        # 占位已得但落库失败 → 释放占位，避免该会话被永久锁死
        _release_run_slot_sync(req.conversation_id, run_id)
        _release_global_run_slot_sync()
        _forget_run_environment(run_id)
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
                    # 模型行的核采样：此前这一层没有 top_p 字段，chat_stream 恒用
                    # 自己的 0.9 默认，设置页改 top_p 对 Agent 运行毫无影响
                    top_p=effective_top_p,
                    # H13 修复 C7：应用演化后的 max_tokens（原实现硬编码 _safe_max）
                    max_tokens=effective_max_tokens,
                    # 推理/思考内容的 token 预算上限（来自模型配置，作为意图识别预算的上限）
                    max_reasoning_tokens=model_cfg.get("reasoning_tokens"),
                    enable_thinking=effective_thinking,
                    enable_tools=effective_enable_tools,
                    # 模型能力门：标记不支持工具调用的模型预判去 tools（三态语义见 AgentConfig）
                    supports_tool_calling=(
                        bool(model_cfg["supports_tool_calling"])
                        if model_cfg.get("supports_tool_calling") is not None else None
                    ),
                    # 备用模型回退：解析主模型的备用端点完整配置（None=不启用）
                    fallback_model_cfg=db.resolve_fallback_model_cfg(model_cfg),
                    enable_intention_guide=req.enable_intention_guide,
                    enable_dynamic_budget=req.enable_dynamic_budget,
                    # C-review M10：成本/token 硬熔断
                    max_cost_usd=req.max_cost_usd,
                    max_total_tokens=req.max_total_tokens,
                )
                async for trace_event in harness.run(
                    # 视频提示注入在此处生效（模型首条消息 = effective_task）；
                    # 意图识别/记忆检索等上游环节仍用原始 req.task
                    task=effective_task,
                    conversation_id=req.conversation_id,
                    ssh_session_id=req.ssh_session_id,
                    environment=env_resolution.effective,
                    environment_source=env_resolution.source,
                    target_host=env_resolution.host,
                    enable_visible_terminal=req.visible_terminal,
                    system_prompt=final_system_prompt,
                    history=history,
                    memories=memory_contents,
                    config=harness_config,
                    run_id=run_id,
                    skill_ids=req.skill_ids,
                    auto_skill_match=req.auto_skill_match,
                    permission_mode=permission_mode,
                    # 技能允许集让 SkillRein 连触发词自动匹配也一起收紧；
                    # memory_scopes 让预检索与 memory_search 工具读同一份范围。
                    expert_skill_ids=(
                        list(policy.skill_ids) if policy.skill_ids is not None else None
                    ),
                    memory_scopes=(
                        final_memory_scopes if policy.memory_scope != "inherit" else None
                    ),
                    # 视觉多模态：随消息下发的图片（image_url 内容块，None=纯文本）
                    images=image_blocks,
                    # 交互式 run：审批卡弹出后 Driver 真的停住等用户点击。
                    # 不传的入口（定时任务）没人可点卡片，保持「拦截即返回」。
                    await_approval=True,
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
        # error 事件是否到达过（反思调度的闸门，见循环末尾）
        error_seen = False
        # S1-01 闸门：assistant 消息是否已落。done 分支落两次（有正文/无正文）中的
        # 任一次置真，finally 的兜底段据此跳过 —— 断连的 run 走不到 done 分支，
        # 那里留下的痕迹是**唯一**一份，不会与 done 的双写。
        assistant_persisted = False
        # S1-01 兜底段的 iterations 来源：done 事件带不到断连路径，取最后观测到的迭代号
        last_iteration = 0
        # ── 挂起（waiting_confirmation）闸门 ──
        # 永久等待语义：澄清待确认点会让 driver 结束生成器而不发 done。
        # 挂起不是结束 —— 不清卡片、不写终稿、不触发反思、finally 的六类
        # 危险收尾（杀远程任务/写中断消息/标 failed/复位会话状态等）全部跳过；
        # 恢复由 POST /agent/runs/{run_id}/resume 以同一 run_id 开新流。
        run_suspended = False
        suspended_pending: dict = {}

        # ── Q-B4③：tool_call 落库挪到单写者后台任务 ──
        # SSE 消费循环每次 yield 前不再被逐条 sqlite 写阻塞（add/update 各一次
        # 连接开关，几十个工具调用的 run 光轨迹落库就吃掉数百毫秒吞吐）。
        # 单写者按入队顺序消费，start 先于 result 落库；配对键用内存序号 seq，
        # db 行 id 由写者在 add 完成后回填进 tc_db_ids（result 的 UPDATE 取用）。
        tc_write_q: asyncio.Queue = asyncio.Queue()
        tc_seq_counter = 0
        tc_db_ids: dict[int, str] = {}

        async def _tc_writer():
            while True:
                job = await tc_write_q.get()
                try:
                    if job is None:
                        return
                    await job()
                except Exception as e:
                    logger.warning("tool_call 后台落库失败: %s", e)
                finally:
                    tc_write_q.task_done()

        tc_writer_task = asyncio.create_task(_tc_writer())

        # H14 修复：立即发送 state=thinking 事件，让前端马上显示"思考中..."提示
        # 原问题：从 fetch 发起到 driver 发出第一个 state 事件之间可能等 2-5 秒
        # （后端预处理：意图识别 + 记忆检索 embedding + 反思加载 + LLM 首 token）
        # 这期间前端 UI 完全空白，用户以为卡住了
        yield sse_event({"type": "state", "state": "thinking", "iteration": 0, "run_id": run_id})

        # 运维专家建议（非阻塞增益，spec §6.3）：未选专家 + 非纯对话 + 运维信号 → chip。
        # 判定失败静默 no-op，绝不拖 run。当前 run 继续以通用身份跑（能力不缺，只缺方法论纪律）。
        try:
            from agent.expert_policy import builtin_general_ops
            from agent.expert_suggest import should_suggest_ops
            if should_suggest_ops(
                req.task or "",
                expert_selected=bool(req.expert_id),
                is_conversation=is_conversation_task,
            ):
                _ops = builtin_general_ops()
                yield sse_event({
                    "type": "expert_suggestion",
                    "slug": _ops["slug"],
                    "name": _ops["name"],
                    "reason": "检测到运维任务，建议切到运维",
                    "task": req.task or "",
                    "run_id": run_id,
                })
        except Exception as e:
            logger.warning("运维专家建议判定失败（跳过）: %s", e)

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
                    # S1-01：兜底段的进度证据只有这个迭代号可累积
                    last_iteration = max(last_iteration, int(event.get("iteration", 0) or 0))
                    # 记录工具调用开始（Q-B4③：落库入队后台单写者，不阻塞 yield）
                    tc_seq_counter += 1
                    _seq = tc_seq_counter
                    _it = event.get("iteration", 0)
                    _tool_name = event["tool"]
                    _args_json = json.dumps(event.get("args", {}), ensure_ascii=False)

                    async def _add_tc(_s=_seq, _it=_it, _t=_tool_name, _aj=_args_json):
                        tc_db_ids[_s] = await asyncio.to_thread(
                            db.add_tool_call,
                            agent_run_id=run_id,
                            iteration=_it,
                            tool_name=_t,
                            arguments=_aj,
                        )

                    await tc_write_q.put(_add_tc)
                    # H10 修复：保存序号以便 tool_call_result 时回写结果
                    pending_tool_call_ids.append(_seq)
                    run_trace_tool_calls.append({
                        # 前端 AgentTraceSnapshot.toolCalls[].id 是列表 :key 与结果
                        # 配对用的身份键，缺失时重载会话后每条记录的 key 都是 undefined
                        # （Q-B4③：键改用 run 内序号，同 run 内唯一即满足配对语义）
                        "id": str(_seq),
                        "tool": _tool_name,
                        "args": event.get("args", {}),
                        "iteration": _it,
                        # C-review NIT：trace 回填按 tc_id 精确匹配（原 (tool, iteration)
                        # 反向匹配在同轮同工具多次调用时张冠李戴）
                        "tc_id": _seq,
                    })
                elif event.get("type") == "tool_call_result":
                    # H10 修复：将工具结果回写到 tool_calls 表
                    # （Q-B4③：回写入队后台单写者；start→result 的 FIFO 配对键
                    # 由 db 行 id 改为内存序号，db id 由写者从 tc_db_ids 取）
                    if pending_tool_call_ids:
                        _seq = pending_tool_call_ids.pop(0)
                        _result_text: Optional[str] = None
                        try:
                            result_str = event.get("result", "")
                            if not isinstance(result_str, str):
                                result_str = json.dumps(result_str, ensure_ascii=False)
                            # m1 修复：截断时标记，避免 Agent 拿到不完整数据做出错误决策
                            truncated = len(result_str) > 5000
                            _result_text = result_str[:5000] + (
                                "\n...[truncated]" if truncated else "")
                        except Exception as e:
                            logger.warning("tool_call 结果序列化失败: %s", e)
                        if _result_text is not None:
                            _ok = event.get("success", True)
                            _dur = event.get("duration_ms", 0)

                            async def _upd_tc(_s=_seq, _rt=_result_text,
                                              _ok=_ok, _du=_dur):
                                _db_id = tc_db_ids.pop(_s, None)
                                if _db_id is None:
                                    # start 落库失败时无从回写（快照轨迹不受影响）
                                    logger.warning("tool_call seq=%s 回写跳过：start 未落库", _s)
                                    return
                                await asyncio.to_thread(
                                    db.update_tool_call,
                                    tool_call_id=_db_id,
                                    result=_rt,
                                    success=_ok,
                                    duration_ms=_du,
                                )

                            await tc_write_q.put(_upd_tc)
                        # 回写 run_trace_tool_calls（用于 agent_trace_snapshot 持久化）
                        # C-review NIT：按 tc_id 精确匹配 — 原 (tool, iteration) 反向
                        # 匹配在同轮同工具多次调用时张冠李戴
                        # 快照里的 result 必须是字符串：ops_* / job_* 等工具返回 dict，
                        # 原样落库后前端按 string 渲染成 "[object Object]"
                        trace_result = event.get("result", "")
                        if not isinstance(trace_result, str):
                            trace_result = str(trace_result)
                        for tc_entry in reversed(run_trace_tool_calls):
                            if tc_entry.get("tc_id") == _seq and "result" not in tc_entry:
                                tc_entry["result"] = trace_result
                                tc_entry["success"] = event.get("success", True)
                                break
                elif event.get("type") == "done":
                    # Q-B4③：先等后台写者清空 tool_call 落库队列，再写 run 终态与
                    # 轨迹快照 —— 原实现逐条 await 天然有序，这里显式补齐该序。
                    # 30s 兜底：写者按设计不会死（逐任务吞异常），超时放行快照。
                    try:
                        await asyncio.wait_for(tc_write_q.join(), timeout=30)
                    except Exception as e:
                        logger.warning("tool_call 落库队列清空超时/失败: %s", e)
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
                            context_summary=event.get("context_summary") or None,
                            # 功能 B 消费侧闭环：done 透出的 undo 快照落库标记，
                            # 运行列表「回滚」列的数据源（投影守卫见 test_done_event_projection）
                            undo_recorded=1 if event.get("undo_recorded") else 0,
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
                            assistant_persisted = True
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
                        else:
                            # 无正文的运行（纯工具型任务、或失败早退）也要落一条 assistant
                            # 消息承载轨迹快照：否则上方精心构造的 snapshot 随分支一起丢掉，
                            # 重载历史后用户完全看不到 Agent 做过什么。文案沿用前端
                            # finalizeAgentMessage 对空输出的约定。
                            await asyncio.to_thread(
                                db.add_message,
                                conversation_id=req.conversation_id,
                                role="assistant",
                                content="(本轮无文本输出)"
                                if final_state == "completed"
                                else "(AI 运行失败，未产生输出)",
                                reasoning=total_reasoning if total_reasoning else None,
                                model_used=model_cfg.get("model_id") or model_cfg["id"],
                                tokens_used=cost_data.get("total_tokens", 0),
                                run_id=run_id,
                                agent_trace=agent_trace_snapshot,
                            )
                            assistant_persisted = True
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
                            summary = memory_reconcile.format_episodic(
                                task=req.task,
                                tool_calls=run_trace_tool_calls,
                                final_content=total_content,
                                iterations=event.get("iterations", 0),
                                folded_summary=event.get("context_summary") or "",
                            )
                            await asyncio.to_thread(
                                mstore.enqueue_job,
                                memory_reconcile.JOB_EPISODIC,
                                {
                                    "content": summary,
                                    "conv_id": req.conversation_id,
                                    "run_id": run_id,
                                    "importance": 0.6,
                                    "scope": "global",
                                },
                            )
                            logger.info("已投递情景记忆 job (run_id=%s)", run_id)
                        except Exception as e:
                            logger.warning("投递情景记忆 job 失败: %s", e)

                    # ── 事后反思 + 策略演化（异步执行，不阻塞 SSE；超时保护防卡死） ──
                    # H12: 传递 cost_usd / total_tokens / state_history，
                    # 供 MetaReflection 做成本维度反思 + 状态转移异常检测，
                    # 并持久化供 EvolutionEngine 多维演化读取
                    reflect_task = asyncio.create_task(
                        asyncio.wait_for(
                            _run_reflection_and_evolution(
                                run_id=run_id,
                                task=req.task,
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

                elif (event.get("type") == "state"
                        and event.get("state") == "waiting_confirmation"):
                    # 挂起事件（driver 在澄清待确认点结束生成器时发出）。
                    # 刻意不走 done 分支：done 会清卡片、固化 assistant 终稿、
                    # 触发反思 —— 全是「run 已结束」的语义，与挂起相反。
                    run_suspended = True
                    final_state = "waiting_confirmation"
                    suspended_pending = dict(event.get("pending") or {})
                    try:
                        await asyncio.to_thread(
                            db.update_agent_run,
                            run_id=run_id,
                            status="waiting_confirmation",
                            iteration=int(event.get("iteration", 0) or 0),
                        )
                    except Exception as e:
                        logger.warning(
                            "waiting_confirmation 状态落库失败 run_id=%s: %s",
                            run_id, e)

                elif event.get("type") == "error":
                    # driver 出错时先 yield error、紧接着 _emit_done（driver.py:236-250），
                    # done 分支带完整的 iteration/cost 统计并已落终态。final_state 非 None
                    # 时不再重复写状态：否则 done 之后才到达的异常会把 completed 翻成 failed
                    error_seen = True
                    if final_state is None:
                        try:
                            await asyncio.to_thread(
                                db.update_agent_run,
                                run_id=run_id,
                                status="failed",
                            )
                        except Exception as e:
                            # 状态更新失败不应连带吞掉后续清理，但必须留痕
                            logger.warning("error 分支更新 run 状态失败 run_id=%s: %s", run_id, e)

                yield sse_event(event)

                # 挂起收流：发终帧 done(state=waiting_confirmation) 后结束。
                # 前端契约：pending/state=waiting_confirmation 不清卡片、不固化，
                # 用 GET /runs/{id}/pending 重建卡片、用 resume 开新流。
                if run_suspended:
                    yield sse_event({
                        "type": "done",
                        "state": "waiting_confirmation",
                        "run_id": run_id,
                        "pending": suspended_pending,
                    })
                    break

            # H13 修复 C1：失败运行不应错过反思。engine_producer 异常等「只有 error、
            # 没有 done」的 run 在这里补一次调度；done 分支已按完整统计调度过，
            # final_state 作为闸门保证每个 run 只写一条 agent_strategies。
            # （原实现在 error 分支内直接调度，driver 出错时 error+done 各来一次，
            # 同一 run 落两条策略、其中一条 iterations=0，污染 Evolution 的 recent-10 窗口）
            if error_seen and final_state is None:
                try:
                    reflect_task = asyncio.create_task(
                        asyncio.wait_for(
                            _run_reflection_and_evolution(
                                run_id=run_id,
                                task=req.task,
                                final_state="failed",
                                iterations=0,  # error 时迭代数未知
                                tool_calls=run_trace_tool_calls,
                                final_content=total_content,
                            ),
                            timeout=120.0,
                        )
                    )
                    reflect_task.add_done_callback(_task_exception_logger("reflection_error"))
                except Exception as e:
                    logger.warning("error 分支反思调度失败 run_id=%s: %s", run_id, e)

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Agent 运行异常 (event_stream)")
            # 审查R2修复：与 error 分支同款 final_state 守卫。done 分支已落 completed
            # 终态后，[DONE] 送出时客户端断连抛出的普通异常曾无条件把 run 翻成 failed
            # 并清零统计；终态已定稿时不再改写。
            if final_state is None:
                try:
                    await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="failed")
                except Exception:
                    pass
            yield sse_event({"type": "error", "message": str(e)})
            yield "data: [DONE]\n\n"
        finally:
            # ── 抗取消清理 v2：清理体移出被取消的 task ──────────────────
            # 故障根因：客户端断连 → Starlette/anyio cancel scope 取消宿主
            # task。anyio 的取消是"持续投递"：scope 退出前，finally 里每个
            # await 检查点都会再次抛 CancelledError（"Cancelled by cancel
            # scope xxx"）。asyncio.shield 只保护内层协程、外层 await 照样
            # 立刻失败 —— v1 里所有 shield 清理实际都退化为 fire-and-forget，
            # 且每步都误报"清理失败"（清理其实还在后台跑，但异常无人观察、
            # 顺序无保证）。
            # 对策（两段式）：
            #   1) 同步段：无 await 点的操作直接在 finally 执行 —— 取消只能
            #      在 await 检查点投递，同步段免疫，绝不丢失；
            #   2) 异步段：全部 await 清理收进独立 task _final_cleanup()，
            #      它不属于任何 anyio scope，不被反复取消，可真正按序跑完。
            #      finally 里仅一次 shield await 等它：正常路径等清理落库完成
            #      再返回；断连路径外层 await 立即被再取消（静默接住，不必
            #      re-raise —— 原始 CancelledError 仍会从 finally 正常传播），
            #      内层 task 继续在后台跑完，异常由 done_callback 留痕。
            # ── 同步段（零 await，取消免疫）────────────────────────────
            # 占位最先释放（取消也丢不掉）：否则 run 在 DB 里已 completed，
            # 内存占位残留会让新请求永远报"已有运行中的 Agent 任务"。
            _release_run_slot_sync(req.conversation_id, run_id)
            # O1：全局并发槽与会话槽同生命周期，二者必须在同一取消免疫段释放，
            # 否则槽位泄漏会让后续所有 run 永远吃 429。
            _release_global_run_slot_sync()
            # 挂起的 run 不清环境登记与交互态（澄清内存态被 release 会把
            # 恢复所需的题目删掉；环境登记在恢复时由快照回填，不依赖墓碑）。
            if not run_suspended:
                # 环境判定登记与 once 授权池同生命周期：run 结束后不再被授权端点读到，
                # 不清理就会随每次 run 无界增长（与环境无关的旧 run 永远查不到）
                _forget_run_environment(run_id)
            try:
                heartbeat_task.cancel()
                engine_task.cancel()
            except BaseException:
                pass
            # Q-B4③：投 sentinel（排在剩余任务之后，顺序保住）；写者收尾的
            # 等待移入 _final_cleanup（10s 兜底），本 task 内不再 await
            try:
                tc_write_q.put_nowait(None)
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
            # 清理该 run 名下待前端回传的工具结果（visible_terminal 命令交付）。
            # driver 的 remove 在 yield 处被 GeneratorExit 打断时会漏掉，
            # 残留条目随进程存活 —— 由 clear_run 兜底（模块文档约定的收尾点）
            try:
                PendingDeliveries.clear_run(run_id)
            except BaseException:
                pass
            # run 级授权/拒绝/澄清池同样在这里兜底回收：driver 被 cancel 时
            # ReflectionRein 不触发，而客户端断连也不会走 release-run 接口。
            # 挂起路径绝不能进：release_clarification_run 会把恢复所需的
            # 待确认题内存态删掉（DB 快照仍在，但同进程恢复会落空）。
            if not run_suspended:
                try:
                    _release_run_interaction_state(run_id)
                except BaseException:
                    logger.warning("run 交互状态回收失败 (run_id=%s)", run_id, exc_info=True)

            # ── 异步段（独立 task，真正按序跑完）───────────────────────
            async def _final_cleanup():
                # Q-B4③：收尾后台写者 —— 尽量让断连路径的轨迹也落完库；
                # 10s 兜底后放弃（与旧行为"写一半"同向）。
                try:
                    await asyncio.wait_for(tc_writer_task, timeout=10)
                except BaseException:
                    try:
                        tc_writer_task.cancel()
                    except BaseException:
                        pass
                # P2 修复：run 取消/断连时清理该 run 名下的远程后台任务
                # （job_run target=remote 的进程脱离会话运行，不杀会永久残留）
                # 挂起路径跳过：挂起不是取消，用户之前起的远端作业必须保留
                if not run_suspended:
                    try:
                        from agent.tools.builtin.ops_job import kill_remote_jobs_for_run
                        await kill_remote_jobs_for_run(run_id)
                    except BaseException:
                        logger.warning("取消联动清理远程任务失败 (run_id=%s)", run_id, exc_info=True)
                # S1-01：done 分支没跑到时补落 assistant 痕迹（闸门 assistant_persisted）。
                # 收尾点选 finally 而非循环尾：渲染层 abort() 会让消费循环卡在下一次 yield
                # 上并收到 GeneratorExit，那条路既到不了循环尾、也不被 except Exception 接住，
                # 只有这里是正常 break / event_stream 异常 / GeneratorExit 三条路的共同收口。
                # 挂起路径跳过：不写「AI 运行中断」，挂起有专属恢复语义。
                if not assistant_persisted and not run_suspended:
                    try:
                        await persist_interrupted_run(
                            conversation_id=req.conversation_id,
                            run_id=run_id,
                            total_content=total_content,
                            total_reasoning=total_reasoning,
                            tool_calls=run_trace_tool_calls,
                            iterations=last_iteration,
                            model_used=model_cfg.get("model_id") or model_cfg["id"],
                        )
                    except BaseException:
                        logger.warning(
                            "断连 run 的 assistant 痕迹落库失败 (run_id=%s)", run_id, exc_info=True
                        )
                # 客户端断开时将 agent_run 标记为 failed（若仍为 running/init）。
                # 挂起路径跳过：run 已是 waiting_confirmation（非终态），标 failed
                # 会把「等待用户」变成「崩溃」，启动对账也会据此误判。
                if not run_suspended:
                    try:
                        run = await asyncio.to_thread(db.get_agent_run, run_id)
                        if run and run.get("status") in ("running", "init"):
                            await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="failed")
                    except BaseException:
                        pass
                # 修复：Agent 启动时将会话状态设为 in_progress，结束时须重置回
                # completed — 若不复位，会话永久滞留 in_progress。
                # （占位释放已提到最前的同步操作；此步为对称的 DB 侧复位）
                # 挂起路径跳过（最危险的一处）：复位成 completed 后，开启会话
                # 保留清理时 prune_conversations 会把整个会话连同待确认点级联删掉。
                if not run_suspended:
                    try:
                        cur_status = await asyncio.to_thread(db.get_conversation, req.conversation_id)
                        if cur_status and cur_status.get("status") == "in_progress":
                            await asyncio.to_thread(
                                db.update_conversation_status, req.conversation_id, "completed"
                            )
                    except BaseException:
                        pass
                # 低频清理 agent_runs/tool_calls（每 6h 节流，best-effort）
                try:
                    await _maybe_prune_agent_runs()
                except BaseException:
                    pass

            _cleanup_task = asyncio.create_task(_final_cleanup())
            _cleanup_task.add_done_callback(_task_exception_logger("agent_final_cleanup"))
            try:
                # 正常路径：等清理真正跑完再返回（保持 v1 的落库时序）。
                # 断连路径：此 await 立即被 cancel scope 再取消 —— 静默接住，
                # _cleanup_task 继续在后台完成（shield 恰好保证内层不被取消；
                # 与 v1 的区别：这里只有这一个 await 检查点，且不再误报失败）。
                await asyncio.shield(_cleanup_task)
            except asyncio.CancelledError:
                pass
            except BaseException:
                logger.warning("最终清理任务异常 (run_id=%s)", run_id, exc_info=True)

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


# ── 工具面（结果回传 + 工具列表/启停）→ routes/agent_tool_result.py（P2 拆分，端点路径与契约不变）──
# re-export 保持 `routes.agent.<符号>` 的既有调用方（含后端测试）不变。
from routes.agent_tool_result import (  # noqa: F401  (re-export)
    ToolResultRequest, UpdateToolEnabledRequest, agent_tool_result,
    list_tools, update_tool_enabled,
)


# ── Agent 权限管理 → routes/agent_permission.py（P2 拆分，端点路径与契约不变）──
# re-export 保持 `routes.agent.<符号>` 的既有调用方（含后端测试）不变。
from routes.agent_permission import (  # noqa: F401  (re-export)
    AllowPermissionRequest, DenyCommandRequest, PermissionGrantRequest,
    SafetyClassifyRequest, _release_run_interaction_state,
    allow_tool_permission, classify_safety_commands, deny_command,
    grant_tool_permission, list_tool_permissions, release_run_permissions,
    revoke_tool_permission,
)


# ── 任务结构化 → routes/agent_structure.py（P2 拆分，端点路径与契约不变）──
# re-export 保持 `routes.agent.<符号>` 的既有调用方（含后端测试）不变。
from routes.agent_structure import (  # noqa: F401  (re-export)
    StructureRequest, StructureTarget, _extract_json_object, structure_task,
)


# ── 提交信息生成 → routes/agent_commit.py（P2 拆分，端点路径与契约不变）──
# re-export 保持 `routes.agent.<符号>` 的既有调用方（含后端测试）不变。
from routes.agent_commit import (  # noqa: F401  (re-export)
    CommitMessageRequest, _sanitize_commit_message, generate_commit_message,
)


# ── 拒绝命令 + release-run → routes/agent_permission.py（P2 拆分，端点路径与契约不变）──


# ── 澄清向导（Clarification Wizard）→ routes/agent_clarification.py（P2 拆分，端点路径与契约不变）──
# re-export 保持 `routes.agent.<符号>` 的既有调用方（含后端测试）不变。
from routes.agent_clarification import (  # noqa: F401  (re-export)
    ClarificationAnswerRequest, skip_clarification, submit_clarification_answer,
)


# ── 挂起任务（取消 / 待确认点 / 恢复）→ routes/agent_runs.py（P2 拆分，端点路径与契约不变）──
# re-export 保持 `routes.agent.<符号>` 的既有调用方（含后端测试）不变。
from routes.agent_runs import (  # noqa: F401  (re-export)
    ResumeRunRequest, cancel_suspended_run, get_run_pending,
    resume_suspended_run,
)


# ── Agent 记忆管理 → routes/agent_memory.py（P2 拆分，端点路径与契约不变）──


# ── 治理（tick + 策略）→ routes/agent_governance.py（P2 拆分，端点路径与契约不变）──


# ── Trace（运行列表 + 轨迹）→ routes/agent_trace.py（P2 拆分，端点路径与契约不变）──
# re-export 保持 `routes.agent.<符号>` 的既有调用方（含后端测试）不变。
from routes.agent_trace import (  # noqa: F401  (re-export)
    get_agent_run_trace, list_agent_runs,
)


