"""记忆范围透传守卫：空集 = 明确不读记忆，不能被 `or` 退回默认；
Saddle 把 ctx.metadata 的专家 scope 原样交给 ToolContext。"""
from agent.harness.contracts import RunContext
from agent.harness.saddle import Saddle
from agent.tools import ToolContext
from agent.tools.builtin.memory_tools import _host_scopes


def test_empty_scope_disables_memory_read():
    assert _host_scopes(ToolContext(memory_scopes=[])) == []


def test_explicit_scope_is_used_verbatim():
    assert _host_scopes(ToolContext(memory_scopes=["global"])) == ["global"]
    assert _host_scopes(ToolContext(
        ssh_session_id="sess-1", memory_scopes=["global", "host:sess-1"],
    )) == ["global", "host:sess-1"]


def test_none_scope_falls_back_to_legacy_context_scopes():
    ctx = ToolContext(ssh_session_id="sess-1")
    assert ctx.memory_scopes is None
    scopes = _host_scopes(ctx)
    assert isinstance(scopes, list) and scopes
    assert "global" in scopes


def test_saddle_passes_expert_scopes_to_tool_context():
    saddle = object.__new__(Saddle)
    run_ctx = RunContext.new(task="x")
    run_ctx.metadata["memory_scopes"] = []
    assert saddle._build_tool_context(run_ctx).memory_scopes == []

    run_ctx.metadata["memory_scopes"] = ["global"]
    assert saddle._build_tool_context(run_ctx).memory_scopes == ["global"]


def test_saddle_without_expert_leaves_scopes_none():
    saddle = object.__new__(Saddle)
    run_ctx = RunContext.new(task="x")
    assert saddle._build_tool_context(run_ctx).memory_scopes is None
