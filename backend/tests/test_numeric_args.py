"""模型填写的数字参数：统一解析 + 判定视图与执行命令逐字节一致。

三条口径（此前各写各的，本轮收敛到 ops_common 的 parse_int_arg / clamp_int）：
  - execute：非数字回一句「这个参数该怎么填」，而不是 ValueError 冒到驱动变成裸栈
  - bound_command（安全判定视图）：绝不抛，非法值折叠为默认值
  - 两边的区间钳制必须算出同一条命令串 —— 授权键 = sha256(命令)，一边把
    max_lines=0 折成 200、另一边钳成 1，预授权就永不命中

回归守护：把任一边改回各自的 try/except + int() 钳制，consistency 用例会失败。
"""
import types

import pytest

from agent.tools.builtin import ops_file, ops_obs
from agent.tools.builtin.ops_common import clamp_int, parse_int_arg
from agent.tools.builtin.ops_file import FileGrepTool, file_grep_cmd
from agent.tools.builtin.ops_obs import LogReadTool, NetworkProbeTool, SysOverviewTool, log_tail_cmd
from agent.tools.builtin.shell_exec import ShellExecTool

_CTX = types.SimpleNamespace(ssh_session_id="sess-1")


def _stub_headless(monkeypatch, module):
    """把模块的 run_headless 换成记录命令串的桩，返回 captured dict。"""
    captured: dict = {}

    async def fake_headless(context, command, timeout=30):
        captured["cmd"] = command
        return True, "matched-line\n[exit_code=0]"

    monkeypatch.setattr(module, "run_headless", fake_headless)
    return captured


# ── 解析器本身 ──────────────────────────────────────────────
@pytest.mark.parametrize("raw,expect", [
    (5, 5), ("5", 5), (" 5 ", 5), (5.7, 5), (None, None), ("", None), ("30s", None), ([1], None),
])
def test_parse_int_arg_accepts_only_int_like(raw, expect):
    value, err = parse_int_arg(raw, "limit")
    if expect is None:
        assert value is None and "limit" in err and repr(raw) in err
    else:
        assert value == expect and err == ""


def test_parse_int_arg_default_covers_missing_not_garbage():
    """缺参（None/空串）走默认值；给了填不动的值要报错，不能悄悄换成默认值蒙混。"""
    assert parse_int_arg(None, "top", 3) == (3, "")
    assert parse_int_arg("", "top", 3) == (3, "")
    value, err = parse_int_arg("top-3", "top", 3)
    assert value is None and err


@pytest.mark.parametrize("raw,expect", [
    (0, 1), (-7, 1), (9999, 200), ("abc", 100), (None, 100), ("150", 150),
])
def test_clamp_int_never_raises(raw, expect):
    assert clamp_int(raw, 100, 1, 200) == expect


# ── file_grep：判定视图 ⇄ 执行命令一致 ──────────────────────
@pytest.mark.parametrize("max_lines", [1, 0, -5, 200, 5000, 99999, "3000"])
@pytest.mark.asyncio
async def test_file_grep_bound_command_matches_executed_command(max_lines, monkeypatch):
    args = {"path": "/var/log", "pattern": "denied", "max_lines": max_lines}
    view = FileGrepTool.bound_command(args)
    captured = _stub_headless(monkeypatch, ops_file)
    result = await FileGrepTool().execute(_CTX, **args)
    assert result.success is True, result.error
    clamped = min(5000, max(1, int(max_lines)))
    assert view == captured["cmd"] == file_grep_cmd("/var/log", "denied", clamped)


@pytest.mark.asyncio
async def test_file_grep_bound_command_folds_garbage_but_execute_reports(monkeypatch):
    """同一脏值：判定视图给默认 200（绝不抛），执行侧明确报错 —— 两边都不炸。"""
    args = {"path": "/var/log", "pattern": "denied", "max_lines": "200行"}
    assert FileGrepTool.bound_command(args) == file_grep_cmd("/var/log", "denied", 200)
    _stub_headless(monkeypatch, ops_file)
    result = await FileGrepTool().execute(_CTX, **args)
    assert result.success is False and "max_lines" in result.error


# ── log_read：同上 ─────────────────────────────────────────
@pytest.mark.parametrize("lines", [0, 100, 1001, "50"])
@pytest.mark.asyncio
async def test_log_read_bound_command_matches_executed_command(lines, monkeypatch):
    args = {"path": "/var/log/messages", "lines": lines, "pattern": ""}
    view = LogReadTool.bound_command(args)
    captured = _stub_headless(monkeypatch, ops_obs)
    result = await LogReadTool().execute(_CTX, **args)
    assert result.success is True, result.error
    clamped = max(1, min(int(lines), 1000))
    assert view == captured["cmd"] == log_tail_cmd("/var/log/messages", clamped)


@pytest.mark.asyncio
async def test_log_read_garbage_lines_reports_without_executing(monkeypatch):
    captured = _stub_headless(monkeypatch, ops_obs)
    result = await LogReadTool().execute(_CTX, "/var/log/messages", "all", "")
    assert result.success is False and "lines" in result.error
    assert "cmd" not in captured  # 校验发生在下发之前


# ── 其余数字槽位：脏值回提示而不是异常 ─────────────────────
@pytest.mark.asyncio
async def test_network_probe_rejects_non_numeric_port():
    result = await NetworkProbeTool().execute(_CTX, "10.0.0.1", "8080/tcp", 5)
    assert result.success is False and "port" in result.error


@pytest.mark.asyncio
async def test_sys_overview_missing_top_uses_default(monkeypatch):
    captured = _stub_headless(monkeypatch, ops_obs)
    result = await SysOverviewTool().execute(_CTX, None)
    assert result.success is True, result.error
    assert "head -n 4" in captured["cmd"]  # top=3 → df -h | head -n (top+1)


@pytest.mark.asyncio
async def test_shell_exec_bad_timeout_reports_before_dispatch():
    """非数字 timeout 的报错发生在分支之前：既不本机执行，也不带着脏值下发。"""
    ctx = types.SimpleNamespace(ssh_session_id="", visible_terminal=False)
    result = await ShellExecTool().execute(ctx, "df -h", "30s")
    assert result.success is False
    assert "timeout" in result.error
