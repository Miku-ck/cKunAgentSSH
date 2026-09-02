"""
内置工具包 — 按模式注册工具

工具清单:
  - read_file      (ssh)  读取文件内容
  - write_file     (ssh)  写入文件内容
  - patch_file     (ssh)  精确字符串替换编辑文件
  - delete_file    (ssh)  删除文件
  - move_file      (ssh)  移动/重命名文件
  - list_dir       (ssh)  列出目录内容
  - grep           (ssh)  内容搜索（正则）
  - glob           (ssh)  递归文件查找
  - shell_exec     (ssh)  执行 shell 命令
  - web_search     (ssh)  联网搜索
  - web_fetch      (ssh)  抓取网页内容
  - todo_write     (ssh)  管理任务列表
  - load_skill     (ssh)  按需加载技能完整指令
  - spawn_subagent (ssh)  启动子 Agent 处理独立子任务
"""
import logging

from agent.tools import ToolRegistry, BaseTool

logger = logging.getLogger(__name__)

# 统一的工具类清单（消除 register_builtin_tools 与 get_all_tool_classes 的重复维护）
from agent.tools.builtin.file_ops import (
    ReadFileTool, WriteFileTool, ListDirTool,
    DeleteFileTool, MoveFileTool,
)
from agent.tools.builtin.patch_file import PatchFileTool
from agent.tools.builtin.grep_tool import GrepTool
from agent.tools.builtin.glob_tool import GlobTool
from agent.tools.builtin.shell_exec import ShellExecTool
from agent.tools.builtin.web_tools import WebSearchTool, WebFetchTool
from agent.tools.builtin.todo_write import TodoWriteTool
from agent.tools.builtin.load_skill import LoadSkillTool
from agent.tools.builtin.spawn_subagent import SpawnSubagentTool

_ALL_TOOL_CLASSES: list[type[BaseTool]] = [
    ReadFileTool,
    WriteFileTool,
    PatchFileTool,
    DeleteFileTool,
    MoveFileTool,
    ListDirTool,
    GrepTool,
    GlobTool,
    ShellExecTool,
    WebSearchTool,
    WebFetchTool,
    TodoWriteTool,
    LoadSkillTool,
    SpawnSubagentTool,
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
