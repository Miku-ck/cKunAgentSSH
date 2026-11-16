"""运维专家 MCP 插件（stdio）—— 只读诊断工具集。

供 cKunAgentSSH 的「运维专家（general_ops）」挂载的工具供给插件。
设计原则：
- 只读诊断，不做任何变更类操作（改配置/重启服务/杀进程留给 Agent 的
  命令执行通道，那里有人工审批与审计）；
- 全部工具声明 read_only_hint=True（服务端建议，最终只读资格由用户
  在插件面板逐项确认）；
- 仅依赖标准库 + mcp SDK，不引入 psutil 等第三方（有则自动增强）；
- 绝不 print() 到 stdout —— stdout 是 MCP 协议通道，日志一律走 stderr。

启动方式（插件面板配置）：
  command: <python.exe>
  args:    ["-u", "<本文件绝对路径>"]

工具清单（调用名 = mcp__ops_expert__<name>）：
  system_overview   系统概览：主机名/OS/CPU/内存/各盘使用率
  disk_usage        指定路径所在分区的容量使用情况
  top_processes     资源占用 Top 进程列表（psutil 可用时含 CPU/内存）
  tcp_probe         TCP 端口连通性探测（目标:端口 是否可达 + 延迟）
  http_probe        HTTP 健康检查（状态码/延迟/Server 头，只发 GET）
  log_tail          安全读取日志文件尾部（2MB 上限，防止巨文件拖爆会话）
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# MCP 2.x（requirements.txt 锁 mcp>=2.0,<3.0）：FastMCP 已更名 MCPServer。
mcp = MCPServer("ops-expert")


def _readonly():
    """全部工具声明只读（manager.py 按 annotations.read_only_hint 读取，
    最终只读资格仍由用户在插件面板逐项确认）。"""
    return {"annotations": ToolAnnotations(read_only_hint=True)}

_MAX_LOG_BYTES = 2 * 1024 * 1024  # log_tail 单次读取上限
_PROC_TOP_LIMIT = 30


def _log(msg: str) -> None:
    """诊断日志走 stderr，绝不污染 stdout 协议通道。"""
    print(f"[ops-expert] {msg}", file=sys.stderr, flush=True)


# ── 工具 1：系统概览 ─────────────────────────────────────────────────────────


def _memory_info() -> dict:
    """内存信息：Windows 走 GlobalMemoryStatusEx，POSIX 读 /proc/meminfo。"""
    if sys.platform == "win32":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        total, avail = st.ullTotalPhys, st.ullAvailPhys
        return {
            "total_gb": round(total / 2**30, 2),
            "available_gb": round(avail / 2**30, 2),
            "used_percent": st.dwMemoryLoad,
        }
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, val = line.partition(":")
            info[key.strip()] = int(val.strip().split()[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used_pct = round((total - avail) / total * 100, 1) if total else None
        return {
            "total_gb": round(total / 2**30, 2),
            "available_gb": round(avail / 2**30, 2),
            "used_percent": used_pct,
        }
    except OSError:
        return {"error": "无法读取内存信息"}


def _drive_usage() -> list[dict]:
    """枚举分区使用率：Windows 遍历盘符，POSIX 只看根分区。"""
    out = []
    if sys.platform == "win32":
        import string

        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            try:
                u = shutil.disk_usage(root)
            except OSError:
                continue  # 光驱/空读卡器等不可读盘跳过
            out.append({
                "mount": root,
                "total_gb": round(u.total / 2**30, 1),
                "free_gb": round(u.free / 2**30, 1),
                "used_percent": round(u.used / u.total * 100, 1) if u.total else None,
            })
    else:
        try:
            u = shutil.disk_usage("/")
            out.append({
                "mount": "/",
                "total_gb": round(u.total / 2**30, 1),
                "free_gb": round(u.free / 2**30, 1),
                "used_percent": round(u.used / u.total * 100, 1) if u.total else None,
            })
        except OSError:
            pass
    return out


@mcp.tool(**_readonly())
def system_overview() -> str:
    """系统概览：主机名、操作系统、CPU 核数、内存使用、各分区磁盘使用率。
    运维排障的第一步：先看整体水位，再决定深入哪个方向。"""
    try:
        loadavg = None
        if hasattr(os, "getloadavg"):
            try:
                loadavg = [round(x, 2) for x in os.getloadavg()]
            except (OSError, ValueError):
                pass
        payload = {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "loadavg": loadavg,
            "memory": _memory_info(),
            "disks": _drive_usage(),
            "sampled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"system_overview 失败: {type(exc).__name__}: {exc}"


# ── 工具 2：指定路径磁盘使用 ─────────────────────────────────────────────────


@mcp.tool(**_readonly())
def disk_usage(path: str) -> str:
    """查询指定路径所在分区的容量使用情况（总量/已用/剩余/百分比）。
    path 必须是已存在的目录或文件路径。"""
    try:
        target = Path(path)
        if not target.exists():
            return f"路径不存在: {path}"
        u = shutil.disk_usage(str(target if target.is_dir() else target.parent))
        return json.dumps({
            "path": str(target),
            "mount_total_gb": round(u.total / 2**30, 1),
            "used_gb": round(u.used / 2**30, 1),
            "free_gb": round(u.free / 2**30, 1),
            "used_percent": round(u.used / u.total * 100, 1) if u.total else None,
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"disk_usage 失败: {type(exc).__name__}: {exc}"


# ── 工具 3：Top 进程 ────────────────────────────────────────────────────────


def _top_processes_psutil(limit: int) -> list[dict] | None:
    """psutil 可用时按内存排序取 Top 进程（含 CPU%，单次采样近似值）。"""
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            mem = p.info["memory_info"]
            procs.append({
                "pid": p.info["pid"],
                "name": (p.info["name"] or "")[:80],
                "memory_mb": round((mem.rss if mem else 0) / 2**20, 1),
                "cpu_percent": p.info["cpu_percent"],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x["memory_mb"], reverse=True)
    return procs[:limit]


def _top_processes_tasklist(limit: int) -> list[dict]:
    """Windows 兜底：tasklist /FO CSV 解析（GBK 输出，手动解码）。"""
    try:
        raw = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=15,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{"error": f"tasklist 执行失败: {exc}"}]
    text = None
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    rows = []
    for line in text.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 5 or not parts[1].isdigit():
            continue
        # tasklist CSV: 名称, PID, 会话名, 会话号, 内存
        mem_kb = "".join(ch for ch in parts[4] if ch.isdigit())
        rows.append({
            "name": parts[0][:80],
            "pid": int(parts[1]),
            "memory_mb": round(int(mem_kb or 0) / 1024, 1),
        })
    rows.sort(key=lambda x: x["memory_mb"], reverse=True)
    return rows[:limit]


def _top_processes_ps(limit: int) -> list[dict]:
    """POSIX 兜底：ps aux 按内存排序。"""
    try:
        raw = subprocess.run(
            ["ps", "aux", "--sort=-%mem"], capture_output=True, timeout=15,
        ).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{"error": f"ps 执行失败: {exc}"}]
    rows = []
    for line in raw.splitlines()[1:]:
        cols = line.split(None, 10)
        if len(cols) < 11:
            continue
        try:
            rows.append({
                "user": cols[0], "pid": int(cols[1]),
                "cpu_percent": float(cols[2]), "mem_percent": float(cols[3]),
                "name": cols[10][:80],
            })
        except ValueError:
            continue
    return rows[:limit]


@mcp.tool(**_readonly())
def top_processes(limit: int = 10) -> str:
    """按内存占用列出 Top N 进程（1-30）。psutil 可用时附带 CPU 占用，
    否则回退 tasklist/ps。用于定位内存泄漏与异常进程。"""
    limit = max(1, min(int(limit or 10), _PROC_TOP_LIMIT))
    rows = _top_processes_psutil(limit)
    source = "psutil"
    if rows is None:
        source = "tasklist" if sys.platform == "win32" else "ps"
        rows = _top_processes_tasklist(limit) if sys.platform == "win32" else _top_processes_ps(limit)
    return json.dumps({"source": source, "count": len(rows), "processes": rows},
                      ensure_ascii=False, indent=2)


# ── 工具 4：TCP 端口探测 ────────────────────────────────────────────────────


@mcp.tool(**_readonly())
def tcp_probe(host: str, port: int, timeout_s: float = 3.0) -> str:
    """探测 TCP 端口连通性（connect + 立即关闭，不发送任何应用层数据）。
    返回可达性与连接延迟毫秒。用于判断服务是否在监听。"""
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            return "端口必须在 1-65535 之间"
        timeout_s = min(max(float(timeout_s or 3.0), 0.5), 10.0)
        start = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout_s):
            latency_ms = round((time.monotonic() - start) * 1000, 1)
        return json.dumps({
            "host": host, "port": port, "reachable": True,
            "latency_ms": latency_ms,
        }, ensure_ascii=False)
    except OSError as exc:
        return json.dumps({
            "host": host, "port": int(port or 0), "reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False)


# ── 工具 5：HTTP 健康检查 ───────────────────────────────────────────────────


@mcp.tool(**_readonly())
def http_probe(url: str, timeout_s: float = 5.0) -> str:
    """HTTP 健康检查：发一次 GET（只读请求），返回状态码/延迟/Server 头。
    用于探活 web 服务与健康端点。不跟随需要认证的页面。"""
    try:
        timeout_s = min(max(float(timeout_s or 5.0), 0.5), 15.0)
        req = urllib.request.Request(
            url, method="GET",
            headers={"User-Agent": "ckun-ops-expert/1.0 (health-probe)"},
        )
        start = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body_head = resp.read(512).decode("utf-8", errors="replace")
            return json.dumps({
                "url": url, "status": resp.status,
                "latency_ms": round((time.monotonic() - start) * 1000, 1),
                "server": resp.headers.get("Server", ""),
                "content_type": resp.headers.get("Content-Type", ""),
                "body_head": body_head[:200],
            }, ensure_ascii=False, indent=2)
    except urllib.error.HTTPError as exc:
        return json.dumps({
            "url": url, "status": exc.code, "reachable": True,
            "error": f"HTTP {exc.code}: {exc.reason}（服务在响应，但该端点返回错误码）",
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({
            "url": url, "reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False)


# ── 工具 6：日志尾部 ────────────────────────────────────────────────────────


@mcp.tool(**_readonly())
def log_tail(path: str, lines: int = 100) -> str:
    """安全读取日志文件尾部 N 行（默认 100，上限 500）。
    大文件只从尾部取 2MB 窗口再截行，不会整读拖爆内存；仅读取，不修改。"""
    try:
        lines = max(1, min(int(lines or 100), 500))
        f = Path(path)
        if not f.exists():
            return f"文件不存在: {path}"
        if not f.is_file():
            return f"不是常规文件（可能是目录/设备）: {path}"
        size = f.stat().st_size
        with f.open("rb") as fh:
            if size > _MAX_LOG_BYTES:
                fh.seek(-_MAX_LOG_BYTES, os.SEEK_END)
            chunk = fh.read(_MAX_LOG_BYTES)
        text = chunk.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        tail = all_lines[-lines:]
        truncated_note = (
            f"\n... [文件共 {size} 字节，仅展示尾部 {len(tail)} 行；"
            f"窗口 {min(size, _MAX_LOG_BYTES)} 字节]" if size > _MAX_LOG_BYTES else ""
        )
        return "\n".join(tail) + truncated_note
    except PermissionError:
        return f"无读取权限: {path}"
    except Exception as exc:
        return f"log_tail 失败: {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    _log("ops-expert MCP server 启动（stdio）")
    mcp.run()
