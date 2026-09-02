"""
Harness 契约层 — 所有组件共享的数据类型与协议

设计原则：
- 所有组件通过 dataclass / Protocol 交互，不直接依赖具体实现
- TraceEvent 是唯一的跨组件事件格式，可序列化、可重放
- UndoAction 是工具回滚的协议，每个工具可选实现
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Protocol, Union, runtime_checkable


# ──────────────────────────────────────────────────────────────────────────────
# 状态机
# ──────────────────────────────────────────────────────────────────────────────

class State(str, Enum):
    """Agent 运行状态 — Driver 内部表达，替代原 state_machine.py"""
    INIT = "init"
    PLANNING = "planning"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    REFLECTING = "reflecting"
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_REACHED = "max_reached"
    HUMAN_HELP = "human_help"  # H14 新增：人工介入状态（终止态）

    @property
    def is_terminal(self) -> bool:
        return self in (State.COMPLETED, State.FAILED, State.MAX_REACHED, State.HUMAN_HELP)

    @property
    def is_active(self) -> bool:
        return self in (State.PLANNING, State.THINKING, State.ACTING, State.OBSERVING, State.REFLECTING)


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
    PRE = "pre"
    DURING = "during"
    POST = "post"


@dataclass
class TraceEvent:
    """
    统一事件格式 — 所有组件产出的唯一事件类型

    设计目标：
    - 可序列化：to_dict() 可直接 JSON 化
    - 可重放：保存事件序列即可离线重放整个 run
    - 可订阅：TelemetryBus 多订阅者（SSE/日志/指标/重放）
    - 可嵌套：span_id 支持工具执行内部子操作
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
    name: str                                  # 工具名（如 "read_file"）
    args: dict[str, Any]                       # 参数


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    priority: str = "normal"  # 优先级：high（错误/关键）/ normal（常规）/ low（噪声）


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
    - FileWriteUndo: 保存原文件内容，reverse 时恢复
    - ShellUndo:     记录命令，reverse 时执行反向命令（如 mkdir → rmdir）
    - NoOpUndo:      只读工具（web_search 等），reverse 时空操作
    """

    async def apply_reverse(self) -> None:
        """执行反向操作，撤销工具造成的影响"""
        ...


@dataclass
class NoOpUndo:
    """空撤销 — 用于只读工具（web_search/web_fetch/read_file/list_dir）"""
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
    # 语法验证配置
    enable_syntax_verify: bool = True  # 是否启用编辑后语法检查
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
    # 检测批量 read_file/grep 并注入"先概览再深入"的策略引导
    enable_progressive_gathering: bool = True
    # reclassify_interval: 每隔 N 轮迭代重新评估意图等级
    reclassify_interval: int = 5


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
    mode: str
    conversation_id: str
    workspace_path: str
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
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # LLM 配置（Bridle 注入）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 技能 ID 列表（前端显式选择），SkillRein 在 AGENT_START 时注入到 system_prompt
    skill_ids: list[str] = field(default_factory=list)
    # SSH 会话 ID（SSH 模式下由前端传入，供 shell_exec 等工具远程执行）
    ssh_session_id: str = ""
    # 目标环境标记（"test" | "prod"），驱动生产高风险命令的审计与审批强化
    environment: str = "test"

    @classmethod
    def new(cls, task: str, mode: str, **opts) -> "RunContext":
        """工厂方法 — 生成新 run_id"""
        return cls(
            run_id=uuid.uuid4().hex,
            task=task,
            mode=mode,
            conversation_id=opts.get("conversation_id", ""),
            workspace_path=opts.get("workspace_path", ""),
            ssh_session_id=opts.get("ssh_session_id", ""),
            environment=opts.get("environment", "test"),
            system_prompt=opts.get("system_prompt", ""),
            history=opts.get("history", []),
            memories=opts.get("memories", []),
            config=opts.get("config", AgentConfig()),
            skill_ids=opts.get("skill_ids", []),
        )

    async def transition(self, new_state: State, event: str = "") -> None:
        """状态转移 — 记录历史"""
        old = self.state
        self.state = new_state
        self.state_history.append({"from": old.value, "event": event, "to": new_state.value})

    def push_undo(self, action: UndoAction) -> None:
        self.undo_stack.append(action)

    async def pop_undo(self) -> Optional[UndoAction]:
        if not self.undo_stack:
            return None
        return self.undo_stack.pop()


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
    ) -> LLMOutput:
        ...


# ──────────────────────────────────────────────────────────────────────────────
# 工具注册表协议
# ──────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class ToolRegistry(Protocol):
    """工具注册表协议 — Saddle 通过它执行工具"""

    def schemas(self) -> list[dict[str, Any]]:
        """返回 OpenAI function calling 格式的工具 schema 列表"""
        ...

    async def execute(self, name: str, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        """执行工具"""
        ...
