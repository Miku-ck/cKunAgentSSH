# -*- coding: utf-8 -*-
"""第 13d 轮回归：service_manage 的失败原因分类。

缺陷家族：「同一条错误文本既当失败原因、又当主机能力探针」——
原先只看输出里有没有 "not found"/"no such file"，于是
  - systemctl 好好的、只是 unit 不存在 → 被告知「这台机器没有 systemd」（误报，方向被带偏）
  - 真·非 systemd 主机（容器/无 dbus）→ 什么提示都没有（漏报）

运行: python -m pytest backend/tests/test_ops_service_round13d.py -v
"""
import asyncio

import pytest

from agent.tools import ToolContext
from agent.tools.builtin import ServiceManageTool
from agent.tools.builtin import ops_service

_NO_SYS = "目标未提供可用的 systemctl"
_NO_UNIT = "没有对应的 unit"


def _ctx() -> ToolContext:
    return ToolContext(ssh_session_id="", mode="ssh", environment="test",
                       visible_terminal=False)


async def _run(action: str, output: str, success: bool = False):
    async def fake_run(ctx, command, timeout):
        return success, output

    saved = ops_service.run_headless
    ops_service.run_headless = fake_run
    try:
        return await ServiceManageTool().execute(_ctx(), action=action, name="nginx")
    finally:
        ops_service.run_headless = saved


# ── 误报方向：unit 不存在 ≠ 没有 systemd ────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "Failed to restart nginx.service: Unit nginx.service not found.",
    "Failed to get unit file state for nginx.service: No such file or directory",
])
def test_missing_unit_does_not_claim_no_systemd(raw):
    res = asyncio.run(_run("restart", raw))
    assert res.success is False
    assert _NO_SYS not in res.output, "systemd 明明可用，不能说主机没有 systemd"
    assert _NO_UNIT in res.output
    assert raw in res.output, "systemctl 原文必须保留"


def test_unrelated_failure_gets_no_invented_diagnosis():
    """unit 在、服务起不来：不补任何「环境结论」，只把 systemctl 的失败原样交回。"""
    raw = "Job for nginx.service failed because the control process exited with error code."
    res = asyncio.run(_run("start", raw))
    assert res.success is False
    assert _NO_SYS not in res.output and _NO_UNIT not in res.output
    assert res.output == raw


# ── 漏报方向：真·非 systemd 主机要认出来 ───────────────────────────────────
@pytest.mark.parametrize("raw", [
    "sh: 1: systemctl: not found",
    "bash: systemctl: command not found",
    "System has not been booted with systemd as init system (PID 1). Can't operate.",
    "Failed to connect to bus: Host is down",
    "Failed to get a connection to the bus: Connection refused",
    "Running in chroot, ignoring request.",
])
def test_systemctl_unavailable_is_detected(raw):
    res = asyncio.run(_run("status", raw))
    assert res.success is False
    assert _NO_SYS in res.output
    assert _NO_UNIT not in res.output


# ── 标记与文案时序 ─────────────────────────────────────────────────────────
def test_destructive_failure_keeps_single_marker_and_prefix():
    """修复前非破坏分支会拼出 "[!] [!] 目标未提供…" 双前缀；破坏分支标记只出现一次。"""
    raw = "Failed to stop nginx.service: Unit nginx.service not found."
    res = asyncio.run(_run("stop", raw))
    assert res.output.count("[destructive=true") == 1
    assert res.output.count("[!]") == 1
    assert res.output.index("[destructive=true") < res.output.index(_NO_UNIT)


def test_success_path_unchanged():
    res = asyncio.run(_run("status", "● nginx.service - A high performance Web server", success=True))
    assert res.success is True
    assert _NO_SYS not in res.output and _NO_UNIT not in res.output


def test_case_insensitive_matching():
    """systemctl 的消息大小写不固定（No such file / NO SUCH FILE）。"""
    raw = "Failed to restart nginx.service: Unit nginx.service NOT FOUND."
    assert _NO_UNIT in asyncio.run(_run("restart", raw)).output


def test_command_echo_line_is_not_read_as_missing_executable():
    """有的执行器会把调用的命令行一起回显：这行里的 "systemctl" 不代表它不存在。"""
    raw = ("systemctl restart nginx.service\n"
           "Failed to restart nginx.service: Unit nginx.service not found.")
    res = asyncio.run(_run("restart", raw))
    assert _NO_SYS not in res.output
    assert _NO_UNIT in res.output
