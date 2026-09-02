# -*- coding: utf-8 -*-
"""C-review M14 回归：ssh 模式不注册本地文件工具，系统提示明确边界。

修复前：ToolMeta.mode_scope 默认 ["ssh"]，file_ops.py 全套本地 os.path 工具
在 ssh 模式注册 — 操作的是后端主机而非远程服务器（split brain），undo/backup
跟踪错误文件系统。
修复后：本地文件工具 mode_scope=["work","ide"]（仅本地模式），ssh 模式文件操作
一律走 shell_exec；ssh 系统提示明确说明该边界，效率指引按模式切换话术。

运行: python -m pytest backend/tests/test_ssh_mode_file_tools.py -v
"""
import pytest

_LOCAL_FILE_TOOLS = {
    "read_file", "write_file", "patch_file", "delete_file",
    "move_file", "list_dir", "grep", "glob",
}
_SSH_CORE_TOOLS = {
    "shell_exec", "web_search", "web_fetch", "todo_write", "load_skill",
    "spawn_subagent",
}


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """工具注册会查 DB 的禁用工具表 — 测试中屏蔽（只验证 mode_scope 过滤）"""
    import database
    monkeypatch.setattr(database, "list_disabled_tools", lambda mode: [])


def test_ssh_mode_has_no_local_file_tools():
    from agent.tools import create_registry_for_mode
    reg = create_registry_for_mode("ssh")
    names = set(reg.names())
    leaked = names & _LOCAL_FILE_TOOLS
    assert not leaked, f"ssh 模式不得注册本地文件工具: {leaked}"


def test_ssh_mode_keeps_core_tools():
    from agent.tools import create_registry_for_mode
    reg = create_registry_for_mode("ssh")
    names = set(reg.names())
    missing = _SSH_CORE_TOOLS - names
    assert not missing, f"ssh 模式核心工具缺失: {missing}"


def test_get_all_tool_classes_ssh_excludes_file_tools():
    from agent.tools.builtin import get_all_tool_classes
    names = {cls.name for cls in get_all_tool_classes("ssh")}
    assert not (names & _LOCAL_FILE_TOOLS)
    assert "shell_exec" in names


def test_file_tools_scoped_to_local_modes():
    from agent.tools.builtin import get_all_tool_classes
    # work/ide（本地模式）保留文件工具
    work_names = {cls.name for cls in get_all_tool_classes("work")}
    assert _LOCAL_FILE_TOOLS <= work_names, "本地模式应保留文件工具"


def test_ssh_system_prompt_states_file_tool_boundary():
    """ssh 系统提示必须明确：无本地文件工具、文件操作走 shell_exec"""
    from prompts import MODE_PROMPTS
    p = MODE_PROMPTS["ssh"]
    assert "远程服务器" in p
    assert "shell_exec" in p
    assert "read_file" in p  # 明确点名不可用的工具


def test_efficiency_hint_switches_by_mode():
    """效率指引：ssh 模式不得引用 read_file，应引导 shell_exec 范围读取"""
    from routes.agent import _build_agent_system_prompt
    from routes.agent import AgentRunRequest

    req = AgentRunRequest(task="检查 nginx 配置", conversation_id="conv-test", mode="ssh")
    prompt = _build_agent_system_prompt(req, reflection_injection="", is_conversation_task=False)
    assert "read_file 的 start_line" not in prompt, "ssh 模式不得引导使用不存在的 read_file"
    assert "sed -n" in prompt or "head -n" in prompt, "ssh 模式效率指引应给出 shell_exec 范围读取话术"
