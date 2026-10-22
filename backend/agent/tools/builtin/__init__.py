"""
内置工具包 — 运维 Agent 工具集

完整工具类清单以下方 _ALL_TOOL_CLASSES 为单一事实源（work/ide 模式已移除，
现存工具全部注册，不再按 mode_scope 过滤），此处不再另抄一份易漂移的枚举。
概览分组：shell 执行 / 联网 / 待办 / 技能按需加载 /
子 Agent，运维观测与服务·定时·后台任务，远程文件读写·编辑·检索，图片查看，
记忆自编辑，以及 Spec/Plan/Goal 工作流落盘。
"""
import logging

from agent.tools import ToolRegistry, BaseTool

logger = logging.getLogger(__name__)

# 统一的工具类清单（消除 register_builtin_tools 与 get_all_tool_classes 的重复维护）
from agent.tools.builtin.shell_exec import ShellExecTool
from agent.tools.builtin.web_tools import WebSearchTool, WebFetchTool
from agent.tools.builtin.todo_write import TodoWriteTool
from agent.tools.builtin.load_skill import LoadSkillTool
from agent.tools.builtin.spawn_subagent import SpawnSubagentTool
# 运维智能工具（只读观测 / 日志 / 服务生命周期 / 计划定时）
from agent.tools.builtin.ops_obs import (
    SysOverviewTool,
    ProcessInspectTool,
    NetworkProbeTool,
    LogReadTool,
)
from agent.tools.builtin.ops_service import ServiceManageTool
from agent.tools.builtin.ops_schedule import ScheduleOpTool
# 运维文件操作（远程/本地双模式）、图片查看、后台任务
from agent.tools.builtin.ops_file import (
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    FileGlobTool,
    FileGrepTool,
)
from agent.tools.builtin.ops_image import ReadImageTool
from agent.tools.builtin.ops_job import (
    JobRunTool,
    JobListTool,
    JobOutputTool,
    JobKillTool,
)
# 记忆自编辑（检索 / 登记 / 作废 / 画像块更新）
from agent.tools.builtin.memory_tools import (
    MemorySearchTool,
    MemoryAddTool,
    MemoryInvalidateTool,
    BlockUpdateTool,
)
# Runbook 知识库（排障经验沉淀 / 检索，复用 memories 表 kind='runbook'）
from agent.tools.builtin.runbook_tools import RunbookSaveTool, RunbookSearchTool
# Spec/Plan/Goal 三阶段工作流（文档落盘，非 runbook）
from agent.tools.builtin.workflow_tools import (
    SpecWriteTool,
    PlanWriteTool,
    GoalWriteTool,
)

_ALL_TOOL_CLASSES: list[type[BaseTool]] = [
    ShellExecTool,
    WebSearchTool,
    WebFetchTool,
    TodoWriteTool,
    LoadSkillTool,
    SpawnSubagentTool,
    # 运维智能
    SysOverviewTool,
    ProcessInspectTool,
    NetworkProbeTool,
    LogReadTool,
    ServiceManageTool,
    ScheduleOpTool,
    # 运维文件操作 / 图片查看 / 后台任务
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    FileGlobTool,
    FileGrepTool,
    ReadImageTool,
    JobRunTool,
    JobListTool,
    JobOutputTool,
    JobKillTool,
    # 记忆自编辑
    MemorySearchTool,
    MemoryAddTool,
    MemoryInvalidateTool,
    BlockUpdateTool,
    # Runbook 知识库（经验沉淀 / 检索）
    RunbookSaveTool,
    RunbookSearchTool,
    # Spec/Plan/Goal 三阶段工作流
    SpecWriteTool,
    PlanWriteTool,
    GoalWriteTool,
]


def register_builtin_tools(registry: ToolRegistry) -> None:
    """注册全部内置工具，再按 DB 开关移除被禁用的工具"""
    for tool_cls in _ALL_TOOL_CLASSES:
        registry.register(tool_cls())

    # 按 DB 配置移除被禁用的工具（mode='all' 全局禁用 + SSH 模式禁用一并生效）
    try:
        import database as db
        from prompts import SSH_MODE
        disabled_list = db.list_disabled_tools(SSH_MODE)
        for name in disabled_list:
            if registry.get(name):
                registry.unregister(name)
    except Exception:
        # DB 未初始化时降级为不过滤（保持原有行为）
        pass

    logger.info("注册了 %d 个工具: %s", len(registry.names()), registry.names())


def get_all_tool_classes() -> list[type[BaseTool]]:
    """获取所有工具类（不经过 DB 过滤，用于 UI 展示）

    返回工具类列表，调用方自行实例化读取 name/description/parameters。
    """
    return list(_ALL_TOOL_CLASSES)
