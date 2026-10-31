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
import json
import logging
import time
from typing import TYPE_CHECKING, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

import database as db
from routes.terminal import _get_model_config
from sse_utils import sse_event
from agent.audit import append_record_async
from agent import audit
from agent.environment_policy import EnvironmentResolution, resolve_run_environment
from agent.memory import mstore
from agent.memory import budget as memory_budget
from agent.memory import reconcile as memory_reconcile
from agent.memory import retriever as memory_retriever
from agent.memory.embedding import get_embedding_client
from agent.consciousness_tick import get_global_tick
from agent.pending_delivery import PendingDeliveries
from prompts import _current_time_str, SSH_MODE, SSH_MODE_PROMPT
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

# 对话姿态提示：由前端模式选择器选中项决定，注入到 system prompt 前部。
# goal = 主流「目标姿态」，聚焦"这次要达成什么、怎么算成功"；
# plan = 「计划/方案姿态」，强调先给方案与影响面、批准后再动手。
_WORKFLOW_MODE_PROMPTS = {
    "goal": (
        "本会话处于「目标」姿态：请始终把用户这一次要达成的目标放在第一位，"
        "主动澄清并锁定『达成什么、怎么算成功』，执行围绕该目标收敛，避免被枝叶问题带偏。"
    ),
    "plan": (
        "本会话处于「计划」姿态：请先给出解决方案与影响面评估，"
        "在用户明确批准前不要直接动手更改环境；如需读取/调研以支撑方案，可正常进行。"
    ),
}

