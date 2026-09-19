"""本地执行沙箱 — 能力限制层（与 safety.py 意图过滤正交）。

设计定位
--------
`safety.py` 决定"命令看起来危不危险"（意图过滤）；本模块决定"执行者最多能碰到什么"
（能力限制）。二者正交：意图过滤可被脚本内容 / 环境变量 / 别名 / SSH 跳板绕过，
一旦绕过，爆炸半径 = 该账号全部权限；沙箱把半径压到
"一次性隔离执行 + 只读根 + 无网络 + 资源上限 + 降权 + 丢权能"。

这是原审计结论的 P2 落地：fail-closed（P0/P1）堵住"静默落到本机执行"，
本模块进一步把"本机执行"本身限制进最小权限容器/命名空间。

后端自动探测顺序（首个可用者胜出）
----------------------------------
    docker → podman → bwrap(bubblewrap) → systemd-run
- docker / podman：容器隔离，需部署镜像（SandboxLimits.image，默认 alpine:latest）。
- bwrap：Linux 命名空间隔离，无需镜像，直接复用宿主文件系统（只读绑定）。
- systemd-run：Linux 资源管控（内存/进程数/CPU），不隔离网络。
- wsl：**不自动选用**。WSL 会改变执行环境——宿主命令的路径/解释器/二进制
  在 WSL 内不可见（如 `python D:/...` → command not found, rc=127, 输出丢失），
  与 target=local"在宿主上执行"的语义冲突。_wsl_argv 保留供显式指定场景使用。

无后端时的策略
------------
    require_sandbox=True（默认）→ fail-closed 拒绝本地执行，返回 SandboxUnavailable。
    开发环境可设 CKUN_AGENT_ALLOW_UNSANDBOXED_LOCAL=1 放行（不安全，仅调试用）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


# 开发环境逃生开关：设置后允许"无沙箱后端时裸跑本机"（不安全，仅本地调试）
ALLOW_UNSANDBOXED_LOCAL = os.environ.get("CKUN_AGENT_ALLOW_UNSANDBOXED_LOCAL") == "1"


class SandboxUnavailable(Exception):
    """无可用沙箱后端且策略要求沙箱时抛出（fail-closed 信号）。"""


@dataclass
class SandboxLimits:
    """单次隔离执行的资源与能力约束。"""
    image: str = "alpine:latest"      # docker/podman 用：需含所需命令的镜像
    memory_mb: int = 256              # 内存上限
    pids: int = 64                    # 进程数上限
    cpus: float = 1.0                 # CPU 配额（核数）
    network: bool = False             # False → 禁用网络
    read_only_root: bool = True       # True → 根只读，仅 workdir 可写挂载
    as_root: bool = False             # False → 降权到 uid/gid 1000


# ── 各后端命令行构造 ──────────────────────────────────────────────────────────

def _docker_argv(bin_name: str, argv: List[str], workdir: Optional[str],
                 L: SandboxLimits) -> List[str]:
    cmd: List[str] = [bin_name, "run", "--rm"]
    cmd += ["--network", "none" if not L.network else "host"]
    cmd += ["-m", f"{L.memory_mb}m"]
    cmd += ["--pids-limit", str(L.pids)]
    cmd += ["--cpus", str(L.cpus)]
    if L.read_only_root:
        cmd += ["--read-only"]
        if workdir:
            cmd += ["-v", f"{workdir}:{workdir}"]
    cmd += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    cmd += ["--user", "0" if L.as_root else "1000"]
    cmd += [L.image]
    cmd += list(argv)
    return cmd


def _bwrap_argv(argv: List[str], workdir: Optional[str],
                L: SandboxLimits) -> List[str]:
    cmd: List[str] = ["bwrap"]
    if L.read_only_root:
        cmd += ["--ro-bind", "/", "/"]
        if workdir:
            cmd += ["--bind", workdir, workdir]
    else:
        cmd += ["--bind", "/", "/"]
    cmd += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    if not L.network:
        cmd += ["--unshare-net"]
    cmd += ["--unshare-pid", "--die-with-parent", "--cap-drop", "ALL"]
    if not L.as_root:
        cmd += ["--uid", "1000", "--gid", "1000"]
    cmd += ["--"]
    cmd += list(argv)
    return cmd


def _systemd_argv(argv: List[str], workdir: Optional[str],
                  L: SandboxLimits) -> List[str]:
    # 注意：systemd-run 不隔离网络，仅做资源管控
    cpu_pct = max(1, int(round(L.cpus * 100)))
    cmd: List[str] = [
        "systemd-run", "--scope",
        "-p", f"MemoryMax={L.memory_mb}M",
        "-p", f"TasksMax={L.pids}",
        "-p", f"CPUQuota={cpu_pct}%",
        "-p", "NoNewPrivileges=yes",
        "--",
    ]
    cmd += list(argv)
    return cmd


def _wsl_argv(argv: List[str], workdir: Optional[str],
              L: SandboxLimits) -> List[str]:
    # 仅提供独立 Linux 用户态，无资源/网络隔离（兜底后端）
    joined = " ".join(shlex.quote(a) for a in argv)
    return ["wsl", "--", "bash", "-c", joined]


def detect_backend() -> Optional[str]:
    """按探测顺序返回首个可用的沙箱后端可执行名；无则返回 None。

    wsl 不在自动探测列表：它在 Windows 上会把宿主命令劫持进 WSL Linux 环境，
    宿主路径/解释器/二进制在该环境中不可见（实测 `python D:/...` rc=127、
    stdout 为空），与本地执行"在宿主上跑"的语义直接冲突。
    显式需要 WSL 隔离时可直接调用 build_isolated_argv("wsl", ...)。
    """
    for cand in ("docker", "podman", "bwrap", "systemd-run"):
        if shutil.which(cand):
            return cand
    return None


def build_isolated_argv(backend: str, argv: List[str], workdir: Optional[str],
                        L: SandboxLimits) -> List[str]:
    """为给定后端构造隔离执行命令行（argv 列表）。"""
    if backend in ("docker", "podman"):
        return _docker_argv(backend, argv, workdir, L)
    if backend == "bwrap":
        return _bwrap_argv(argv, workdir, L)
    if backend == "systemd-run":
        return _systemd_argv(argv, workdir, L)
    if backend == "wsl":
        return _wsl_argv(argv, workdir, L)
    raise SandboxUnavailable(f"未知沙箱后端: {backend}")


async def run_sandboxed(
    argv: List[str],
    workdir: Optional[str] = None,
    limits: Optional[SandboxLimits] = None,
    require_sandbox: bool = True,
) -> "asyncio.subprocess.Process":
    """在沙箱中隔离执行 argv，返回 asyncio 子进程对象。

    - 探测到后端 → 构造隔离命令并 exec。
    - 无后端：
        require_sandbox=True 且未开逃生开关 → 抛 SandboxUnavailable（fail-closed）。
        require_sandbox=False 或已开逃生开关 → 裸跑 argv 并记警告（不安全）。
    """
    L = limits or SandboxLimits()
    backend = detect_backend()
    if backend is None:
        if require_sandbox and not ALLOW_UNSANDBOXED_LOCAL:
            raise SandboxUnavailable(
                "未检测到沙箱后端（docker/podman/bwrap/systemd-run），"
                "本地执行被 fail-closed 拒绝。请安装容器/命名空间运行时；"
                "开发环境可设 CKUN_AGENT_ALLOW_UNSANDBOXED_LOCAL=1 放行（不安全）。"
            )
        logger.warning("沙箱后端不可用，放行未隔离本机执行（不安全）：%s",
                       " ".join(argv))
        wrapped = list(argv)
    else:
        wrapped = build_isolated_argv(backend, argv, workdir, L)
        logger.info("本地执行经沙箱后端 %s 隔离：%s", backend, " ".join(wrapped))

    proc = await asyncio.create_subprocess_exec(
        *wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
    )
    return proc
