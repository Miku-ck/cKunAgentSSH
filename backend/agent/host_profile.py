"""目标机环境画像 — 把"这台机器是什么环境"变成 Agent 的已知事实。

问题
----
Agent 生成运维命令时最大的盲区是目标机环境：不知道发行版就可能对 Ubuntu 发
`yum install`；不知道 init 系统就可能对非 systemd 机器发 `systemctl`。
`ops_obs.sys_overview` 虽然读了 `uname -r`，但那只是"某次工具调用的输出"，
不会沉淀成已知事实随每轮进入上下文 —— 模型往往在第一轮就凭训练数据里的
印象生成命令。

本模块在 run 开始时用一条**只读、免审**的探测命令
（`ops_obs.host_profile_cmd`，对应远端 agent-exec 的 host_profile 模板）
取回环境事实，压缩成紧凑块注入系统提示。

硬约束
------
1. **失败必须静默降级**：探测失败 / 超时 / 目标机未部署新版 agent-exec，
   一律返回"环境未知"文案，绝不抛异常、绝不阻塞 run。旧版 wrapper 没有
   这条模板是**预期情形**（新增模板需重新部署才生效），不能因此让 run 失败。
2. **按 ssh_session_id 隔离**：多主机场景下画像不能串味。
3. 成功 TTL 长（30 分钟）、失败 TTL 短（5 分钟）—— 后者便于目标机补部署
   agent-exec 后自动恢复，无需重启后端。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 画像缓存 TTL（秒）
_TTL_OK = 30 * 60
_TTL_UNKNOWN = 5 * 60
# 探测超时（秒）：只读小命令，给 10 秒足够覆盖慢链路
_PROBE_TIMEOUT = 10
# 紧凑块长度上限（字符），控制常驻 system prompt 的 token 预算
_MAX_CHARS = 400

NO_SESSION_TEXT = (
    "[目标机环境] 未连接主机（无 SSH 会话）。本模式的运维工具仅在远程 SSH 会话内执行，"
    "不会在后端主机本地执行任何命令。"
)
UNKNOWN_TEXT = (
    "[目标机环境] 未知（环境探测未返回有效信息）。生成命令前请先用只读工具确认环境"
    "（sys_overview 看内核与负载、process_inspect 看进程、log_read 看日志），"
    "不要假设发行版、包管理器或 init 系统。"
)

# ssh_session_id -> (expire_at_monotonic, profile_text)
_CACHE: dict[str, tuple[float, str]] = {}
# 缓存上限：超过时先清过期项，仍超则淘汰 expire_at 最小的条目（防长期运行无界增长）
_CACHE_MAX = 50


def _clean(v: str) -> str:
    """净化远端返回的字段值：去掉换行/控制字符与方括号。

    画像会原样进系统提示，若不去方括号，被攻陷的目标机可借
    /etc/os-release 的 PRETTY_NAME 伪造「[目标机环境] …」段标签做提示注入。
    """
    v = v.replace("\r", " ").replace("\n", " ")
    v = v.replace("[", "（").replace("]", "）")
    return " ".join(v.split())


def clear_cache(session_id: Optional[str] = None) -> None:
    """清空画像缓存（测试与手动刷新用）。session_id=None 时全清。"""
    if session_id is None:
        _CACHE.clear()
        return
    _CACHE.pop(session_id, None)


def is_known_profile(text: str) -> bool:
    """探测文案是否为真实环境事实（而非降级占位文案）。

    降级文案是给模型看的提示，不是关于这台机器的事实 —— 把它写进 host-profile
    画像块会让"探测失败一次"常驻到下次成功为止，读起来像确凿结论。
    """
    return bool(text) and text not in (NO_SESSION_TEXT, UNKNOWN_TEXT)


# ── 解析（纯函数，便于单测）────────────────────────────────────────────────

def _split_sections(raw: str) -> dict[str, list[str]]:
    """把 `echo "[段名]"; ...` 的输出按段名切分。

    尾部哨兵行（`[exit_code=..]` / `[stderr]`）必须先于段标签判断，
    否则会被误认成段名。
    """
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("[exit_code=") or s == "[stderr]":
            continue
        if s.startswith("[") and s.endswith("]") and len(s) < 30:
            current = s[1:-1]
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(s)
    return sections


def _parse_os_release(lines: list[str]) -> str:
    """从 /etc/os-release 片段提取可读发行版名。

    PRETTY_NAME 优先（Ubuntu/Debian/Alpine/RHEL8+ 均在前 5 行）；
    CentOS 7 这类无 PRETTY_NAME 的则回退 NAME + VERSION 拼接。
    """
    kv: dict[str, str] = {}
    for ln in lines:
        if "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        kv[k.strip()] = v.strip().strip('"').strip("'")
    if kv.get("PRETTY_NAME"):
        return kv["PRETTY_NAME"]
    joined = " ".join(p for p in (kv.get("NAME"), kv.get("VERSION")) if p)
    return joined or kv.get("ID", "")


def _basenames(lines: list[str]) -> list[str]:
    """从 `command -v` 输出的绝对路径中取可执行名（保序去重）。"""
    out: list[str] = []
    for ln in lines:
        name = ln.strip().rsplit("/", 1)[-1]
        if name and name not in out:
            out.append(name)
    return out


def build_host_profile(raw: str) -> str:
    """把探测原始输出压缩成一行紧凑的环境事实；无有效信息时返回 UNKNOWN_TEXT。

    输出示例：
        [目标机环境] 发行版: Ubuntu 22.04.3 LTS; init: systemd;
        包管理器: apt-get; 容器: docker; 用户: ckunagent; 目录: /srv/ckun-agent
    """
    if not raw or not raw.strip():
        return UNKNOWN_TEXT

    sec = _split_sections(raw)

    distro = _clean(_parse_os_release(sec.get("发行版", [])))
    init_lines = sec.get("init", [])
    init = _clean(init_lines[0]) if init_lines else ""

    tools = _basenames(sec.get("包管理器", []))
    pkg_order = [t for t in ("apt-get", "dnf", "yum", "apk", "pacman", "zypper") if t in tools]
    pkg = pkg_order[0] if pkg_order else ""
    container = [t for t in ("docker", "podman") if t in tools]

    ident = sec.get("身份", [])
    user = _clean(ident[0]) if len(ident) > 0 else ""
    cwd = _clean(ident[1]) if len(ident) > 1 else ""

    facts: list[str] = []
    if distro:
        facts.append(f"发行版: {distro}")
    if init:
        facts.append(f"init: {init}")
    if pkg:
        facts.append(f"包管理器: {pkg}")
    if container:
        facts.append("容器: " + ",".join(container))
    if user:
        facts.append(f"用户: {user}")
    if cwd:
        facts.append(f"目录: {cwd}")

    if not facts:
        return UNKNOWN_TEXT

    text = "[目标机环境] " + "; ".join(facts)
    if len(text) > _MAX_CHARS:
        text = text[: _MAX_CHARS - 3] + "..."
    return text


# ── 探测入口（在 run 开始时调用一次）──────────────────────────────────────

async def ensure_host_profile(ctx) -> str:
    """取目标机环境画像（带缓存）。

    - 无 SSH 会话 → NO_SESSION_TEXT（不探测）
    - 命中未过期缓存 → 直接返回
    - 否则探测一次，成功写长 TTL、失败写短 TTL
    - **任何异常都被吞掉并降级**：画像属增强信息，不能因其失败影响 run
    """
    session_id = (getattr(ctx, "ssh_session_id", "") or "").strip()
    if not session_id:
        return NO_SESSION_TEXT

    now = time.monotonic()
    cached = _CACHE.get(session_id)
    if cached and cached[0] > now:
        return cached[1]

    try:
        # 局部导入：避免模块级依赖 electron_bridge，纯解析函数可独立测试
        from agent.tools.builtin.ops_common import run_headless
        from agent.tools.builtin.ops_obs import host_profile_cmd

        ok, raw = await run_headless(ctx, host_profile_cmd(), _PROBE_TIMEOUT)
        profile = build_host_profile(raw) if ok else UNKNOWN_TEXT
    except Exception as e:
        # 桥不可用 / 会话已断 / 目标机未部署该模板 → 一律降级
        logger.warning("目标机环境探测失败，降级为未知: %s", e)
        profile = UNKNOWN_TEXT

    ttl = _TTL_OK if profile != UNKNOWN_TEXT else _TTL_UNKNOWN
    if len(_CACHE) >= _CACHE_MAX:
        now = time.monotonic()
        for k in [k for k, (exp, _) in _CACHE.items() if exp <= now]:
            _CACHE.pop(k, None)
        while len(_CACHE) >= _CACHE_MAX:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
    _CACHE[session_id] = (now + ttl, profile)
    return profile
