"""多会话只读扇出（fleet_overview / fleet_sessions）—— 并发、隔离、只读、清理。

本文件钉七件事：
  1. 多台并发采集：每台独立临时会话，对比表含全部主机与超阈值 ⚠ 标记；
  2. 单台失败不放大：一台建连失败只标 ❌，其余主机观测照常返回（success=True）；
  3. 全部失败才整体失败（success=False），失败信息如实上抛；
  4. fail-closed：桥不可用整批拒绝，绝不降级到后端本机执行；
  5. 临时会话清理：每台建连后必须断开（S0-06 归属机制下 disconnect 只能断
     自己建的，不碰用户终端）；
  6. 远端命令模板与 sys_overview 同源（白名单 RE_SYS_OVERVIEW 契约不破）；
  7. 参数防线：targets 空拒绝、未知 id 逐个标注、超上限截断并提示。

运行: python -m pytest backend/tests/test_fleet_overview.py -v
"""
import asyncio

import pytest

from agent.tools.builtin import ops_fleet
from agent.tools.builtin.ops_obs import sys_overview_cmd
from agent.tools.builtin.ops_fleet import FleetOverviewTool, FleetSessionsTool


def _ok_output(load="load average: 0.52, 0.44, 0.40", mem="Mem:          2000        1000        1000          0",
               disk=""):
    return (
        "[CPU 负载]\n"
        f" 12:00:01 up 3 days,  1 user,  {load}\n"
        "[内存]\n"
        f"{mem} [buffers]\n"
        "Swap: 0 0 0\n"
        "[磁盘 TOP 3]\n"
        "/dev/sda1        50G   30G   20G  60% /\n"
        f"{disk}\n"
        "[开机时长]\n2026-10-19 12:00:00\n"
        "[内核版本]\n5.15.0-91-generic"
    )


class _FakeBridge:
    """ElectronBridge 替身：可控的建连/执行/断开记录。"""

    def __init__(self, connections, fail_connect=(), exec_outputs=None, available=True):
        self.connections = connections
        self.fail_connect = set(fail_connect)
        self.exec_outputs = exec_outputs or {}
        self._available = available
        self.disconnected = []
        self.exec_commands = []          # (session_id, command)

    def is_available(self):
        return self._available

    async def list_connections(self):
        return list(self.connections)

    async def connect_ssh(self, connection_id, timeout=60.0):
        if connection_id in self.fail_connect:
            from agent.electron_bridge import BridgeError, BridgeErrorCode
            raise BridgeError(BridgeErrorCode.HTTP_ERROR, "连接超时")
        return {"success": True, "sessionId": f"s-{connection_id}",
                "name": connection_id}

    async def disconnect_ssh(self, session_id, timeout=15.0):
        self.disconnected.append(session_id)

    async def exec_ssh_command(self, ssh_session_id, command, timeout):
        self.exec_commands.append((ssh_session_id, command))
        output = self.exec_outputs.get(ssh_session_id, _ok_output())
        return {"success": True, "output": output, "error": "", "exit_code": 0}


_CONN = [
    {"id": "conn-a", "name": "web-1", "host": "10.0.0.1", "environment": "test"},
    {"id": "conn-b", "name": "db-1", "host": "10.0.0.2", "environment": "prod"},
]


@pytest.fixture
def patch_bridge(monkeypatch):
    def _install(bridge):
        monkeypatch.setattr(ops_fleet, "ElectronBridge", lambda: bridge)
        # exec_remote 在 _collect_one 内延迟导入 —— 补丁打在定义模块上；
        # 真实执行路径走这里（不经 bridge.exec_ssh_command），命令记录在此
        async def _fake_exec(session_id, command, timeout, keep_full=False):
            bridge.exec_commands.append((session_id, command))
            out = bridge.exec_outputs.get(session_id, _ok_output())
            return True, out, 0, ""
        monkeypatch.setattr("agent.tools.builtin.ops_common.exec_remote", _fake_exec)
        return bridge
    return _install


def _run(tool, ctx=None, **kwargs):
    return asyncio.run(tool.execute(None, **kwargs))


# ── 1. 并发采集 + 对比 + 阈值标记 ─────────────────────────────────────────

