# -*- coding: utf-8 -*-
"""S1-04 回归 — Sandbox 这条「不可禁用」底线看的命令入口必须与全仓唯一清单一致。

Sandbox 的 docstring 自称与 FilesystemGuard 不同：后者是 Reins 策略层、链可被装配方
裁剪，Sandbox 是 Saddle 内置的不可禁用底线。可原实现把命令入口硬写成
`("shell_exec",)`、取命令只读 `command`/`shell` 两个键，而
`reins/core.py:_COMMAND_EXEC_TOOLS` 明写「job_run 同属命令入口」、
`command_from_args` 的口径是 command/shell/cmd 三键 ——
于是 `job_run(action=…, cmd="rm -rf /")` 在这层完全没有内容级拦截，
唯一屏障恰好是它声称不可依赖的那一层。
"""
import pytest

from agent.harness.contracts import ToolCall
from agent.harness.reins.core import _COMMAND_EXEC_TOOLS, command_from_args
from agent.harness.saddle import Sandbox

HARDLINE = "rm -rf /"
SAFE = "uptime"
# command_from_args 认的三个键
CMD_KEYS = ("command", "shell", "cmd")


class _Ctx:
    """validate 不读 ctx；用显式空壳而不是 None，将来它真依赖 ctx 时测试会红。"""


@pytest.mark.parametrize("tool", sorted(_COMMAND_EXEC_TOOLS))
@pytest.mark.parametrize("key", CMD_KEYS)
def test_sandbox_blocks_hardline_for_every_command_entry_tool_and_key(tool, key):
    """底线必须覆盖 _COMMAND_EXEC_TOOLS 里**每个**工具的**每个**命令键 —— 逐键参数化，
    漏一个组合就红一条，不再靠注释提醒。"""
    call = ToolCall(id="c1", name=tool, args={key: HARDLINE})
    res = Sandbox().validate(call, _Ctx())
    assert not res.ok, f"{tool} 经 {key} 提交 {HARDLINE!r} 竟被放行"
    assert "不可逆" in res.reason


@pytest.mark.parametrize("tool", sorted(_COMMAND_EXEC_TOOLS))
def test_sandbox_still_passes_safe_commands(tool):
    res = Sandbox().validate(ToolCall(id="c1", name=tool, args={"cmd": SAFE}), _Ctx())
    assert res.ok


def test_sandbox_uses_the_canonical_extractor_not_its_own_copy():
    """键清单的单一来源：Sandbox 不许再自己抄一份 args 取值顺序。"""
    # command_from_args 对非 dict 安全返回空串，Sandbox 走同一条路而不是另写判据
    res = Sandbox().validate(ToolCall(id="c1", name="shell_exec", args=None), _Ctx())
    assert res.ok
    assert command_from_args(None) == ""


def test_dangerous_still_deferred_to_guard_and_gate():
    """本层只管 hardline；dangerous 的归属没变（口径见 validate 内注释）。"""
    res = Sandbox().validate(
        ToolCall(id="c1", name="job_run", args={"cmd": "systemctl restart nginx"}), _Ctx())
    assert res.ok


def test_non_command_tools_are_not_content_checked_here():
    """file_write 这类自拼命令的工具由 extract_bound_command 负责，不在这条路上。"""
    res = Sandbox().validate(
        ToolCall(id="c1", name="file_write", args={"command": HARDLINE}), _Ctx())
    assert res.ok
