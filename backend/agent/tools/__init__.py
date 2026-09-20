"""
工具注册中心 — BaseTool / ToolResult / ToolRegistry

所有工具继承 BaseTool，通过 ToolRegistry 注册。
ToolRegistry 生成 OpenAI function calling 格式的 tools 参数，
并负责工具执行（含权限检查由 Hook 层拦截）。
"""
from dataclasses import dataclass, field
from typing import Optional
import logging
import threading

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    output: str
    error: str = ""
    priority: str = "normal"  # 优先级：high（错误/关键）/ normal（常规）/ low（噪声）
    # 可见终端模式：非空时表示命令需交付到可见终端（前端写入 PTY/SSH）后由驱动挂起等待，
    # 结构: {"tool_id": str, "command": str, "session_id": str, "requires_confirm": bool}
    pending_delivery: Optional[dict] = None


@dataclass
class ToolMeta:
    """工具元数据 — 用于权限判断和成本控制"""
    risk_level: str = "safe"        # safe / moderate / dangerous
    timeout: float = 30.0           # 默认超时（秒）
    cacheable: bool = False         # 结果是否可缓存
    reversible: bool = True         # 操作是否可逆
    # 审查 MINOR：同一调用重复执行是否产生相同结果（只读/覆盖写）。
    # 仅 idempotent 工具允许 TRANSIENT 错误自动重试 — 非幂等工具
    # （write/shell/delete/move/spawn_subagent）重试会放大副作用
    # （命令双跑、写入两次、子 Agent 双启动）。默认 False（安全方向）。
    idempotent: bool = False
    mode_scope: list[str] = field(default_factory=lambda: ["ssh"])
    # 问题 5：per-action 风险覆盖。当调用 args 中指定了特定 action 时，
    # PermissionGate 在 prod 环境按此处声明的风险等级处理（默认按 risk_level）。
    # 例如 service_manage 工具 risk_level=moderate，但 stop/restart 在 prod
    # 环境应视为 dangerous（需每次授权），而非 session 级缓存。
    destructive_actions: frozenset[str] = field(default_factory=frozenset)


class BaseTool:
    """所有工具的基类"""
    name: str = ""
    description: str = ""
    parameters: dict | None = None
    meta: ToolMeta | None = None

    def __init__(self):
        # 深拷贝类级 dict/meta，避免子类类级赋值的 dict 在实例间共享
        # （子类 class ReadFileTool(BaseTool): parameters = {...} 所有实例拿到同一对象）
        import copy
        if self.parameters is not None and type(self) is not BaseTool:
            # 只有子类实例才深拷贝——基类属性保持 None 不变
            self.parameters = copy.deepcopy(self.parameters)
        if self.parameters is None:
            self.parameters = {}
        if self.meta is not None and type(self) is not BaseTool:
            self.meta = copy.deepcopy(self.meta)
        if self.meta is None:
            self.meta = ToolMeta()

    async def execute(self, context: "ToolContext", **kwargs) -> ToolResult:
        """执行工具，子类必须实现"""
        raise NotImplementedError


@dataclass
class ToolContext:
    """工具执行上下文 — 传入工具的运行时信息"""
    ssh_session_id: str = ""        # SSH 会话 ID（SSH 模式）
    mode: str = "ssh"               # 当前模式
    conversation_id: str = ""
    agent_run_id: str = ""
    # 审查修复（P2 E6）：子 Agent / spawn_subagent 需要向主 Agent 的
    # CostTracker 上报 LLM token 用量（原实现子 Agent 调用的 token 完全丢失）。
    # None 表示无追踪器（如无头定时调度 run）
    cost_tracker: object | None = None
    # 可见终端模式：True 时 shell_exec 不 headless 执行，而是登记待交付到可见终端
    visible_terminal: bool = False
    # 目标环境标记（test/prod）— 生产高风险命令需确认
    environment: str = "test"


class ToolRegistry:
    """工具注册中心"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._lock = threading.RLock()  # 线程安全

    def register(self, tool: BaseTool) -> None:
        """注册工具"""
        if not tool.name:
            raise ValueError("工具必须定义 name 属性")
        with self._lock:
            self._tools[tool.name] = tool
        logger.info("注册工具: %s (risk=%s)", tool.name, tool.meta.risk_level)

    def unregister(self, name: str) -> None:
        """注销工具"""
        with self._lock:
            if name in self._tools:
                del self._tools[name]

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict]:
        """获取工具 schema 列表"""
        with self._lock:
            snapshot = list(self._tools.values())

        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
            }
            for tool in snapshot
        ]

    async def execute(self, name: str, args: dict, context: ToolContext) -> ToolResult:
        """执行工具调用"""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, output="", error=f"未知工具: {name}")

        try:
            result = await tool.execute(context=context, **args)
            return result
        except Exception as e:
            logger.exception("工具 %s 执行异常", name)
            return ToolResult(success=False, output="", error=str(e))


def create_registry_for_mode(mode: str) -> ToolRegistry:
    """根据模式创建工具注册中心，注册该模式可用的工具"""
    from agent.tools.builtin import register_builtin_tools

    registry = ToolRegistry()
    register_builtin_tools(registry, mode)
    return registry