def _paired_permission_mode(workflow_mode: Optional[str], permission_mode: str) -> str:
    """plan 姿态 ⇒ 只读强制的配对强制（P1 修复，审批审查 2026-10-11）。

    前端 resolveRunModes 恒成对下发，但 HTTP 边界不能信任客户端：system prompt
    按 workflow_mode 注入「写操作会被直接拒绝」，PlanModeGuard 按 permission_mode
    激活真正的拦截 —— 客户端只发前者就是「prompt 声称只读、强制不存在」的空开关。
    故 workflow_mode='plan' 时强制 permission_mode='plan' 并留痕。
    反向（permission_mode='plan' 而姿态非 plan）是合法的纯只读请求，不改写。
    """
    if workflow_mode == "plan" and permission_mode != "plan":
        logger.warning(
            "workflow_mode=plan 与 permission_mode=%r 不配对，强制为 plan", permission_mode)
        return "plan"
    return permission_mode


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
) -> str:
    """构建 Agent 系统提示

    拼装顺序：时间 → 模式角色 → **当前目标** → 画像块 → 用户 prompt
              → 运维方法论 → 工具/输出引导

    Args:
        profile_text: 画像块渲染结果（agent.memory.blocks.render_for_context），
            含用户偏好、操作规则与目标机环境事实，已按 memory.blocks_budget_tokens
            裁剪；空串不注入
        is_change: 任务是否可能改变目标机状态。None 表示未做意图识别，
            此时按"变更"处理 —— 方向性取舍见下方注释。
        goal_text: 会话当前工作流阶段注入段（含当前阶段与相应工件），空串不注入。
            紧跟角色设定插入：目标是「任务目的」，与角色同属身份层，放最靠前
            以获得最高注意力；环境事实、用户 prompt 等方法层信息排其后。
            对话型任务同样注入 —— 用户一旦声明目标即为全程约束，纪律第 4 条
            处理的正是「新要求与目标冲突」，纯问答场景同样适用。
    """
    from prompts import OPS_METHOD_CHANGE, OPS_METHOD_READONLY

    # 开头注入当前时间（解决 LLM 训练数据截止导致日期/星期回答过时的问题）
    time_injection = (
        f"[系统时间] 当前时间：{_current_time_str()}。"
        f"如用户询问日期/星期/时间，请以本信息为准，不要使用训练数据中的旧时间。"
    )

    # 拼装：时间 + 模式基础 prompt + 用户 system_prompt + 画像块
    parts = [time_injection, SSH_MODE_PROMPT]
    # 对话姿态：由前端模式选择器决定（goal=聚焦目标 / plan=先方案后动手）。
    # 仅对非纯对话型任务注入（纯问答不需要方法论姿态）。
    # workflow_mode 为 None = 用户未表态（前端 × 清掉后），此时不注入任何姿态段：
    # 若退化成 goal，× 就只是个能让 chip 消失、却什么都不关的摆设。
    if not is_conversation_task and req.workflow_mode is not None:
        parts.append(_WORKFLOW_MODE_PROMPTS[req.workflow_mode])
    # 会话目标：每轮 run 都复现，是「目标不漂移」的主手段
    if goal_text:
        parts.append(goal_text)
    # 画像块：常驻的已知事实（用户偏好 / 操作规则 / 目标机环境），让"这是台什么机器、
    # 用户要什么"先于任何命令生成进入上下文，避免模型凭训练数据印象对 Ubuntu 发 yum。
    # 无 SSH 会话时主机块文案会显式声明"运维工具不在本机执行"。
    # 对话型任务不注入（不调工具，环境与操作规则无意义）——即便调用方传了也在此拦掉。
    if profile_text and not is_conversation_task:
        parts.append(profile_text)
    if req.system_prompt:
        parts.append(req.system_prompt)

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
            "清理日志用 log_cleanup（默认 dry_run 只列出匹配文件，把清单给用户确认后"
            "再用 dry_run=false 删除），不要用 find -delete / rm -rf 手工拼删除命令；"
            "生产环境删除需预先在 allowlist 登记 log_cleanup，但试运行预览无需登记。"
            "若目标目录不在 log_cleanup 允许范围（/tmp、/var/tmp、/var/log 应用子目录、"
            "agent 托管日志目录）内，直接告知用户该目录需人工清理并给出命令，"
            "不要构造删除命令的变体。"
        )
        # 记忆通道引导：预检索注入是"被动想起来"，工具是"主动查/记/纠错"
        parts.append(
            "记忆使用：上下文里 [画像] 是常驻已知事实、[相关记忆] 是本轮检索到的历史经验，"
            "可直接引用无需再调工具。需要更多历史经验、或判断某做法在其他主机上的先例时用 "
            "memory_search；确认了值得长期保留的主机特性/操作教训时用 memory_add（进后台对账队列，"
            "本轮不生效）；用户给出持久偏好、或要修正 [画像] 里的既有内容时用 block_update；"
            "发现 [相关记忆] 里的事实已被证伪时用 memory_invalidate 作废它，不要留着互相矛盾的记忆。"
        )
        # 探测命令构建规范：多段探测用 ; 串联而非 &&；exit 非零属预期，不是连接异常
        parts.append(
            "探测命令规则：一次执行多段探测/排查命令时用 ; 串联各段，不要用 &&——"
            "du/find/ls 遇到无权限路径会以非零码退出（属预期），&& 链会因此中断后续段落。"
            "命令 exit code 非零不等于连接或执行异常：先看已捕获的输出内容再下结论"
            "（du 仍会输出可读部分的大小）。"
            "注意：末段 `echo ...:$?` 捕获的是整行最后一条命令的退出码，多段探测时"
            "末段若因权限报错会把整行标成失败。对以探测/统计为主的命令链，建议在结尾追加"
            "`; true` 使整体退出码收敛为 0，避免把可读的探测结果误报为失败；对真正需要"
            "判断成败的关键命令则不要追加。"
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


def _load_active_goal_text(conversation_id: str) -> str:
    """读取会话当前工作流阶段（Spec/Plan/Goal）并渲染为注入文本，无内容一律返回空串。

    调用方在 async 上下文里经 asyncio.to_thread 执行。注入是增强项，
    任何失败都必须降级为空串，绝不让它把整轮对话拖垮（500 / run 起不来）。
    """
    if not conversation_id:
        return ""
    try:
        from agent.workflow import hydrate_for_session
        return hydrate_for_session(conversation_id)
    except Exception as e:
        logger.warning("读取会话工作流阶段失败（跳过注入）: %s", e)
        return ""


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
    # 对话姿态（goal/plan 单选，来自前端模式选择器）。驱动 _build_agent_system_prompt
    # 注入对应的姿态前缀。
    # 可空是「目标」chip 的 × 能真正关掉的前提：前端 activeMode 允许 null 后，若这里
    # 仍默认 "goal"，× 只是把同一个 'goal' 又发一遍 —— 用户看到 chip 消失而行为毫无
    # 变化，那是「能关、但关的是个摆设」。缺省改为 None = 不注入任何姿态段；
    # 老客户端不传该字段时行为随之变化（原先默认注入 goal 前缀），这是有意的：
    # 没表态 ≠ 选了目标姿态。
    workflow_mode: Optional[Literal["goal", "plan"]] = None


# ── Routes ──

# C-review MINOR-12：同会话并发 run 防护。
# 原实现同一 conversation 可并发多个 run：消息交错写入、授权互相消费、
# 备份/文件互覆。这里用进程级 {conversation_id: run_id} 标记进行中的 run，
# 请求时原子地 check-and-set，事件流 finally 中释放。
_in_flight_runs: dict[str, str] = {}
_in_flight_lock = asyncio.Lock()


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


async def _release_run_slot(conversation_id: str, run_id: str) -> None:
    """释放占位（兼容旧调用；实际为无 await 的同步操作）"""
    _release_run_slot_sync(conversation_id, run_id)


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
    # ── 按 SSH 模式选择默认模型（兜底）──
    # 审查 M-13：db 同步调用放线程池，避免阻塞事件循环（卡住所有并发请求/心跳）
    effective_model_id = req.model_id
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
        # ── 按模式过滤显式 skill_ids ──
        if not req.skill_ids:
            return
        disabled_skill_ids = set(await asyncio.to_thread(db.list_disabled_skills_for_mode, SSH_MODE))
        original_skill_ids = req.skill_ids
        req.skill_ids = [sid for sid in req.skill_ids if sid not in disabled_skill_ids]
        if len(req.skill_ids) != len(original_skill_ids):
            logger.info(
                "Agent 显式 skill_ids 按 mode=%s 过滤: %d -> %d",
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
        # 会话目标：用户声明的结构化目标随每轮 run 复现（防漂移的主手段）。查询走
        # to_thread（与其它 db 访问一致），失败/无目标降级为空串，不影响 run。
        return await asyncio.to_thread(_load_active_goal_text, req.conversation_id or "")

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
    goal_text = _setup_results[4]
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
            scopes=memory_retriever.context_scopes(req.ssh_session_id or ""),
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

    # P1 修复（审批审查 2026-10-11）：plan 姿态配对强制（理由见
    # _paired_permission_mode docstring）；此局部变量是 harness.run 的唯一来源。
    permission_mode = _paired_permission_mode(req.workflow_mode, req.permission_mode)
    # 结构化增强含新增写步骤时，auto 回落 normal（fail-closed，服务端不信任前端）。
    permission_mode = _structure_aware_permission_mode(
        req.enhanced_has_added_write_steps, permission_mode)

    # M3 修复：提取 system prompt 构建为独立函数
    final_system_prompt = _build_agent_system_prompt(
        req,
        profile_text,
        is_conversation_task,
        # pre_intention 为 None 表示本次未做意图识别（enable_intention_guide=False）
        is_change=pre_intention.is_change if pre_intention else None,
        goal_text=goal_text,
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

    # P0-1：环境标签由服务端判定（见 agent.environment_policy）。
    # req.environment 只算一个「上限请求」：主机连接配置标了 prod 就不允许降级成
    # test —— 否则一次请求就能把生产强化的「危险命令仅预登记 allowlist」整条绕开。
    # 拿不到主机信息时不拦 run，但 source 会一路带到审批卡与审计记录（未核实）。
    env_resolution = await resolve_run_environment(
        req.environment, req.ssh_session_id or "")
    _remember_run_environment(run_id, env_resolution)
    try:
        # 持久化 agent_run 记录（审查 M-13：放线程池）
        await asyncio.to_thread(
            db.create_agent_run,
            run_id=run_id,
            conversation_id=req.conversation_id,
            task=req.task,
            max_iterations=req.max_iterations,
            mode=SSH_MODE,
            model_used=model_cfg.get("model_id") or model_cfg["id"],
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
            req.task,
            model_used=model_cfg.get("model_id") or model_cfg["id"],
            agent_trace=_user_msg_trace,
        )
        await asyncio.to_thread(db.touch_conversation, req.conversation_id)
        await asyncio.to_thread(db.update_conversation_status, req.conversation_id, "in_progress")
    except Exception:
        # 占位已得但落库失败 → 释放占位，避免该会话被永久锁死
        await _release_run_slot(req.conversation_id, run_id)
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
                    enable_intention_guide=req.enable_intention_guide,
                    enable_dynamic_budget=req.enable_dynamic_budget,
                    # C-review M10：成本/token 硬熔断
                    max_cost_usd=req.max_cost_usd,
                    max_total_tokens=req.max_total_tokens,
                )
                async for trace_event in harness.run(
                    task=req.task,
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
            # ── 抗取消清理 ─────────────────────────────────────────────
            # 故障根因：客户端断连 → 响应任务被 cancel → finally 里第一个
            # await 抛 CancelledError（BaseException，except Exception 接不住）
            # → 其后的占位释放/会话复位全部跳过 → run 在 DB 里已 completed，
            # 但内存占位残留，新请求永远报"已有运行中的 Agent 任务"。
            # 对策：1) 占位用无 await 的同步操作最先释放（取消也丢不掉）；
            #      2) 其余清理全部 except BaseException；
            #      3) 关键异步清理用 shield（外层被取消时内层任务继续跑完）。
            _release_run_slot_sync(req.conversation_id, run_id)
            # 环境判定登记与 once 授权池同生命周期：run 结束后不再被授权端点读到，
            # 不清理就会随每次 run 无界增长（与环境无关的旧 run 永远查不到）
            _forget_run_environment(run_id)
            try:
                heartbeat_task.cancel()
                engine_task.cancel()
            except BaseException:
                pass
            # Q-B4③：收尾后台写者 —— 先投 sentinel（排在剩余任务之后，顺序保住），
            # 尽量让断连路径的轨迹也落完库；10s 兜底后放弃（与旧行为"写一半"同向）。
            try:
                tc_write_q.put_nowait(None)
                await asyncio.shield(asyncio.wait_for(tc_writer_task, timeout=10))
            except BaseException:
                try:
                    tc_writer_task.cancel()
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
            # ReflectionRein 不触发，而客户端断连也不会走 release-run 接口
            try:
                _release_run_interaction_state(run_id)
            except BaseException:
                logger.warning("run 交互状态回收失败 (run_id=%s)", run_id, exc_info=True)
            # P2 修复：run 取消/断连时清理该 run 名下的远程后台任务
            # （job_run target=remote 的进程脱离会话运行，不杀会永久残留；
            #  shield 保证断连 cancel 时清理任务仍会在后台跑完）
            try:
                from agent.tools.builtin.ops_job import kill_remote_jobs_for_run
                await asyncio.shield(kill_remote_jobs_for_run(run_id))
            except BaseException:
                logger.warning("取消联动清理远程任务失败 (run_id=%s)", run_id, exc_info=True)
            # S1-01：done 分支没跑到时补落 assistant 痕迹（闸门 assistant_persisted）。
            # 收尾点选 finally 而非循环尾：渲染层 abort() 会让消费循环卡在下一次 yield
            # 上并收到 GeneratorExit，那条路既到不了循环尾、也不被 except Exception 接住，
            # 只有这里是正常 break / event_stream 异常 / GeneratorExit 三条路的共同收口。
            # 每一步单独包 BaseException + shield，遵循本块开头成文的抗取消姿势。
            if not assistant_persisted:
                try:
                    await asyncio.shield(
                        persist_interrupted_run(
                            conversation_id=req.conversation_id,
                            run_id=run_id,
                            total_content=total_content,
                            total_reasoning=total_reasoning,
                            tool_calls=run_trace_tool_calls,
                            iterations=last_iteration,
                            model_used=model_cfg.get("model_id") or model_cfg["id"],
                        )
                    )
                except BaseException:
                    logger.warning(
                        "断连 run 的 assistant 痕迹落库失败 (run_id=%s)", run_id, exc_info=True
                    )
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
    # 用户点「拒绝」而非执行：必须区分于「执行失败」，否则模型看到
    # "[已写入可见终端] <cmd>"，以为破坏性命令已经在真实终端里跑过
    rejected: bool = False
    # S1-03：发起回传的 run。不传 = 不声明，服务端照旧按 tool_id 认领；
    # 传了就必须与 create() 时登记的 run 一致，否则一次跨 run 的冒名回传
    # 能把另一个挂起循环直接闭合成成功。
    run_id: Optional[str] = None
    # requires_confirm 的交付：调用方必须显式声明「用户确认过」。
    # 这不是密码学证明（本机持 token 的调用方想声明什么都能声明），但它把
    # 「用户从未点确认」从**服务端无从分辨**变成**一个需要主动主张、且会留在
    # 交付项上供审计读取的事实**（confirmed_by_user）。
    confirmed: bool = False


@router.post("/agent/tool-result")
async def agent_tool_result(req: ToolResultRequest):
    """回传可见终端命令执行结果，恢复挂起的 Agent 循环。

    - 200：成功恢复对应的待交付命令
    - 404：tool_id 未知（可能已超时清除/会话结束）
    - 403：run 归属对不上，或 requires_confirm 的交付未经确认（S1-03）
    """
    delivery = PendingDeliveries.get(req.tool_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail=f"tool_id 未知: {req.tool_id}")
    if req.run_id is not None and req.run_id != delivery.run_id:
        # 措辞与 tool_id 未知分开：这一条是「认领对了但越权」，排查时含义完全不同
        logger.warning(
            "拒绝跨 run 的交付回传 (tool_id=%s 属 run=%s，回传方自称 run=%s)",
            req.tool_id, delivery.run_id, req.run_id,
        )
        raise HTTPException(status_code=403, detail="该待交付命令不属于此 run")
    if not req.rejected and delivery.requires_confirm and not req.confirmed:
        logger.warning(
            "拒绝未经用户确认的交付回传 (tool_id=%s, run=%s)", req.tool_id, delivery.run_id
        )
        raise HTTPException(status_code=403, detail="该命令需要用户确认后才能回传执行结果")

    if req.rejected:
        ok = PendingDeliveries.reject(req.tool_id)
    else:
        ok = PendingDeliveries.resume(
            req.tool_id,
            output=req.output,
            success=req.success,
            exit_code=req.exit_code,
            confirmed=req.confirmed,
        )
    if not ok:
        raise HTTPException(status_code=404, detail=f"tool_id 未知: {req.tool_id}")
    return {"status": "ok", "tool_id": req.tool_id}


@router.get("/agent/tools")
def list_tools():
    """查询可用工具列表（含启用状态）"""
    # 获取所有工具类的元信息（包括未注册的，用于显示被禁用的工具）
    from agent.tools.builtin import get_all_tool_classes
    all_tools_info = get_all_tool_classes()

    tools = []
    disabled_list = db.list_disabled_tools(SSH_MODE)
    for tool_cls in all_tools_info:
        # 实例化以读取 name/description/parameters（这些可能是实例属性或类属性）
        instance = tool_cls()
        tools.append({
            "name": instance.name,
            "description": instance.description,
            "parameters": instance.parameters,
            "risk_level": instance.meta.risk_level,
            "enabled": instance.name not in disabled_list,
            # 透传工具元数据，供前端展示
            "timeout": instance.meta.timeout,
            "cacheable": instance.meta.cacheable,
            "reversible": instance.meta.reversible,
        })
    return {"mode": SSH_MODE, "tools": tools}


class UpdateToolEnabledRequest(BaseModel):
    enabled: bool


@router.patch("/agent/tools/{tool_name}/enabled")
def update_tool_enabled(tool_name: str, req: UpdateToolEnabledRequest, mode: str = "all"):
    """更新工具启用状态（默认 mode=all 全局开关）"""
    result = db.upsert_tool_config(tool_name, mode, req.enabled)
    return result


# ── Agent 权限管理 ──


class PermissionGrantRequest(BaseModel):
    # 与 AllowPermissionRequest 同口径限长：这个键会原样落进 allowlist.txt，
    # 不限长度就等于给授权清单文件开了一个无界写入入口（字符集校验管不住长度）
    tool: str = Field(..., min_length=1, max_length=64, description="工具名")
    # 审查 M-3：默认 scope 从 "always" 收紧为 "once"（仅放行下一次调用）。
    # 原默认 always 会在用户点一次后永久免审批。
    # Q-B4：裸 str 改枚举 —— 非法 scope 在请求模型层就被 422 拒掉，
    # 不再需要路由函数里的手工 if 白名单。
    scope: Literal["once", "session", "always"] = Field(
        "once", description="授权范围：once（仅本次）/ session（会话）/ always（永久）")
    # 命令执行工具（shell_exec）时传入被批准的具体命令，
    # 授权绑定到命令哈希（同命令免审批，其他 dangerous 命令仍需审批）
    command: Optional[str] = Field(None, description="命令执行工具的具体命令（用于命令级绑定）")
    # C-review M11：session 级授权绑定的会话 ID — 防跨会话授权泄漏
    conversation_id: str = Field("", description="会话 ID（session 授权按会话键控）")
    # P1 竞态修复：once 授权绑定的运行 ID — 运行终止时按 run_id 清理
    run_id: str = Field("", description="运行 ID（once 授权按 run_id 隔离，终态自动清理）")


@router.post("/agent/permission/grant")
async def grant_tool_permission(req: PermissionGrantRequest):
    """授权工具

    审查R2修复（线程竞态）：本组审批端点此前是同步 def 路由，被 FastAPI 丢进
    anyio 线程池执行，与事件循环上的 PermissionGate 消费/清理代码（reins.check、
    event_stream finally 的回收）无锁并发读写同一批可变 dict/set —— sorted()
    迭代中另一线程 add/discard 会抛 RuntimeError，defaultdict 复合 += 丢账。
    改为 async def 后 gate 状态操作全部回到事件循环单线程，天然串行；
    审计落盘（append_record，阻塞文件写）经 asyncio.to_thread 移出事件循环。
    gate 内部的小额本地文件写（allowlist 覆盖写，几行文本）随 grant 留在循环内：
    把它单独挪进线程会把状态突变也带出循环，竞态复活 —— 两害相权取小文件同步写。

    scope=once: 仅放行下一次相同工具（命令工具为相同命令）调用
    scope=session: 进程级单例 _confirmed_session，跨 run 共享，进程重启失效（H13 修复 A5）；
      C-review M11：按 conversation_id 键控，只对发起会话的 run 生效
    scope=always: 写入 ~/.ckunagentic/allowlist.txt，所有后续 run 放行
      （命令工具写入的是 `tool:cmdhash` 键，不覆盖其他命令 — 审查 M-3）
    """
    from agent.harness.contracts import TERMINAL_RUN_STATES
    from agent.harness.reins import _get_global_permission_gate_sync, approval_wait
    from agent.harness.reins.gate import normalize_host, scoped_key
    # hardline 预检（Q-B4①）：底线命令在 FilesystemGuard 层无条件阻断
    # （reins.guards 的 hardline 分支不问 PermissionGate、不看 allowlist），
    # 把它写进 allowlist 的预授权必然落空 —— 与其 200 登记「已授权」的死键
    # （审计里还留下一条 approved），不如 400 把不可放行原因摆到面上。
    # 只拦 hardline：dangerous 是审批机制要处理的正常输入，不在此收窄。
    if req.command and str(req.command).strip():
        from agent.harness.safety import classify_command
        _level, _reason = classify_command(str(req.command))
        if _level == "hardline":
            raise HTTPException(
                status_code=400,
                detail=(f"命令不可放行：{_reason or '命中 hardline 底线'}。"
                        "hardline 命令被无条件阻断，allowlist 无法放行，"
                        "登记预授权不会生效；请改用安全的替代方案。"),
            )
    # once 授权的两道前置守卫（都只在 scope==once 时触发，合并到一个条件块）：
    #   1) 缺 run_id：无法被任何 run 消费，直接 400（原实现落空 run_id 全局池，有跨 run 竞态）
    #   2) run 已终态（P2 修复，审批审查 2026-10-11）：cleanup_run_once 已清空该 run 的池，
    #      此刻落库的键永不被消费也不再被清理（死键泄漏），前端却显示「已授权」。典型触发：
    #      审批兜底到期后 run 结束、SSE 迟到/断连窗口内的迟到点击。行缺失同样 409（fail-closed）：
    #      审批卡只在 run 运行中产生，届时 DB 行必已存在，缺行说明 run_id 本身不可信。
    if req.scope == "once":
        if not req.run_id:
            raise HTTPException(status_code=400, detail="scope=once 授权必须携带 run_id")
        try:
            _row = await asyncio.to_thread(db.get_agent_run, req.run_id)
        except Exception as e:
            # 查询本身失败不拦授权：这条校验防的是「死键泄漏 + 假已授权」的完整性
            # 问题，不是安全闸门（once 键消费时 PermissionGate 本就还要查表）。
            # 拿不到证据不等于 run 已死，告警放行。
            logger.warning("grant 前置查询 run=%s 状态失败，放行授权: %s", req.run_id, e)
        else:
            _run_status = (_row or {}).get("status", "")
            if _row is None or _run_status in TERMINAL_RUN_STATES:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"run {req.run_id} 已结束（status={_run_status or '不存在'}），"
                        "once 授权无处消费；请在新的运行中重新确认该命令"
                    ),
                )

    # H13 修复 A5：session scope 改用进程级单例（原 501 限制移除）
    pg = _get_global_permission_gate_sync()
    # 写前审计：先把「这条授权发生了」落进链，再让授权生效。顺序反过来（先 grant
    # 再审计）时，一次写失败就会留下「授权已生效、没有记录」的缺口 —— 而审计链里
    # 最危险的形态不是空链，是看起来完整、实际漏了一条的链。
    # grant_key_for 只校验 + 算键、不改任何授权状态，保证审计记录不会先于一条
    # 最终被 ValueError 拒绝的授权落地。
    try:
        grant_key = pg.grant_key_for(
            req.tool, req.command, scope=req.scope, run_id=req.run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # C-review C2 修复：批准动作落审计链（原实现只写内存/allowlist，
    # 事后无法证明"谁批准了什么"，等保审计目标落空）。
    # S2-04：审批决策必须带 run_id。已退役的 docs/security-boundary.md §6 声明每条审批
    # 记录可追到它服务的 run，而原实现八处 append_record 全都没传 run_id ——
    # grant 这一处连 req.run_id 都攥在手里（scope=once 还强制它非空，否则 400）。
    # 事后只能证明「有人批过 foo」，证不了「批的是哪次运行」，等保审计目标落空。
    # critical=True：写不进去就不授权（全局 handler 转 503），详见 audit.append_record。
    # 主机限域：always 与 session 都用 —— once 本来就随 run 结束而失效，run 只跑
    # 一台主机，不需要再限定作用域。session 不限定就会漏成「测试机上点本会话允许，
    # 同一对话切到生产机后同一条命令直接免审」（会话跨主机，而主机是具体的）。
    # host 来自服务端反查（与 environment 同源），拿不到时为空 → 落全局条目，
    # 与匹配端「host 为空只查全局」对称，不会出现「授权了但永不命中」。
    grant_host = _run_host(req.run_id) if req.scope in ("always", "session") else ""
    await append_record_async(
        action=f"permission_grant:{req.tool}",
        conclusion="approved",
        run_id=req.run_id,
        target_type="tool",
        target_id=scoped_key(grant_host, grant_key),
        # 环境列原先恒为默认值 "test"：对 prod 主机的授权在审计页的 prod 视图里
        # 直接不存在，等于把生产审批藏起来了。取本次 run 的服务端判定结果。
        environment=_run_environment(req.run_id),
        command_raw=req.command or "",
        result=(f"scope={req.scope},conversation={req.conversation_id or '-'}"
                f",host={grant_host or '全局'}"),
        critical=True,
    )
    # 审计已落地，此刻才让授权生效。
    pg.grant(req.tool, scope=req.scope, command=req.command,
             conversation_id=req.conversation_id, run_id=req.run_id,
             host=grant_host)
    # 唤醒正在等这张卡的 run：Driver 在审批点阻塞（见 reins.approval_wait），
    # 不叫醒它就得一直挂到兜底超时。授权已落库，醒来重判自然放行。
    approval_wait.resolve_for_call(
        req.run_id, req.tool, grant_key, approval_wait.ALLOWED)
    return {"tool": req.tool, "scope": req.scope, "grant_key": grant_key,
            "host": normalize_host(grant_host), "granted": True}


@router.post("/agent/permission/revoke")
async def revoke_tool_permission(tool: str, command: Optional[str] = None,
                                 conversation_id: Optional[str] = None,
                                 host: Optional[str] = None):
    """撤销工具授权（从永久 allowlist 和 session scope 移除）

    审查R2修复：改 async def 回到事件循环单线程操作 PermissionGate 状态，
    与 grant 端点同一姿态（详见 grant 路由 docstring）。

    提供 command 时只撤销该命令的绑定授权；否则撤销该工具全部授权。
    C-review M11：提供 conversation_id 时只撤该会话的 session 授权，
    否则（默认）撤所有会话。
    host：不传 = 全局与所有主机限域条目全撤（「全部撤销」按钮的语义）；
          host="" = 只撤全局条目；host="h" = 只撤该主机的条目。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    pg.revoke(tool, command=command, conversation_id=conversation_id, host=host)
    # C-review C2 修复：撤销动作同样落审计链
    await append_record_async(
        action=f"permission_revoke:{tool}",
        conclusion="revoked",
        # S2-04：这两处（撤销 / 手动登记永久授权）按设计不属于任何 run —— 它们改的是
        # 跨 run 的 allowlist 与会话级状态，run_id 留空是正确取值，不是漏传。
        target_type="tool",
        # 撤销标的带主机前缀（与 allowlist 行同形），事后能分辨「撤掉了某台
        # 机器的预授权」还是「撤掉了全局条目」。host=None = 全撤，不加前缀。
        target_id=(f"@{host}|{command or tool}" if host else (command or tool)),
        command_raw=command or "",
        result=f"host={host if host is not None else '所有作用域'}",
    )
    return {"tool": tool, "granted": False, "host": host}


@router.get("/agent/permission/list")
async def list_tool_permissions():
    """查询已永久授权的工具列表

    审查R2修复：改 async def —— `sorted(pg._confirmed_permanent)` 是跨多行的
    迭代快照，此前在 anyio 线程池执行时与循环内 grant/revoke/check 的并发
    add/discard 相撞可直接抛 RuntimeError（迭代中集合被改）。

    `tools` 保持原来的字符串数组语义（前端与 test_admin_permission_and_webkeys 都按此消费）。
    `entries`/`ttl_hours`（E3）是附加信息：授权何时写入、何时到期，面板要能诚实
    回答"这条预登记还会有效多久"，而不是只报"已授权"。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    return {
        "tools": sorted(pg._confirmed_permanent),
        "entries": pg.permanent_entries(),
        "ttl_hours": max(0, int(pg.allowlist_ttl_hours)),
    }


class AllowPermissionRequest(BaseModel):
    """手动登记永久授权（免去先触发审批弹窗再点"始终允许"的路径）"""
    tool: str = Field(..., min_length=1, max_length=64, description="工具名")
    command: Optional[str] = Field(None, max_length=4096,
                                   description="命令执行工具的绑定命令；省略 = 工具级授权")
    host: Optional[str] = Field(
        None, max_length=255,
        description="限定到该主机（allowlist 写成 @host|键）；省略 = 全局条目")


@router.post("/agent/permission/allow")
async def allow_tool_permission(req: AllowPermissionRequest):
    """手动写入永久 allowlist（与审批"始终允许"同源，落 allowlist 文件 + 审计）

    审查R2修复：改 async def 回到事件循环单线程操作 PermissionGate 状态，
    与 grant 端点同一姿态（详见 grant 路由 docstring）。

    安全边界：仅登记，不放宽任何判定 — 命令工具写入的是 `tool:cmdhash`
    命令级绑定键（审查 M-3 语义）。hardline 底线命令在 FilesystemGuard 层
    无条件阻断，allowlist 无法放行 —— 因此本端点对 hardline 命令直接 400
    拒绝（Q-B4①）：原实现照单登记，返回 200 的「已授权」是一条 FilesystemGuard
    必然拦下的死预授权，面板显示与真实判定自相矛盾。

    主机维度：提供 host 时写成 `@host|键`（只对该主机生效）；省略 = 全局
    （对所有主机生效）。默认值刻意不用「当前连接主机」—— 这个端点没有
    run 上下文，猜测目标主机会把「全局登记」静默变成「只对某台生效」。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    from agent.harness.reins.gate import (
        is_valid_allowlist_entry, normalize_host, scoped_key)
    # hardline 预检：与 grant 端点同一判据（classify_command，只拦 hardline）。
    # 手动登记入口是自由文本框，恰恰是最需要这道闸的地方。
    if req.command and str(req.command).strip():
        from agent.harness.safety import classify_command
        _level, _reason = classify_command(str(req.command))
        if _level == "hardline":
            raise HTTPException(
                status_code=400,
                detail=(f"命令不可放行：{_reason or '命中 hardline 底线'}。"
                        "hardline 命令被无条件阻断，allowlist 无法放行，"
                        "登记预授权不会生效；请改用安全的替代方案。"),
            )
    pg = _get_global_permission_gate_sync()
    host = (req.host or "").strip()
    try:
        grant_key = pg.grant_key_for(req.tool, req.command, scope="always")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 主机段与键拼成一行再校验：复用读侧同一判据，避免「面板允许输入
    # `a|b`，写进文件后读侧判非法丢弃」这种写得进读不回的形态。
    entry = scoped_key(host, grant_key)
    if not is_valid_allowlist_entry(entry):
        raise HTTPException(
            status_code=400,
            detail=("主机标识不在授权清单允许的格式内"
                    "（期望 [A-Za-z0-9_.:-]，不接受空白、换行或斜杠）"),
        )
    await append_record_async(
        action=f"permission_allow_manual:{req.tool}",
        # S2-04：手动登记永久授权不属于任何 run（它写的是跨 run 的 allowlist），
        # run_id 留空是正确取值，不是漏传 —— 与 permission_revoke 同一条口径。
        # environment 同样留空：登记本身不针对某一次运行；作用范围改由
        # host 维度表达（全局 or 某台主机），写进 result 供审计页辨认。
        # "granted" 不在 audit.append_record 的结论枚举里，会被静默改写为 "executed"
        # —— 手动登记永久授权就伪装成了"执行过一条命令"。用同义且在枚举内的值。
        conclusion="approved",
        target_type="tool",
        target_id=(f"@{normalize_host(host)}|{req.command or req.tool}"
                   if host else (req.command or req.tool)),
        command_raw=req.command or "",
        result=f"host={normalize_host(host) or '全局'},entry={entry}",
        # 与 grant 同一姿态：登记生效前必须已有记录，写不进去则不登记
        critical=True,
    )
    pg.grant(req.tool, scope="always", command=req.command, host=host)
    return {"tool": req.tool, "grant_key": grant_key,
            "host": normalize_host(host), "granted": True}


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
    渲染层自带一份词表来弹确认（旧 bulkCommandDiff.ts 的 DANGEROUS_PATTERNS，
    现已随安全分类端点下线）—— 那是
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


class StructureTarget(BaseModel):
    """当前 active target —— 结构化时主机字段的唯一来源（前端从 useHomeTarget 取）。"""
    host: str = ""
    session_type: Literal["local", "remote"] = "local"


class StructureRequest(BaseModel):
    """POST /agent/structure —— 长任务结构化的纯推理请求（发送前可选旁路）。

    raw_text 上限与 AgentRunRequest.task 同值：改一处须同步另一处与前端
    TASK_CHAR_LIMIT，否则要么结构化收不下、要么采用后发送 422。"""
    raw_text: str = Field(..., min_length=1, max_length=50000)
    active_target: StructureTarget = StructureTarget()
    # 仅作展示提示（前端读当前主机判定）；结构化端点不据此放行任何操作，
    # 真正的 prod 审批强化仍发生在下游真实 run（见 task_structure §0 不变量）。
    environment: Literal["test", "prod"] = "test"
    posture: Optional[Literal["goal", "plan"]] = None
    # 已连接主机白名单：结构化文本里出现集合外的主机 = 臆测 → 逼用户确认。
    allowed_hosts: list[str] = []
    model_id: str = ""


@router.post("/agent/structure")
async def structure_task(req: StructureRequest):
    """把一段较长的模糊运维需求拆成结构化草稿（纯推理，无工具，不执行任何东西）。

    spec 2026-10-24-task-structuring · 接缝 S2。

    两道护栏互相兜底：① 系统提示词让模型「绝不自定主机、写动作非原话即 added」；
    ② coerce_structure_payload 无论模型是否听话都硬锁主机字段、收敛写步骤、
    plan 姿态去写、扫描外机。第二道是可单测的安全底座（见 test_task_structure_*）。

    刻意只 tools=[]、enable_thinking=False：这是一次无工具 LLM 推理，本端点不可能
    触达 shell/SSH/写文件 —— 它是发送前的可选旁路，不是发送前置条件（失败前端降级，
    原稿照发）。"""
    from agent.task_structure import (
        _MAX_STRUCTURE_TOKENS,
        build_structure_system_prompt,
        coerce_structure_payload,
    )

    # 模型解析与 run 同一口径：显式 model_id 优先，否则 SSH 档默认再全局默认。
    effective_model_id = req.model_id
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, SSH_MODE)
        if default_model:
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, None)
        if default_model:
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        raise HTTPException(status_code=400, detail="未配置默认模型，无法结构化")

    model_cfg = await asyncio.to_thread(_get_model_config, effective_model_id)
    if not model_cfg:
        raise HTTPException(status_code=404, detail=f"模型不存在: {effective_model_id}")

    system_prompt = build_structure_system_prompt(req.active_target.host, req.environment)
    from agent.harness.llm_client import StreamLLMClient

    llm = StreamLLMClient(
        base_url=model_cfg["base_url"],
        api_key=model_cfg["api_key"],
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": req.raw_text},
    ]
    try:
        out = await asyncio.wait_for(
            llm.call(
                messages=messages,
                tools=[],  # 空工具面 = 纯推理：本端点不可能执行任何操作
                model=model_cfg.get("model_id") or model_cfg.get("name", ""),
                temperature=0.2,
                max_tokens=_MAX_STRUCTURE_TOKENS,
                enable_thinking=False,
                top_p=model_cfg.get("top_p", 0.9),
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="结构化超时，请直接发送原文")
    except Exception as e:
        logger.warning("结构化 LLM 调用失败: %s", e)
        raise HTTPException(status_code=502, detail="结构化失败，原文可照常发送") from e

    payload = _extract_json_object(out.content)
    if payload is None:
        # 失败诚实上报：不返回"空结构"骗前端当成功，前端据此降级保留原稿可发。
        raise HTTPException(status_code=502, detail="结构化返回无法解析，原文可照常发送")

    coerced = coerce_structure_payload(
        payload,
        active_host=req.active_target.host,
        environment=req.environment,
        posture=req.posture,
        allowed_hosts=req.allowed_hosts,
    )
    coerced["meta"] = {
        "model_used": model_cfg.get("model_id") or model_cfg.get("name", ""),
        "tokens_in": out.prompt_tokens,
        "tokens_out": out.completion_tokens,
    }
    return coerced


class CommitMessageRequest(BaseModel):
    """POST /agent/commit-message —— 提交信息生成的纯推理请求（无工具，不触达仓库）。

    输入 = 仓库名 / 分支 / 变更文件清单（带 porcelain 状态前缀）+ 可选 diff 摘要
    （选中文件对 HEAD 的 unified diff，渲染层已按 clampDiffText 截断）；模型只做
    文字归纳，这里再钉一层字段上限。"""
    repo: str = Field(..., min_length=1, max_length=200)
    branch: str = Field("", max_length=256)
    files: list[str] = Field(..., min_length=1, max_length=500)
    diff: str = Field("", max_length=8000)


def _sanitize_commit_message(text: str) -> str:
    """LLM 输出 → 合法单行提交信息：剥围栏行/引号、取首行、截断到 200（与
    shared/git-repos-core.isValidCommitMsg 同口径，主进程提交前还会复核）。"""
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.split("\n")
        lines = lines[1:]  # 首行围栏（含语言标注整行）
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    first = s.split("\n", 1)[0].strip().strip('`"“”').strip()
    if len(first) > 200:
        first = first[:200].rstrip()
    return first


@router.post("/agent/commit-message")
async def generate_commit_message(req: CommitMessageRequest):
    """按变更文件清单生成 conventional commits 提交信息（纯推理，无工具）。

    与 /agent/structure 同一管线与护栏：tools=[] + enable_thinking=False ——
    本端点不可能执行任何操作，只是文字归纳。失败返回 4xx/5xx，前端降级为
    规则草稿或手写，不阻塞提交。"""
    # 模型解析：默认（SSH 档 > 全局档）> 第一个可用模型 —— 库里有模型但没标默认时
    # 不强迫用户先去设置页打标（提交信息生成是可选旁路，降级路径本就存在）；
    # 一个模型都没有才要求先配置。
    cfg = await asyncio.to_thread(db.get_default_model, SSH_MODE) or await asyncio.to_thread(db.get_default_model, None)
    effective_model_id = (cfg or {}).get("id") or (cfg or {}).get("model_id")
    if not effective_model_id:
        models = await asyncio.to_thread(db.list_models_config)
        if not models:
            raise HTTPException(
                status_code=400,
                detail="还没有可用的 AI 模型：请到 设置 → AI 模型 添加并保存，然后重试",
            )
        effective_model_id = models[0].get("id") or models[0].get("model_id")

    model_cfg = await asyncio.to_thread(_get_model_config, effective_model_id)
    if not model_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"模型配置已失效（{effective_model_id}）：请到 设置 → AI 模型 检查后重试",
        )

    listing = "\n".join(f[:200] for f in req.files)
    system_prompt = (
        "你是 git 提交信息生成器。根据变更文件清单（及提供的 diff 内容，若有）写一条 "
        "conventional commits 风格的提交信息：格式为 type(scope): 描述 或 type: 描述；"
        "type 从 feat/fix/docs/chore/test/refactor 里选，scope 用最贴切的模块名（拿不准就省略）；"
        "描述用简体中文、不超过 30 字、概括这批变更实际做了什么——有 diff 时以 diff 内容为准，"
        "没有 diff 时根据文件路径与状态合理推断。"
        "只输出这一行提交信息本身，不要解释、引号或代码围栏。"
    )
    user_content = f"仓库：{req.repo}\n分支：{req.branch or '未知'}\n变更文件（状态 路径）：\n{listing}"
    if req.diff.strip():
        user_content += f"\n\n变更 diff（可能已截断）：\n{req.diff.strip()}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    from agent.harness.llm_client import StreamLLMClient

    llm = StreamLLMClient(base_url=model_cfg["base_url"], api_key=model_cfg["api_key"])
    try:
        out = await asyncio.wait_for(
            llm.call(
                messages=messages,
                tools=[],  # 空工具面 = 纯推理：本端点不可能执行任何操作
                model=model_cfg.get("model_id") or model_cfg.get("name", ""),
                temperature=0.3,
                max_tokens=120,
                enable_thinking=False,
                top_p=model_cfg.get("top_p", 0.9),
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="模型服务 30 秒未响应：可重试，或直接手写提交信息")
    except Exception as e:
        logger.warning("提交信息 LLM 调用失败: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"模型调用失败（{str(e)[:120] or '服务异常'}）：可重试，或直接手写提交信息",
        ) from e

    message = _sanitize_commit_message(out.content)
    if not message:
        raise HTTPException(status_code=502, detail="模型返回了空结果：可重试，或直接手写提交信息")
    return {"message": message}


def _extract_json_object(text: str) -> Optional[dict]:
    """从 LLM 正文里取第一个 JSON 对象：容忍 ```json 围栏与前后解释文字。"""
    import json as _json
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        # 去掉可能的 json 语言标注行
        if s.lower().startswith("json"):
            s = s[4:]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = _json.loads(s[start:end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


class DenyCommandRequest(BaseModel):
    """visible_terminal 拒绝命令请求 — 标记某 run 下某命令被拒绝"""
    tool: str = Field(..., description="工具名（通常为 shell_exec）")
    command: str = Field(..., description="被拒绝的命令")
    run_id: str = Field("", description="运行 ID（拒绝记录按 run_id 隔离）")


@router.post("/agent/permission/deny-command")
async def deny_command(req: DenyCommandRequest):
    """标记某 run 下某命令被用户拒绝

    审查R2修复：改 async def 回到事件循环单线程操作 PermissionGate 状态，
    与 grant 端点同一姿态（详见 grant 路由 docstring）。

    visible_terminal 拒绝时调用：后续该 run 再次请求此命令时 PermissionGate
    check_permission 直接返回 False，不会再次触发 permission_needed 卡片。
    """
    from agent.harness.reins import _get_global_permission_gate_sync, approval_wait

    if not req.run_id:
        raise HTTPException(status_code=400, detail="deny_command 必须携带 run_id（拒绝记录按 run 隔离）")
    pg = _get_global_permission_gate_sync()
    try:
        denied_key = pg.deny_command(req.tool, req.command, req.run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 先把拒绝标记落了再唤醒：醒来时重判会走「命令曾被用户拒绝」分支，
    # 顺序反过来就是「用户拒了、run 却拿到一句放行」。
    approval_wait.resolve_for_call(
        req.run_id, req.tool, denied_key, approval_wait.DENIED)
    # 审计记录
    await append_record_async(
        action=f"permission_deny_command:{req.tool}",
        conclusion="denied",
        run_id=req.run_id,
        target_type="tool",
        target_id=denied_key,
        # 拒绝同样是「对某台主机的处置决定」：环境列取 run 的服务端判定，
        # 否则一次 prod 上的拒绝在 prod 审计视图里查不到
        environment=_run_environment(req.run_id),
        command_raw=req.command,
    )
    return {"tool": req.tool, "command": req.command,
            "run_id": req.run_id, "denied_key": denied_key}


def _release_run_interaction_state(run_id: str) -> tuple[int, int]:
    """回收 run 级交互状态：once 授权池 + 拒绝记录 + 澄清向导上下文 + 审批等待项。

    ReflectionRein 只在 AGENT_COMPLETE / AGENT_FAILED 正常终态触发。用户点"停止"
    或客户端断连时 driver 直接被 cancel，它不会跑 —— 三个池子便随每次中止的 run
    无界增长（授权池残留尤其危险：无人消费的 once 额度只能等进程重启）。
    全部是同步字典操作（内部无 await），因此无需 shield 也不会被取消打断。

    Returns:
        (释放的 once 授权条数, 释放的拒绝记录条数)
    """
    from agent.harness.reins import (
        _get_global_permission_gate_sync, approval_wait, release_clarification_run)
    pg = _get_global_permission_gate_sync()
    released = pg.cleanup_run_once(run_id)
    try:
        # 先把还在等用户点击的调用叫醒（裁决=aborted → 不重判，走原有拦截路径）。
        # 漏这一步的话，release-run 后等待项还挂着，下一次 grant 会去 set_result
        # 一个没人在等的 future：日志里只剩一句「已授权」，run 早已不在。
        approval_wait.abort_run(run_id)
    except Exception:
        logger.warning("release_run: 审批等待项回收异常 run=%s", run_id, exc_info=True)
    try:
        denied = pg.cleanup_run_denied(run_id)
    except Exception:
        logger.warning("release_run: denied 清理异常 run=%s", run_id, exc_info=True)
        denied = 0
    try:
        release_clarification_run(run_id)
    except Exception:
        logger.warning("release_run: 澄清回收异常 run=%s", run_id, exc_info=True)
    return released, denied


@router.post("/agent/permission/release-run/{run_id}")
async def release_run_permissions(run_id: str):
    """手动清理某次 run 的全部 once 授权/拒绝记录/澄清状态（前端显式终止或超时时调用）

    审查R2修复：改 async def —— _release_run_interaction_state 同时被 SSE 侧
    finally（事件循环线程）调用，此前本路由在 anyio 线程池里 pop 同一批 dict，
    两处并发互为竞态源；回到循环后互斥天然成立。审计文件写放线程池。

    ReflectionRein 在 AGENT_COMPLETE / AGENT_FAILED 时自动回收这三样，
    本接口作为兜底：前端在用户点击"停止"或 run 被强制终止时主动释放。
    SSE 侧的 finally 也调用同一 helper —— 客户端直接断连时不会经过本接口。
    """
    released, denied_count = _release_run_interaction_state(run_id)
    await append_record_async(
        action=f"permission_release_run:{run_id}",
        conclusion="released",
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        # 释放动作也属于这次 run 的环境（与 grant / deny 同口径）
        environment=_run_environment(run_id),
        result=f"released={released} denied={denied_count}",
    )
    return {"run_id": run_id, "released": released, "denied": denied_count}


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
async def submit_clarification_answer(req: ClarificationAnswerRequest):
    """用户回答完澄清问题后调用 — 写入答案到 pending 存储

    审查R2修复：改 async def —— _CLARIFICATION_DONE/_CLARIFICATION_CONTEXT 与
    PermissionGate 同为「循环内消费 + 线程池写入」的进程级可变状态（澄清拦截器、
    release-run 回收都跑在事件循环上），此前本路由在 anyio 线程池里写它们存在
    跨线程竞态；回到循环后天然串行。审计文件写放线程池。

    前端 ClarifyingWizard 在用户答完所有步（或点击"完成"）后提交。
    下一轮 PRE_TOOL_CALL 时 ClarificationInterceptor 读到 _CLARIFICATION_DONE
    即注入用户答案上下文、发 clarification_done 事件、放行工具执行。
    """
    from agent.harness.reins import record_clarification_answers
    # answers 已由 field_validator 清洗：step_idx 合法、choice 受控、custom_text 截断
    # P1-6: 统一入口维护 done+context 配对与时间戳（TTL 可回收孤儿答案）
    record_clarification_answers(req.run_id, req.answers)
    logger.info("clarification answer: run_id=%s steps=%d", req.run_id, len(req.answers))
    # 审计：向导的两个终态必须可事后复原（answered / skipped）
    await append_record_async(
        action="clarification_answer",
        conclusion="answered",
        run_id=req.run_id,
        target_type="run",
        target_id=req.run_id,
        result=f"steps={len(req.answers)}",
    )
    return {"run_id": req.run_id, "status": "recorded", "steps": len(req.answers)}


@router.post("/agent/clarification/skip")
async def skip_clarification(req: ClarificationAnswerRequest):
    """用户跳过所有剩余澄清步 — 写入空答案（空 dict 表示无约束放行）

    审查R2修复：改 async def，与 clarification/answer 端点同一姿态（循环内
    写澄清状态、线程池写审计）。

    ClarificationInterceptor 在格式化空答案时输出"用户选择跳过澄清"提示，
    LLM 按原始命令执行，但仍在 PermissionGate 的权限控制之下。
    """
    from agent.harness.reins import record_clarification_answers
    record_clarification_answers(req.run_id, {})  # 空 answers → _format_answers 输出"跳过"
    logger.info("clarification skip: run_id=%s", req.run_id)
    await append_record_async(
        action="clarification_skip",
        conclusion="skipped",
        run_id=req.run_id,
        target_type="run",
        target_id=req.run_id,
    )
    return {"run_id": req.run_id, "status": "skipped"}


# ── Agent 记忆管理（v2：统一 memories 表 + jobs/audit/blocks）──


class MemoryEditRequest(BaseModel):
    content: Optional[str] = None
    title: Optional[str] = None
    pinned: Optional[bool] = None


class BlockPutRequest(BaseModel):
    value: str


class MemoryCreateRequest(BaseModel):
    kind: Literal["semantic", "episodic", "procedural"] = "semantic"
    content: str
    title: str = ""
    scope: str = "global"
    importance: float = Field(0.7, ge=0.0, le=1.0)


@router.get("/agent/memories")
def list_agent_memories(
    kind: Optional[str] = None,
    mem_type: Optional[str] = None,   # 旧参数名兼容（P4 前端切 kind 后移除）
    scope: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """v2 记忆列表（embedding 不出后端，仅 has_embedding 标记）"""
    rows = mstore.list_memories(
        kind=kind or mem_type, scope=scope, status=status, q=q, limit=limit, offset=offset,
    )
    return {"memories": rows, "count": len(rows)}


@router.get("/agent/memories/stats")
def memory_stats():
    """按 kind/status 的计数 + job 队列水位（管理面板头部）"""
    return mstore.stats()


@router.patch("/agent/memories/{memory_id}")
def edit_agent_memory(memory_id: str, req: MemoryEditRequest):
    """用户编辑：改内容（旧向量作废）与/或 pin 状态"""
    row = mstore.get_memory(memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="记忆不存在")
    if req.content is not None:
        if not req.content.strip():
            raise HTTPException(status_code=400, detail="内容不能为空")
        mstore.user_edit(memory_id, content=req.content.strip(),
                         title=(req.title if req.title is not None else row.get("title") or ""))
    if req.pinned is not None:
        mstore.set_pinned(memory_id, req.pinned)
    return {"updated": True, "memory": mstore.get_memory(memory_id)}


@router.post("/agent/memories")
def create_agent_memory(req: MemoryCreateRequest):
    """用户手动新增：直写 active 记忆并置顶（pinned），不过对账管线。

    对账是为自动沉淀准备的（判重、消解矛盾）；用户在 UI 里敲定的事实本身就是仲裁
    结果，再让模型判一遍只会把它改写成另一句话。pinned 让它不参与池容量修剪与衰减
    ——见 mstore.trim_pools/decay_memories 的 pinned=0 条件。
    不嵌向量：本路径没有模型凭证上下文，宁缺毋滥（与 user_edit 置 NULL 同策略），
    FTS 关键词通道照常召回，列表里的 has_embedding 如实显示 false。
    """
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    scope = req.scope.strip() or "global"
    if scope != "global" and not (scope.startswith("host:") or scope.startswith("run:")):
        raise HTTPException(status_code=400, detail=f"非法 scope: {scope}")
    mid = mstore.add_memory(
        kind=req.kind, content=content, scope=scope,
        title=req.title.strip(), source="user_ui",
        importance=req.importance, pinned=1,
    )
    mstore.add_audit(mid, "ADD", new=content, reason="user_ui")
    return {"created": True, "id": mid, "memory": mstore.get_memory(mid)}


@router.delete("/agent/memories/{memory_id}")
def delete_agent_memory(memory_id: str):
    """用户删除 = invalidate（失效不物理删，审计链保留）"""
    ok = mstore.retire(memory_id, reason="user_delete")
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在或已失效")
    return {"deleted": True, "id": memory_id}


@router.post("/agent/memories/clear")
def clear_agent_memories():
    """清空：全部非 pinned 记忆 invalidate（pinned 是用户显式保留，永不被批量操作波及）"""
    return {"invalidated": mstore.retire_all_unpinned()}


@router.get("/agent/memories/audit")
def list_memory_audit(memory_id: Optional[str] = None, limit: int = Query(50, ge=1, le=500)):
    return {"audit": mstore.list_audit(memory_id=memory_id, limit=limit)}


@router.get("/agent/memories/jobs")
def list_memory_jobs(status: Optional[str] = None, limit: int = Query(50, ge=1, le=500)):
    return {"jobs": mstore.list_jobs(status=status, limit=limit)}


@router.post("/agent/memories/jobs/{job_id}/retry")
def retry_memory_job(job_id: str):
    if not mstore.retry_job(job_id):
        raise HTTPException(status_code=404, detail="job 不存在或非 failed 状态")
    return {"retried": True, "id": job_id}


@router.get("/agent/blocks")
def list_memory_blocks():
    return {"blocks": mstore.list_blocks()}


@router.put("/agent/blocks/{key}")
def put_memory_block(key: str, req: BlockPutRequest):
    """UI 整块写入（agent 只能走 replace 工具，见 tools/memory.py）"""
    from agent.memory import blocks as memory_blocks
    if not memory_blocks.allowed_key(key):
        raise HTTPException(status_code=400, detail=f"非法块键: {key}")
    try:
        version = mstore.put_block(key, req.value, updated_by="user_ui")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"key": key, "version": version}


@router.get("/agent/blocks/{key}/history")
def memory_block_history(key: str, limit: int = Query(20, ge=1, le=100)):
    return {"history": mstore.list_block_history(key, limit=limit)}


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


@router.post("/agent/tick/run")
async def consciousness_tick_run():
    """手动执行一次 tick（对账 + 维护），返回执行结果"""
    return await get_global_tick().trigger_now()


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


@router.get("/agent/runs")
async def list_agent_runs(
    status: Optional[str] = Query(default=None, max_length=32),
    run_id: Optional[str] = Query(default=None, max_length=128),
    conversation_id: Optional[str] = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """按时间倒序列出 Agent 运行记录（运维审计页「运行列表」分段的数据源）。

    为什么要有这一条：此前只有 /agent/runs/{run_id}/trace 单查，前端要找回一条
    已经跑完的 run 只能靠消息气泡里那枚 ▸轨迹 —— 会话被删，号就无处可查。
    items 与 total 共用 database._run_filters，两个各抄一遍迟早对不上。
    query/count 是同步 sqlite，移入线程池避免阻塞事件循环（与 /audit/records 同法）。
    """
    items, total = await asyncio.to_thread(
        lambda: (
            db.get_agent_runs(
                conversation_id=conversation_id, status=status, run_id=run_id,
                limit=limit, offset=offset,
            ),
            db.count_agent_runs(conversation_id=conversation_id, status=status, run_id=run_id),
        )
    )
    return {"items": items, "total": total}


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
                # 三态透传：NULL = 调用发起了但结果从未回写（崩溃/中止）。原写法
                # bool(tc.get("success", 1)) 把 NULL 折成 True，半截调用被回放成成功。
                "success": None if tc.get("success") is None else bool(tc["success"]),
                "duration_ms": tc.get("duration_ms"),
                "created_at": tc.get("created_at"),
            }
            for tc in tool_calls
        ],
    }


