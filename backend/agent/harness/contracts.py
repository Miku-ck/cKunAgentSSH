"""
Harness 契约层 — 所有组件共享的数据类型与协议

设计原则：
- 所有组件通过 dataclass / Protocol 交互，不直接依赖具体实现
- TraceEvent 是唯一的跨组件事件格式，可序列化（to_dict 可直接 JSON 化）
- UndoAction 是工具回滚的协议，每个工具可选实现
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


# ──────────────────────────────────────────────────────────────────────────────
# 状态机
# ──────────────────────────────────────────────────────────────────────────────

class State(str, Enum):
    """Agent 运行状态 — Driver 内部表达，替代原 state_machine.py

    C-review NIT：PLANNING / REFLECTING 是保留态 — 当前 Driver 从不进入
    （保留枚举成员以兼容 state_history 消费方与潜在后续规划/反思阶段）。
    state_history 消费者不应假定这两个状态会出现。
    """
    INIT = "init"
    PLANNING = "planning"  # 保留态：Driver 从不进入（C-review NIT）
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    REFLECTING = "reflecting"  # 保留态：Driver 从不进入（C-review NIT）
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_REACHED = "max_reached"
    HUMAN_HELP = "human_help"  # H14 新增：人工介入状态（终止态）


# run 的终态集合：进入后不应再转移出去，同时是「这个会话还有没有在跑的 run」的判据。
# 这份清单原先在三处各抄一遍 —— database.update_agent_run（置 completed_at）、
# routes/agent.py（释放陈旧 run 占位）、agent/reflection.py（状态转移异常检测），
# 且 reflection 那份漏了 HUMAN_HELP，与上面枚举自己的注释「终止态」矛盾：
# 一条以人工介入结束的 run 在反思侧被视作仍可继续，转移异常一条都不报。
# 用 .value 收进集合而不是直接放成员：str-Enum 的哈希语义不保证与裸字符串一致。
TERMINAL_RUN_STATES: frozenset[str] = frozenset(
    st.value for st in (State.COMPLETED, State.FAILED, State.MAX_REACHED, State.HUMAN_HELP)
)


# 会改动作文件系统工具名（run 摘要的 mutated 统计与预算守卫的「已触碰文件」共用）。
# 原先 driver._FILE_MUTATION_TOOLS 与 flow._FILE_MUTATION_TOOLS 各写一份字面量，
# 两者之间唯一的联系是 flow 那句「与 driver 对齐」的注释 —— 加一个注册名只改一处，
# 摘要与预算就会静默分叉（一侧把文件变更算成未知工具）。
# 必须用 builtin 实际注册名：work 模式旧名（write_file/patch_file/edit_file）已随
# 全库清理移除，留旧名的那一侧正是 2026-09 那次修复的成因。
FILE_MUTATION_TOOLS: frozenset[str] = frozenset({"file_write", "file_edit"})


# 无副作用的只读观测工具 —— 单一事实源。
# 两个消费方，同一个判定（「纯观测、重复调用无副作用」）：
#   1. driver 只读工具并行化：同一轮 LLM 产出的连续只读调用可并发执行
#      （段间以副作用工具为屏障，保证 read→write→read 的顺序语义不被破坏）；
#   2. LoopDetector 循环检测放宽：读取大文件、目录遍历、观测轮询是合理的
#      高频重复调用，长窗口阈值放宽到 7。
# ⚠️ 语义边界（勿混淆，两套口径合并必然漂移）：
#   - plan_mode.PLAN_READ_ONLY_TOOLS 是「计划模式下允许的工具」，含
#     todo_write/plan_write/spec_write/goal_write 这类【写工件】工具 ——
#     它们 safe+reversible+idempotent 三判据全满足但有写副作用，绝不能进本集合；
#   - DynamicBudgetRein 的读文件统计判据是「带 path 参数的读工具」（子集，
#     见 flow._FILE_READ_TOOLS），与本集合的「无副作用」不是同一件事。
# 注意：web_search/web_fetch/memory_search/load_skill 虽也无副作用，但不入本集合
# —— 并行扇出的目标场景是「同一 SSH 会话的远程观测」（sys_overview+process_inspect+
# network_probe+log_read 一次采集全机状态），外部网络请求与全局状态读取保持串行，
# 避免并发放大对外部服务的压力。
READONLY_TOOLS: frozenset[str] = frozenset({
    "file_read", "file_glob", "file_grep", "read_image",
    "log_read", "sys_overview", "process_inspect", "network_probe",
    "job_list", "job_output",
})


# prod 强化的「未预授权」拒绝标记 —— 拦截原因子串的单一事实源。
# 两个生产方（guards.py shell 路径 / gate.py 工具级路径）拼 intercept_reason
# 时都必须引用本常量；消费方 ConfidenceMonitor 用它把「策略拒绝」从工具失败
# 里豁免出去（政策堵路 ≠ 模型困惑，见 flow.py）。字符串字面量散落三处必然
# 漂移——一旦措辞改动，豁免与求助卡静默失效，run 又会烧到「低置信度」误诊。
POLICY_DENY_MARKER: str = "未预授权"


def is_intercepted_entry(entry: dict) -> bool:
    """判定一条 tool_calls_log 条目是否被拦截（工具未真正执行）。

    driver 的拦截分支必写 intercept_reason（见 _execute_serial_call），
    执行过的条目（成功或命令真跑了但非零退出）没有这个键。
    """
    return entry.get("success") is False and bool(entry.get("intercept_reason"))


def is_policy_denial(entry: dict) -> bool:
    """判定一条 tool_calls_log 条目是否为 prod 策略拒绝（未预授权）。

    依赖 driver 在拦截分支写入 entry["intercept_reason"]；普通失败
    （命令真跑了但非零退出/异常）不含该标记，照常计入置信度扣分。
    """
    if not is_intercepted_entry(entry):
        return False
    return POLICY_DENY_MARKER in (entry.get("intercept_reason") or "")


class ErrorType(str, Enum):
    """错误分类 — Stirrups 用于决定恢复策略"""
    TRANSIENT = "transient"        # 瞬时（网络/超时/503）— 重试
    INVALID_ARGS = "invalid_args"  # 参数错误（含上下文超限）— 反馈/压缩
    FORBIDDEN = "forbidden"        # 权限/Hook 拦截 — 不重试
    FATAL = "fatal"                # 致命 — 终止


# ──────────────────────────────────────────────────────────────────────────────
# 统一事件契约
# ──────────────────────────────────────────────────────────────────────────────

class TracePhase(str, Enum):
    """事件阶段"""
    DURING = "during"


@dataclass
class TraceEvent:
    """
    统一事件格式 — 所有组件产出的唯一事件类型

    设计目标：
    - 可序列化：to_dict() 可直接 JSON 化
    - 可订阅：TelemetryBus 多订阅者（SSE/日志/指标）
    - 可嵌套：span_id 支持工具执行内部子操作

    注意（C-review MINOR-9）：TelemetryBus 只在 run 生命周期内保留最近
    500 条事件（FIFO 截断），run 结束即 clear、从不落盘 —— 因此**不支持**
    离线重放整个 run。原"保存事件序列即可离线重放"的承诺不成立，已修订。
    如需审计/回放，需另行将事件持久化（见审计链 audit.append_record）。
    """
    kind: str                                  # "state" | "content" | "tool_call_start" | ...
    source: str                                # 产出组件："driver" / "saddle" / "reins" / ...
    run_id: str
    phase: TracePhase = TracePhase.DURING
    iteration: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    span_id: str = ""
    parent_span_id: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.span_id:
            self.span_id = uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        """转可序列化 dict（供 SSE / 持久化）"""
        return {
            "kind": self.kind,
            "source": self.source,
            "run_id": self.run_id,
            "phase": self.phase.value,
            "iteration": self.iteration,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 工具调用契约
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    """单次工具调用 — LLM 输出解析后的结构化表达"""
    id: str                                    # LLM 返回的 tool_call_id
    name: str                                  # 工具名（如 "file_read"）
    args: dict[str, Any]                       # 参数


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    priority: str = "normal"  # 优先级：high（错误/关键）/ normal（常规）/ low（噪声）
    # 可见终端模式：非空时表示命令需交付到可见终端，Driver 挂起等待前端回传
    pending_delivery: Optional[dict] = None


@dataclass
class LLMOutput:
    """LLM 调用结果 — Driver 决策依据"""
    content: str = ""                          # 文本回复
    reasoning: str = ""                        # 推理过程（thinking 模型）
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Any = None                            # 原始响应（调试用）


# ──────────────────────────────────────────────────────────────────────────────
# 回滚契约
# ──────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class UndoAction(Protocol):
    """
    撤销动作协议 — 工具执行前生成，失败时可回滚

    实现示例：
    - FileContentUndo: 保存原文件内容，reverse 时恢复
    - ShellUndo:       记录命令，reverse 时执行反向命令（如 mkdir → rmdir）
    - NoOpUndo:        只读工具（web_search 等），reverse 时空操作
    """

    async def apply_reverse(self) -> None:
        """执行反向操作，撤销工具造成的影响"""
        ...


@dataclass
class NoOpUndo:
    """空撤销 — 用于只读工具（web_search/web_fetch/file_read/file_glob 等）"""
    reason: str = "readonly"

    async def apply_reverse(self) -> None:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# 策略层契约
# ──────────────────────────────────────────────────────────────────────────────

class ReinPoint(str, Enum):
    """
    策略注入点 — 对应原 9 个 hook 事件，但接口统一

    每个注入点对应 Driver 循环中的一个时机，Reins 按优先级执行所有 Interceptor。
    """
    AGENT_START = "agent_start"
    PRE_ITERATION = "pre_iteration"
    PRE_LLM_CALL = "pre_llm_call"
    POST_LLM_CALL = "post_llm_call"
    PRE_TOOL_CALL = "pre_tool_call"
    POST_TOOL_CALL = "post_tool_call"
    POST_ITERATION = "post_iteration"
    AGENT_COMPLETE = "agent_complete"
    AGENT_FAILED = "agent_failed"


@dataclass
class ReinDecision:
    """
    拦截器决策

    intercepted=True 时 Driver 会：
    1. 发出 events（无论是否 intercepted，events 都会被发出）
    2. 执行 undo_actions 回滚（H13 修复 C5：明确语义）
       - 适用于 POST_TOOL_CALL 拦截器声明"工具已执行但需回滚"的场景
       - Driver 在 intercepted=True 时按顺序执行 undo_actions
       - 与 ctx.undo_stack 不同：undo_actions 是拦截器临时声明的，不入栈
    3. 注入 inject_messages 到下一轮 LLM
    4. terminate=True 时（C-review M6）：Driver 拦截后立即 transition(FAILED)
       并终止整个 run（原实现拦截后 continue，拦截器声称"强制终止"但 run 继续
       烧满 max_iterations）
    """
    intercepted: bool = False
    intercept_reason: str = ""
    events: list[TraceEvent] = field(default_factory=list)
    undo_actions: list[UndoAction] = field(default_factory=list)
    inject_messages: list[dict[str, str]] = field(default_factory=list)  # 注入到下一轮 LLM 的纠偏消息
    terminate: bool = False  # 终止整个 run（FAILED）；仅 PRE_TOOL_CALL 的拦截器会设置
    # 等待用户审批时，本次调用实际绑定的授权键（裸键，不含 `@host|` 前缀）。
    # 只有发 permission_needed 的拦截器会填：Driver 拿它向 reins.approval_wait
    # 登记等待项，grant / deny 端点算出同一把键把它唤醒。空串 = 本次拦截不需要
    # 等人（hardline 阻断、prod 未预授权、计划模式拒绝……那些是「直接否掉」）。
    approval_key: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# 澄清向导契约 — 仅当工具调用意图不明确时前置多步问答收窄范围
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ClarificationOption:
    """单个澄清选项（A / B / C 卡片）"""
    id: str                 # "A" | "B" | "C"
    label: str              # 卡片展示文案；可含项目动态上下文引用（如 git diff 摘要）


@dataclass
class ClarificationStep:
    """一步澄清问题（含当前题面 + 选项 + 是否允许 D 自定义输入）"""
    question: str
    options: list[ClarificationOption]
    allow_custom: bool = True


@runtime_checkable
class Interceptor(Protocol):
    """
    拦截器协议 — 所有策略实现这个接口

    内置拦截器（迁移自 hooks/permissions/self_monitor）：
    - FilesystemGuard:   pre_tool_call, 路径越界/敏感 shell 拦截
    - LoopDetector:      pre_tool_call, 循环检测
    - ConfidenceMonitor: post_iteration, 置信度低时求助
    - PermissionGate:    pre_tool_call, 权限检查
    - AuditLogger:       post_tool_call/agent_failed, 审计日志
    """
    point: ReinPoint
    priority: int

    async def check(self, ctx: "RunContext") -> ReinDecision: ...


# ──────────────────────────────────────────────────────────────────────────────
# 运行上下文
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentConfig:
    """Agent 运行配置 — 替代原 engine.py 的 AgentConfig"""
    max_iterations: int = 100
    temperature: float = 0.7
    # 核采样：models_config.top_p 一直有值、面板可改，但此前没有任何一条路径把它
    # 传给 chat_stream（那里硬默认 0.9），所以用户在设置页改的 top_p 从不生效。
    top_p: float = 0.9
    max_tokens: Optional[int] = None  # None 表示不传 max_tokens，由 vLLM 自决输出上限
    enable_thinking: bool = False
    enable_tools: bool = True
    llm_max_retries: int = 2
    compress_on_context_overflow: bool = True
    auto_rollback_on_tool_failure: bool = True  # 工具失败时自动回滚（保护文件一致性）
    # 动态步数预算配置
    enable_dynamic_budget: bool = True  # 是否启用动态步数预算
    dynamic_budget_interval: int = 3   # 每编辑 N 个不同文件追加预算
    dynamic_budget_step: int = 2       # 每次追加的步数
    dynamic_budget_cap: float = 2.0    # 封顶倍数（相对 initial_max）
    # C-review M10：成本/token 硬熔断（POST_ITERATION 的 CostBudgetRein 每轮检查）
    # None → CostBudgetRein 内置默认（$2.0 / 3M tokens）；0 → 显式禁用；>0 → 自定义上限
    max_cost_usd: Optional[float] = None
    max_total_tokens: Optional[int] = None
    # 任务级墙钟 deadline（POST_ITERATION 的 WallClockRein 每轮检查）。
    # 单命令 60s 超时只兜住一条命令挂死，兜不住"每条命令都合法但整体跑不完"
    # 的慢性失控；时间维度的最后防线。0 → 显式禁用。
    max_wall_clock_s: int = 1800
    # ── 强制收敛轮（机制式）──
    # 软预警只是劝导，模型仍可能把最后一轮耗在工具调用上。开启后最后一轮
    # call_llm 不传 tools：模型物理上无法调工具，输出必然是文本 → 走收尾分支，
    # 终态 COMPLETED 而非 MAX_REACHED。False 回退旧行为。
    forced_convergence_enabled: bool = True
    # ── 假完成强校验（CompletionGuard）──
    # 有 workflow goal 的 run 已有 goal 自检锚；这里补无 goal 的 run：
    # 收尾时用轻量 LLM 裁决「最终答案是否覆盖钉扎任务」，FAIL 注入纠偏续跑
    # （每 run 一次），超时/异常/解析失败 fail-open 放行。trivial/simple 不触发。
    completion_guard_enabled: bool = True
    completion_guard_timeout_s: float = 15.0
    # ── 意图识别 + 思考预算（避免无脑长思考拖垮上下文/可观测性）──
    # max_reasoning_tokens: thinking 阶段的 token 上限，None 表示不限制
    # 由 IntentionClassifier 在 run 启动时根据 task 文本推断；
    # Driver 在每轮 LLM 调用前可基于 iteration 动态收紧
    max_reasoning_tokens: Optional[int] = None
    # allow_planning_phase: 是否允许首轮纯规划（不调工具，只输出步骤清单）
    # 仅 complex 等级任务会置为 True，由 Driver 在 iteration=0 时切换引导语
    allow_planning_phase: bool = False
    # enable_intention_guide: 是否启用意图识别引导语注入（默认开启）
    # 关闭时降级为旧行为（无 thinking 预算限制、无规划阶段）
    enable_intention_guide: bool = True
    # ── 渐进式执行控制 ──
    # max_tool_calls_per_turn: 每轮 LLM 回复最多执行的工具调用数
    # 架构层硬约束：超出部分延迟到下一轮执行，防止模型一次性批量操作
    max_tool_calls_per_turn: int = 3
    # enable_progressive_gathering: 启用渐进式信息收集引导
    # 检测批量只读工具调用并注入"先概览再深入"的策略引导
    enable_progressive_gathering: bool = True
    # reclassify_interval: 每隔 N 轮迭代重新评估意图等级
    reclassify_interval: int = 5
    # ── 事后复盘（方案 Phase 2）──
    # 复盘是 run 结束后的额外 LLM 调用（15s 超时、失败静默降级），成本敏感：
    # 默认关闭，由用户显式开启。生成走后台任务，不阻塞 run 终态。
    postmortem_enabled: bool = False

    def __post_init__(self) -> None:
        """温度/核采样在唯一入口校界，而不是各调用点各夹一次。

        models_config 的两列只被写侧 Field(ge/le) 管住：升级前的旧行、手改的库、
        以及 schedule/interactive 两条通道读到的值都可能越界，而越界的 temperature
        或 top_p 发出去就是上游 400 —— 整轮 run 直接失败，用户看不到原因。
        越界回默认而不是钳到边界：一行 "5" 并不说明用户想要 2.0。
        """
        def _bound(value, lo, hi, fallback):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return fallback
            if not (lo <= value <= hi):   # NaN 也走这里：任何比较都为 False
                return fallback
            return float(value)

        self.temperature = _bound(self.temperature, 0.0, 2.0, 0.7)
        self.top_p = _bound(self.top_p, 0.0, 1.0, 0.9)


@dataclass
class RunContext:
    """
    运行上下文 — 贯穿整个 run 的可变状态

    与原 AgentRunContext 的区别：
    - 不再持有 base_url/api_key/model（移到 Bridle 的 LLMClient）
    - 增加 state / undo_stack / span_stack / metadata
    - 增加 transition() 方法替代原 state_machine
    """
    run_id: str
    task: str
    conversation_id: str
    system_prompt: str
    history: list[dict[str, str]]
    memories: list[str]
    config: AgentConfig

    # 运行时状态
    state: State = State.INIT
    iteration: int = 0
    messages: list[dict[str, str]] = field(default_factory=list)  # 累积消息
    tool_calls_log: list[dict[str, Any]] = field(default_factory=list)  # 工具调用日志
    state_history: list[dict[str, str]] = field(default_factory=list)
    undo_stack: list[UndoAction] = field(default_factory=list)
    # H13 修复 A1：span 栈 — 用于 parent_span_id 嵌套追踪
    # PRE_TOOL_CALL 时 push tool_call_id，POST_TOOL_CALL 时 pop
    # tool_call_start 事件取 span_stack[-2] 作为 parent_span_id
    span_stack: list[str] = field(default_factory=list)
    # 只读工具并行化（2026-10）：driver 正在检查/收尾的那条 tool_calls_log 条目。
    # 6 处 Reins 读取点原先统一读 tool_calls_log[-1]，把「列表最后一条」当作
    # 「当前正在检查的那条」—— 串行执行时二者等价；并行段内 POST 阶段处理
    # 第 i 条时 [-1] 已是第 N 条，语义被打破。因此：
    #   - driver 在每次 PRE/POST 检查前显式写入本字段，检查点统一走
    #     ctx.active_tool_call（见下方 property）；
    #   - 未显式写入时（直接构造 ctx 跑单测 / 串行路径未设置）回退
    #     tool_calls_log[-1]，保持全部既有测试与调用方语义逐字不变。
    current_tool_call: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # LLM 配置（Bridle 注入）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 技能 ID 列表（前端显式选择），SkillRein 在 AGENT_START 时注入到 system_prompt
    skill_ids: list[str] = field(default_factory=list)
    # 是否启用按触发词自动匹配技能（False 时 SkillRein 仅注入显式 skill_ids）
    auto_skill_match: bool = True
    # SSH 会话 ID（SSH 模式下由前端传入，供 shell_exec 等工具远程执行）
    ssh_session_id: str = ""
    # 目标环境标记（"test" | "prod"），驱动生产高风险命令的审计与审批强化
    environment: str = "test"
    # 可见终端模式：True 时 shell_exec 命令交付到可见终端（前端 PTY/SSH）而非 headless 执行
    visible_terminal: bool = False

    @classmethod
    def new(cls, task: str, **opts) -> "RunContext":
        """工厂方法 — 生成新 run_id"""
        return cls(
            run_id=uuid.uuid4().hex,
            task=task,
            conversation_id=opts.get("conversation_id", ""),
            ssh_session_id=opts.get("ssh_session_id", ""),
            environment=opts.get("environment", "test"),
            visible_terminal=opts.get("visible_terminal", False),
            system_prompt=opts.get("system_prompt", ""),
            history=opts.get("history", []),
            memories=opts.get("memories", []),
            config=opts.get("config", AgentConfig()),
            skill_ids=opts.get("skill_ids", []),
            auto_skill_match=opts.get("auto_skill_match", True),
        )

    async def transition(self, new_state: State, event: str = "") -> None:
        """状态转移 — 记录历史"""
        old = self.state
        self.state = new_state
        self.state_history.append({"from": old.value, "event": event, "to": new_state.value})

    def push_undo(self, action: UndoAction) -> None:
        self.undo_stack.append(action)

    def remove_undo(self, action: UndoAction) -> None:
        """按【身份】移除自己的 undo 条目。

        saddle.execute 失败回滚后原先用 undo_stack.pop() 弹【栈顶】—— 串行时
        刚 push 的就是自己的，二者等价；并行段内别的调用可能在自己之后 push，
        弹栈顶会误删别人的条目（尤其 file_write/file_edit 的 FileContentUndo
        被误删后，那个文件就失去了回滚能力）。必须按 is 身份定位。
        """
        for i in range(len(self.undo_stack) - 1, -1, -1):
            if self.undo_stack[i] is action:
                self.undo_stack.pop(i)
                return

    @property
    def active_tool_call(self) -> dict[str, Any] | None:
        """当前正在被 Reins 检查的那条 tool_calls_log 条目（单一事实源）。

        全部 PRE/POST_TOOL_CALL 拦截器一律经此读取「当前调用」，不再直接
        读 tool_calls_log[-1]（并行段内 [-1] 已不是当前条目，见
        current_tool_call 字段注释）。未显式设置时回退 [-1]。
        """
        if self.current_tool_call is not None:
            return self.current_tool_call
        return self.tool_calls_log[-1] if self.tool_calls_log else None


# ──────────────────────────────────────────────────────────────────────────────
# LLM 客户端协议
# ──────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端协议 — Stirrups 通过它调用 LLM，便于 mock"""

    async def call(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        enable_thinking: bool,
        reasoning_max_tokens: Optional[int] = None,
        top_p: float = 0.9,
    ) -> LLMOutput:
        ...
