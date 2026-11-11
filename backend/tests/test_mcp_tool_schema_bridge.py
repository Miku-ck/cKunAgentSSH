"""MCP 工具桥接与会话生命周期守卫：命名规范化 / 默认 dangerous /
只读确认才降 safe / 输出截断 / 冲突整批拒绝 / 失效会话 fail-closed。"""
import asyncio

from agent.plugins.manager import PluginManager, truncate_output
from agent.plugins.mcp_tool import McpTool, is_mcp_tool, normalize_tool_name
from agent.run_assembly import register_plugin_tools
from agent.tools import ToolRegistry


def _plugin():
    return {"id": "p1", "slug": "k8s", "config_rev": 2, "timeout_s": 15}


def _spec(name="list_pods"):
    return {
        "name": name, "description": "列出 Pod",
        "inputSchema": {"type": "object", "properties": {"ns": {"type": "string"}}},
        "readOnlyHint": True,
    }


class _FakeSession:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def list_tools(self):
        return type("R", (), {"tools": []})()

    async def call_tool(self, name, arguments=None):
        if self._error:
            raise self._error
        return self._result


def test_name_normalization_and_prefix():
    assert normalize_tool_name("k8s", "list.pods/v1") == "mcp__k8s__list_pods_v1"
    assert is_mcp_tool("mcp__k8s__list_pods")
    assert not is_mcp_tool("file_read")


def test_default_meta_is_dangerous_and_readonly_confirmation_lowers_risk():
    class _M:
        async def call(self, *a, **kw):
            return True, "ok", ""

    tool = McpTool(plugin=_plugin(), spec=_spec(), manager=_M())
    assert tool.meta.risk_level == "dangerous"
    assert tool.meta.reversible is False
    assert tool.meta.idempotent is False
    assert tool.meta.timeout == 15.0
    assert tool.parameters["properties"]["ns"]["type"] == "string"

    safe = McpTool(plugin=_plugin(), spec=_spec(), manager=_M(), readonly=True)
    assert safe.meta.risk_level == "safe"


def test_truncate_output_keeps_head_tail_with_visible_marker():
    text = "A" * 50000 + "M" * 30000 + "Z" * 50000
    out = truncate_output(text)
    assert out.startswith("A" * 100)
    assert out.endswith("Z" * 100)
    assert "已截断" in out
    assert len(out) < len(text)
    assert truncate_output("short") == "short"


def test_registry_conflict_rejects_whole_batch():
    registry = ToolRegistry()
    # 与已注册工具同名时不载入整批插件工具，绝不覆盖原有实现
    from agent.tools import BaseTool
    class _Builtin(BaseTool):
        name = "mcp__k8s__list_pods"
    registry.register(_Builtin())
    batch = [McpTool(plugin=_plugin(), spec=_spec("list_pods"), manager=object())]
    conflicts = register_plugin_tools(registry, batch)
    assert conflicts == ["mcp__k8s__list_pods"]
    assert registry.get("mcp__k8s__list_pods").__class__ is _Builtin


def test_call_without_session_fails_closed():
    manager = PluginManager()
    ok, out, err = asyncio.run(manager.call("p1", "list_pods", {}))
    assert not ok
    assert "会话不可用" in err


def test_call_maps_iserror_and_destroys_session_on_exception(base_db):
    manager = PluginManager()
    from contextlib import AsyncExitStack

    good = type("R", (), {
        "content": [type("C", (), {"type": "text", "text": "hello"})()],
        "isError": False,
    })()
    manager._sessions["p1"] = type("S", (), {
        "plugin_id": "p1", "config_rev": 1,
        "session": _FakeSession(result=good), "stack": AsyncExitStack(),
    })()
    ok, out, err = asyncio.run(manager.call("p1", "list_pods", {}))
    assert ok and out == "hello" and err == ""

    bad = type("R", (), {
        "content": [type("C", (), {"type": "text", "text": "boom"})()],
        "isError": True,
    })()
    manager._sessions["p1"].session = _FakeSession(result=bad)
    ok, out, err = asyncio.run(manager.call("p1", "list_pods", {}))
    assert not ok and "isError" in err

    manager._sessions["p1"].session = _FakeSession(error=RuntimeError("dead"))
    ok, out, err = asyncio.run(manager.call("p1", "list_pods", {}))
    assert not ok
    assert "p1" not in manager._sessions  # 失效会话被销毁，不复用
