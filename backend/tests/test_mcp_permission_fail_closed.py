"""插件权限 fail-closed：计划模式全拒 / 自动审批不代判 /
永久授权在请求模型层被拒 / 插件工具不进入只读白名单。"""
import asyncio

import pytest
from pydantic import ValidationError

from agent.harness.contracts import RunContext
from agent.harness.reins.auto_approve import try_auto_approve
from agent.harness.reins.core import MODE_KEY
from agent.harness.reins.plan_mode import (
    PLAN_READ_ONLY_TOOLS, classify_plan_action,
)
from routes.agent import AllowPermissionRequest, PermissionGrantRequest

MCP_TOOL = "mcp__k8s__restart_deploy"


def test_plan_mode_rejects_all_mcp_tools():
    allowed, reason = classify_plan_action(MCP_TOOL, {}, None)
    assert not allowed
    assert "计划模式" in reason
    assert not any(t.startswith("mcp__") for t in PLAN_READ_ONLY_TOOLS)


def test_auto_approve_never_judges_mcp_tools():
    ctx = RunContext.new(task="x")
    ctx.metadata[MODE_KEY] = "auto"
    ctx.llm_base_url = "http://example.invalid"  # 即便模型可用也不该调用裁决器
    decision = asyncio.run(try_auto_approve(ctx, MCP_TOOL, {}, "dangerous"))
    assert decision is None


def test_permanent_grant_for_mcp_is_rejected():
    with pytest.raises(ValidationError):
        PermissionGrantRequest(tool=MCP_TOOL, scope="always")
    # once / session 仍然允许
    assert PermissionGrantRequest(tool=MCP_TOOL, scope="once").scope == "once"
    assert PermissionGrantRequest(tool=MCP_TOOL, scope="session").scope == "session"


def test_allowlist_endpoint_rejects_mcp_tools():
    with pytest.raises(ValidationError):
        AllowPermissionRequest(tool=MCP_TOOL)
    assert AllowPermissionRequest(tool="file_read").tool == "file_read"


def test_long_mcp_tool_name_fits_permission_request():
    name = "mcp__" + "s" * 40 + "__" + "t" * 40
    assert PermissionGrantRequest(tool=name, scope="once").tool == name
