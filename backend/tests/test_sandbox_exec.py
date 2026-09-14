"""sandbox_exec 能力限制层单元测试（P2 沙箱落地）。

覆盖：
  - detect_backend 探测顺序（docker 优先于 podman）
  - 各后端 build_isolated_argv 关键隔离标志（只读根 / 禁网 / 资源上限 / 丢权能 / 降权）
  - run_sandboxed：无后端 + require_sandbox → fail-closed 抛 SandboxUnavailable
  - run_sandboxed：有后端 → 经隔离命令 exec
  - run_sandboxed：无后端 + require_sandbox=False → 放行裸跑（开发逃生）
"""
import asyncio

import pytest

from agent.harness import sandbox_exec
from agent.harness.sandbox_exec import (
    SandboxLimits,
    SandboxUnavailable,
    build_isolated_argv,
    detect_backend,
    run_sandboxed,
)


def test_detect_backend_order(monkeypatch):
    """docker 应优先于 podman 被选中。"""
    order = ["podman", "docker", "bwrap", "systemd-run", "wsl"]

    def fake_which(name):
        return f"/usr/bin/{name}" if name in order else None

    monkeypatch.setattr(sandbox_exec.shutil, "which", fake_which)
    assert detect_backend() == "docker"


def test_build_docker():
    argv = build_isolated_argv("docker", ["echo", "hi"], "/w", SandboxLimits())
    assert argv[0] == "docker"
    assert "run" in argv
    assert argv[argv.index("--network") + 1] == "none"      # 禁网
    assert "256m" in argv                                     # 内存上限
    assert "--pids-limit" in argv                            # 进程数上限
    assert "--read-only" in argv                             # 只读根
    assert "/w:/w" in argv                                   # workdir 可写挂载
    assert argv[argv.index("--cap-drop") + 1] == "ALL"      # 丢权能
    assert argv[argv.index("--user") + 1] == "1000"         # 降权
    assert "alpine:latest" in argv                          # 镜像
    assert argv[-2:] == ["echo", "hi"]


def test_build_bwrap():
    argv = build_isolated_argv("bwrap", ["echo", "hi"], "/w", SandboxLimits())
    assert argv[0] == "bwrap"
    assert "--ro-bind" in argv and "/" in argv              # 只读根绑定
    assert "/w" in argv                                     # workdir 可写绑定
    assert "--unshare-net" in argv                         # 禁网
    assert argv[argv.index("--cap-drop") + 1] == "ALL"     # 丢权能
    assert argv[-2:] == ["echo", "hi"]


def test_build_systemd():
    argv = build_isolated_argv("systemd-run", ["echo", "hi"], None, SandboxLimits())
    assert argv[0] == "systemd-run"
    assert "--scope" in argv
    assert any("MemoryMax=256M" in a for a in argv)
    assert any("TasksMax=64" in a for a in argv)
    assert any("CPUQuota=100%" in a for a in argv)


def test_build_wsl():
    argv = build_isolated_argv("wsl", ["echo", "hi"], None, SandboxLimits())
    assert argv[:2] == ["wsl", "--"]
    assert argv[2:4] == ["bash", "-c"]
    assert "echo hi" in argv[4]                            # 命令拼成单串


class _FakeProc:
    stdout = None
    stderr = None
    returncode = 0

    def wait(self):
        return asyncio.sleep(0)

    def kill(self):
        pass


async def _make_proc():
    return _FakeProc()


@pytest.mark.asyncio
async def test_run_sandboxed_fail_closed(monkeypatch):
    """无后端 + require_sandbox → fail-closed 抛 SandboxUnavailable。"""
    monkeypatch.setattr(sandbox_exec, "detect_backend", lambda: None)
    with pytest.raises(SandboxUnavailable):
        await run_sandboxed(["echo", "hi"], require_sandbox=True)


@pytest.mark.asyncio
async def test_run_sandboxed_backend_called(monkeypatch):
    """有后端 → 经隔离命令 exec（argv 以后端名开头）。"""
    monkeypatch.setattr(sandbox_exec, "detect_backend", lambda: "bwrap")
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return await _make_proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    proc = await run_sandboxed(["echo", "hi"], workdir="/w", limits=SandboxLimits())
    assert isinstance(proc, _FakeProc)
    assert calls and calls[0][0] == "bwrap"


@pytest.mark.asyncio
async def test_run_sandboxed_fallback_allowed(monkeypatch):
    """无后端 + require_sandbox=False → 放行裸跑（开发逃生，argv 不被包裹）。"""
    monkeypatch.setattr(sandbox_exec, "detect_backend", lambda: None)
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return await _make_proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    await run_sandboxed(["echo", "hi"], require_sandbox=False)
    assert calls and calls[0][0] == "echo"
