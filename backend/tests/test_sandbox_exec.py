"""sandbox_exec 能力限制层单元测试（P2 沙箱落地）。

覆盖：
  - detect_backend 探测顺序（docker 优先于 podman）
  - 各后端 build_isolated_argv 关键隔离标志（只读根 / 禁网 / 资源上限 / 丢权能 / 降权）
  - run_sandboxed：无后端 + require_sandbox → fail-closed 抛 SandboxUnavailable
  - run_sandboxed：有后端 → 经隔离命令 exec
  - run_sandboxed：无后端 + require_sandbox=False → 放行裸跑（开发逃生）
  - workdir 白名单：/ 与文件系统根、越界路径、不存在路径、白名单内路径各形态
    （Q-B2：模型可控 workdir 不得无约束 -v/--bind 进容器）
"""
import asyncio
import os
from pathlib import Path

import pytest

from agent.harness import sandbox_exec
from agent.harness.sandbox_exec import (
    SandboxLimits,
    SandboxUnavailable,
    SandboxWorkdirRejected,
    build_isolated_argv,
    detect_backend,
    run_sandboxed,
    validate_workdir,
    workdir_roots,
)


def test_detect_backend_order(monkeypatch):
    """docker 应优先于 podman 被选中。"""
    order = ["podman", "docker", "bwrap", "systemd-run", "wsl"]

    def fake_which(name):
        return f"/usr/bin/{name}" if name in order else None

    monkeypatch.setattr(sandbox_exec.shutil, "which", fake_which)
    assert detect_backend() == "docker"


def test_build_docker(tmp_workspace, monkeypatch):
    monkeypatch.setenv("CKUN_SANDBOX_WORKDIR_ROOTS", tmp_workspace)
    inner = Path(tmp_workspace) / "wd"
    inner.mkdir()
    argv = build_isolated_argv("docker", ["echo", "hi"], str(inner), SandboxLimits())
    assert argv[0] == "docker"
    assert "run" in argv
    assert argv[argv.index("--network") + 1] == "none"      # 禁网
    assert "256m" in argv                                     # 内存上限
    assert "--pids-limit" in argv                            # 进程数上限
    assert "--read-only" in argv                             # 只读根
    assert f"{inner}:{inner}" in argv                        # workdir 可写挂载
    assert argv[argv.index("--cap-drop") + 1] == "ALL"      # 丢权能
    assert argv[argv.index("--user") + 1] == "1000"         # 降权
    assert "alpine:latest" in argv                          # 镜像
    assert argv[-2:] == ["echo", "hi"]


def test_build_bwrap(tmp_workspace, monkeypatch):
    monkeypatch.setenv("CKUN_SANDBOX_WORKDIR_ROOTS", tmp_workspace)
    inner = Path(tmp_workspace) / "wd"
    inner.mkdir()
    argv = build_isolated_argv("bwrap", ["echo", "hi"], str(inner), SandboxLimits())
    assert argv[0] == "bwrap"
    assert "--ro-bind" in argv and "/" in argv              # 只读根绑定
    assert str(inner) in argv                               # workdir 可写绑定
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
async def test_run_sandboxed_backend_called(monkeypatch, tmp_workspace):
    """有后端 → 经隔离命令 exec（argv 以后端名开头）。"""
    monkeypatch.setattr(sandbox_exec, "detect_backend", lambda: "bwrap")
    monkeypatch.setenv("CKUN_SANDBOX_WORKDIR_ROOTS", tmp_workspace)
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return await _make_proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    proc = await run_sandboxed(
        ["echo", "hi"], workdir=tmp_workspace, limits=SandboxLimits())
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


# ── workdir 白名单（Q-B2：模型可控 workdir 不得任意 bind 进容器）────────────


@pytest.fixture
def whitelist_root(tmp_workspace, monkeypatch):
    """把白名单根钉到工作区临时目录，返回 (根 Path, 白名单内真实子目录)。"""
    monkeypatch.setenv("CKUN_SANDBOX_WORKDIR_ROOTS", tmp_workspace)
    inner = Path(tmp_workspace) / "wd"
    inner.mkdir()
    return Path(tmp_workspace), inner


