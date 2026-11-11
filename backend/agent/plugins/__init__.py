"""MCP 插件子系统。

边界：插件提供**新的可执行能力**（新工具），与专家（只改配置）、技能（过程知识）
严格分开。所有插件工具都走同一套 Reins 治理链、Saddle 沙箱与审计链，
不存在绕过审批的第二条执行通路。

刻意不在包级 import 子模块：`main.py` 会在启动关键路径上 import
`routes.plugins`，若此处 eager import `mcp_tool`，会连带把 `agent.tools`
（整个执行栈）拉进启动阶段 —— 那是 test_startup_critical_path 钉住的回归。
子模块一律按需导入（`__getattr__` 或直接 `from agent.plugins.manager import ...`）。
"""
from typing import Any

_MANAGER_EXPORTS = frozenset({"PluginManager", "get_plugin_manager", "truncate_output"})
_TOOL_EXPORTS = frozenset({"MCP_TOOL_PREFIX", "McpTool", "is_mcp_tool", "normalize_tool_name"})


def __getattr__(name: str) -> Any:
    if name in _MANAGER_EXPORTS:
        from . import manager
        return getattr(manager, name)
    if name in _TOOL_EXPORTS:
        from . import mcp_tool
        return getattr(mcp_tool, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = sorted(_MANAGER_EXPORTS | _TOOL_EXPORTS)
