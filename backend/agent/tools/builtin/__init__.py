"""
内置工具包 — 运维 Agent 工具集

工具清单（ssh 运维模式；文件操作一律经 shell_exec 远程执行，
本地文件工具已随 work/ide 模式移除）:
  - shell_exec     执行 shell 命令（ssh 模式远程执行）
  - web_search     联网搜索
  - web_fetch      抓取网页内容
  - todo_write     管理任务列表
  - load_skill     按需加载技能完整指令
  - spawn_subagent 启动子 Agent 处理独立子任务
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
]


def register_builtin_tools(registry: ToolRegistry, mode: str) -> None:
    """根据模式注册可用的内置工具

    遍历显式 _ALL_TOOL_CLASSES，按 ToolMeta.mode_scope 动态过滤 + DB 开关过滤。
    """
    for tool_cls in _ALL_TOOL_CLASSES:
        if mode in tool_cls.meta.mode_scope:
            registry.register(tool_cls())

    # 按 DB 配置移除被禁用的工具
    try:
        import database as db
        disabled_list = db.list_disabled_tools(mode)
        for name in disabled_list:
            if registry.get(name):
                registry.unregister(name)
    except Exception:
        # DB 未初始化时降级为不过滤（保持原有行为）
        pass

    logger.info("模式 %s 注册了 %d 个工具: %s", mode, len(registry.names()), registry.names())


def get_all_tool_classes(mode: str | None = None) -> list[type[BaseTool]]:
    """获取指定模式下所有工具类（不经过 DB 过滤，用于 UI 展示）

    返回工具类列表，调用方自行实例化读取 name/description/parameters。
    mode 为 None 时返回全部工具类。
    """
    if mode is None:
        return list(_ALL_TOOL_CLASSES)
    return [cls for cls in _ALL_TOOL_CLASSES if mode in cls.meta.mode_scope]