def test_workdir_root_itself_allowed(whitelist_root):
    root, _ = whitelist_root
    # 白名单根自身等于可用范围的全集，允许作为 workdir（is_relative_to 含相等）
    assert validate_workdir(str(root)) == root.resolve()


def test_workdir_inside_whitelist_resolved(whitelist_root):
    root, inner = whitelist_root
    assert validate_workdir(str(inner)) == inner.resolve()
    # 相对路径按 cwd 解析后仍须落在根内；这里从白名单内目录出发
    old = os.getcwd()
    os.chdir(inner)
    try:
        assert validate_workdir(".").is_relative_to(root.resolve())
    finally:
        os.chdir(old)


def test_workdir_filesystem_root_rejected(whitelist_root):
    # cwd='/' 的实测产物是 `-v /:/` —— 文件系统根一律拒绝
    with pytest.raises(SandboxWorkdirRejected):
        validate_workdir("/")


def test_workdir_etc_rejected(whitelist_root):
    # cwd='/etc' 的实测产物是 `-v /etc:/etc` —— 白名单外的既有目录拒绝
    # （Windows 上 /etc 解析为当前盘符根下 etc，无论存在与否都不得通过）
    with pytest.raises(SandboxWorkdirRejected):
        validate_workdir("/etc")


def test_workdir_outside_whitelist_but_existing_rejected(whitelist_root, tmp_workspace):
    # 真实存在但不在白名单根内的目录：containment 拒绝（这是主判据，
    # 不存在的目录由存在性判据拒绝）
    _, inner = whitelist_root
    outside = Path(tmp_workspace).parent / f"outside-{inner.name}"
    outside.mkdir(exist_ok=True)
    try:
        with pytest.raises(SandboxWorkdirRejected):
            validate_workdir(str(outside))
    finally:
        outside.rmdir()


def test_workdir_nonexistent_rejected(whitelist_root):
    # docker 对不存在的 -v 源路径会以 root 自动新建 —— 存在性是硬条件
    _, inner = whitelist_root
    with pytest.raises(SandboxWorkdirRejected):
        validate_workdir(str(inner / "no-such-dir"))


def test_workdir_empty_and_symlink_escape_rejected(whitelist_root):
    with pytest.raises(SandboxWorkdirRejected):
        validate_workdir("")
    _, inner = whitelist_root
    if hasattr(os, "symlink"):
        try:
            link = inner / "to-outside"
            link.symlink_to(Path(inner.parent))
        except OSError:
            return  # Windows 无符号链接特权时跳过该形态
        try:
            with pytest.raises(SandboxWorkdirRejected):
                validate_workdir(str(link))
        finally:
            link.unlink(missing_ok=True)


def test_build_docker_rejects_unvalidated_workdir(whitelist_root):
    # 构造层同样拒绝：不做无约束的 -v {workdir}:{workdir}
    with pytest.raises(SandboxWorkdirRejected):
        build_isolated_argv("docker", ["echo"], "/", SandboxLimits())


@pytest.mark.asyncio
async def test_run_sandboxed_rejects_bad_workdir_before_exec(
        monkeypatch, whitelist_root):
    # 执行入口在任何后端（含裸跑分支）前先过白名单：cwd 本身也是特权
    monkeypatch.setattr(sandbox_exec, "detect_backend", lambda: None)
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return await _make_proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(SandboxWorkdirRejected):
        await run_sandboxed(["echo", "hi"], workdir="/etc", require_sandbox=False)
    assert not calls, "被拒绝的 workdir 不得触达子进程创建"


def test_workdir_roots_env_parsing(monkeypatch, tmp_workspace):
    # env 多根（os.pathsep 分隔）+ 空段过滤；未设 env 时缺省 = 进程 cwd
    monkeypatch.setenv(
        "CKUN_SANDBOX_WORKDIR_ROOTS",
        os.pathsep.join([tmp_workspace, "", str(tmp_workspace) + "-alt"]),
    )
    roots = workdir_roots()
    assert len(roots) == 2 and Path(tmp_workspace) in roots
    monkeypatch.delenv("CKUN_SANDBOX_WORKDIR_ROOTS")
    assert workdir_roots() == [Path.cwd()]