def test_多台并发采集且超阈值标记(patch_bridge):
    hot = _ok_output(load="load average: 5.10, 4.80, 4.20",
                     mem="Mem:          2000        1900        100          0")
    bridge = patch_bridge(_FakeBridge(
        _CONN, exec_outputs={"s-conn-a": _ok_output(), "s-conn-b": hot}))
    res = _run(FleetOverviewTool(), targets=["conn-a", "conn-b"])

    assert res.success is True
    assert "扇出对比（成功 2/2）" in res.output
    assert "web-1" in res.output and "db-1" in res.output
    # 超阈值标记：db-1 的 load 5.10 > 4 与内存 95% > 90
    assert "⚠" in res.output
    assert "超阈值主机" in res.output
    # 每台建了临时会话并全部断开
    assert sorted(bridge.disconnected) == ["s-conn-a", "s-conn-b"]


# ── 2. 单台失败不放大 ─────────────────────────────────────────────────────

def test_单台建连失败不放大整批(patch_bridge):
    bridge = patch_bridge(_FakeBridge(_CONN, fail_connect={"conn-b"}))
    res = _run(FleetOverviewTool(), targets=["conn-a", "conn-b"])

    assert res.success is True, "一台失败不应放大为整批失败"
    assert "扇出对比（成功 1/2）" in res.output
    assert "❌" in res.output and "db-1" in res.output
    # 成功那台的明细仍在
    assert "### web-1" in res.output
    # 失败那台的临时会话不存在 → 无需断开
    assert bridge.disconnected == ["s-conn-a"]


def test_全部失败才整体失败(patch_bridge):
    patch_bridge(_FakeBridge(_CONN, fail_connect={"conn-a", "conn-b"}))
    res = _run(FleetOverviewTool(), targets=["conn-a", "conn-b"])

    assert res.success is False
    assert "所有目标均采集失败" in res.error


# ── 3. fail-closed ────────────────────────────────────────────────────────

def test_桥不可用整批拒绝(patch_bridge):
    patch_bridge(_FakeBridge(_CONN, available=False))
    res = _run(FleetOverviewTool(), targets=["conn-a"])
    assert res.success is False
    assert "fail-closed" in res.error


def test_fleet_sessions桥不可用拒绝(patch_bridge):
    patch_bridge(_FakeBridge(_CONN, available=False))
    res = _run(FleetSessionsTool())
    assert res.success is False


# ── 4. 参数防线 ───────────────────────────────────────────────────────────

def test_targets为空拒绝():
    res = _run(FleetOverviewTool(), targets=[])
    assert res.success is False
    assert "targets 不能为空" in res.error


def test_未知id逐个标注(patch_bridge):
    patch_bridge(_FakeBridge(_CONN))
    res = _run(FleetOverviewTool(), targets=["conn-a", "conn-x"])

    assert res.success is True
    assert "conn-x" in res.output and "连接配置不存在" in res.output
    assert "扇出对比（成功 1/1）" in res.output


def test_超上限截断并提示(patch_bridge, monkeypatch):
    bridge = patch_bridge(_FakeBridge(_CONN))
    many = ["conn-a", "conn-b", "x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10"]
    res = _run(FleetOverviewTool(), targets=many)

    assert res.success is True
    assert "超出单批上限 8" in res.output
    # 截断后只对存在的 2 台发起采集（x3..x10 被截掉）
    assert len(bridge.exec_commands) == 2


# ── 5. 命令模板同源（白名单契约）──────────────────────────────────────────

def test_远端命令与sys_overview同源(patch_bridge):
    bridge = patch_bridge(_FakeBridge(_CONN))
    _run(FleetOverviewTool(), targets=["conn-a", "conn-b"], top=5)

    expected = sys_overview_cmd(5)
    assert all(cmd == expected for _, cmd in bridge.exec_commands), \
        "扇出命令必须复用 sys_overview_cmd 模板（远端白名单 RE_SYS_OVERVIEW 契约）"


# ── 6. fleet_sessions 列表 ────────────────────────────────────────────────

def test_fleet_sessions列出目标(patch_bridge):
    patch_bridge(_FakeBridge(_CONN))
    res = _run(FleetSessionsTool())

    assert res.success is True
    assert "conn-a" in res.output and "web-1" in res.output
    assert "prod" in res.output


def test_fleet_sessions空列表如实说明(patch_bridge):
    patch_bridge(_FakeBridge([]))
    res = _run(FleetSessionsTool())
    assert res.success is True
    assert "没有任何已配置的 SSH 连接" in res.output
