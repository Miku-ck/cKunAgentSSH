"""运维只读观测工具集 — SSH 模式专属。

包含四个只读、安全、幂等的 BaseTool 子类：
  - sys_overview    ：系统概览（CPU 负载 / 内存 / 磁盘 / 开机时长 / 内核版本）
  - process_inspect ：按名称/命令子串匹配进程并输出表格
  - network_probe   ：探测 host:port 连通性 + 本地端口监听状态
  - log_read        ：读取日志尾部（可过滤关键词）

安全约束：所有用户输入的参数一律经 shlex.quote 注入固定命令模板，
绝不允许用户直接提供任意命令文本。命令一律只读，非破坏性。
"""
import shlex

from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.ops_common import TRAVERSAL_ERROR, clamp_int, is_traversal, parse_int_arg, run_headless
from agent.remote_protocol import EXIT_CODE_PREFIX

# ── 远端命令构建（单一事实来源）──────────────────────────────
# 契约源：与远端白名单执行器（scripts/deploy/agent-exec.py）对齐，
# 由 backend/tests/test_remote_whitelist_contract.py 跨端校验。

def _without_sentinel(raw: str) -> str:
    """剔除 run_headless 恒追加的 [exit_code=…] 哨兵行，返回有效输出。

    执行通道把退出码哨兵拼在正文之后（ops_common.run_headless），所以「整串不含
    哨兵」这类判据对任何成功输出都恒假 —— 见 S6-07。逐行剔除才是与本文件进程段
    一致的读法。
    """
    return "\n".join(
        line for line in raw.splitlines() if EXIT_CODE_PREFIX not in line
    ).strip()


def sys_overview_cmd(top: int) -> str:
    """sys_overview 远端模板：多行只读观测脚本（; 串联，缺失命令不影响其余）。"""
    return "\n".join(
        [
            'echo "[CPU 负载]"; uptime 2>/dev/null',
            'echo "[内存]"; free -m 2>/dev/null',
            'echo "[磁盘 TOP %d]"; df -h 2>/dev/null | head -n %d' % (top, top + 1),
            'echo "[开机时长]"; uptime -s 2>/dev/null',
            'echo "[内核版本]"; uname -r 2>/dev/null',
        ]
    )


def host_profile_cmd() -> str:
    """host_profile 远端模板：固定的只读环境探测（**无参数**）。

    探测项：发行版 / init 系统 / 包管理器与容器运行时 / 当前身份与工作目录。
    用途：把"目标机是什么环境"这一事实注入 Agent 系统提示，避免模型靠猜
    生成与发行版不匹配的命令（如 Ubuntu 上给 yum install）。

    两个硬约束（改动时必须同步 scripts/deploy/agent-exec.py 的 RE_HOST_PROFILE）：
    1. **完全固定字面量、无参数**：远端白名单是整串锚点正则匹配，
       有可变参数就得引入捕获组，无参数才能一条精确正则了事。
    2. **末尾 `true` 保证整体 exit_code == 0**：exec_remote 的 success 判定是
       `bridge.success and exit_code == 0`，任一子命令非 0（命令缺失/无权限）
       都会让整条被判失败，进而误报"环境未知"。
    """
    return "\n".join(
        [
            'echo "[发行版]"; cat /etc/os-release 2>/dev/null | head -n 5',
            'echo "[init]"; ps -p 1 -o comm= 2>/dev/null',
            'echo "[包管理器]"; command -v apt-get dnf yum apk pacman zypper docker 2>/dev/null',
            'echo "[身份]"; id -un 2>/dev/null; pwd 2>/dev/null',
            "true",
        ]
    )


def process_inspect_cmd(query: str) -> str:
    """process_inspect 远端模板：按子串匹配进程（排除 grep 自身）。"""
    return f"ps -eo pid,pcpu,pmem,stat,comm,args | grep {shlex.quote(query)} | grep -v grep"


def netprobe_bash_cmd(host: str, port: str, timeout: str) -> str:
    """network_probe 远端模板①：bash /dev/tcp 探测（host/port 经 $1/$2 传参）。"""
    hq, pq = shlex.quote(host), shlex.quote(port)
    return (
        f"bash -c 'exec 3<>/dev/tcp/$1/$2 && echo OK || echo FAIL; "
        f"exec 3<&-' probe {hq} {pq}"
    )


def netprobe_nc_cmd(host: str, port: str, timeout: str) -> str:
    """network_probe 远端模板②：nc 回退探测（macOS/老系统）。"""
    hq, pq, tq = shlex.quote(host), shlex.quote(port), shlex.quote(timeout)
    return f"nc -zvw{tq} {hq} {pq}"


def netprobe_listen_cmd(port: str) -> str:
    """network_probe 远端模板③：本地监听状态（ss 优先，netstat 回退）。"""
    pq = shlex.quote(port)
    return f"ss -ltn 2>/dev/null | grep :{pq}"


def netprobe_listen_fallback_cmd(port: str) -> str:
    """network_probe 远端模板④：netstat 回退监听查询。"""
    pq = shlex.quote(port)
    return f"netstat -an 2>/dev/null | grep :{pq}"


def log_tail_cmd(path: str, lines: int) -> str:
    """log_read 远端模板：无过滤时直接读尾部。"""
    return f"tail -n {lines} {shlex.quote(path)}"


def log_grep_cmd(path: str, pattern: str, lines: int) -> str:
    """log_read 远端模板：关键词过滤 + 限行。"""
    return f"grep {shlex.quote(pattern)} {shlex.quote(path)} | tail -n {lines}"

# 只读观测结果属于噪声，统一使用 low 优先级
_OBS_META = ToolMeta(
    risk_level="safe",
    reversible=True,
    idempotent=True,
    timeout=15.0,
)

class SysOverviewTool(BaseTool):
    name = "sys_overview"
    description = "读取系统只读概览：CPU 负载、内存、磁盘占用（TOP N）、开机时长、内核版本。"
    parameters = {
        "type": "object",
        "properties": {
            "top": {
                "type": "integer",
                "description": "磁盘占用 TOP 行数（1-20，默认 3）",
                "default": 3,
            },
        },
        "required": [],
    }
    meta = _OBS_META

    async def execute(self, context, top=3):
        top, err = parse_int_arg(top, "top", 3)
        if err:
            return ToolResult(False, "", err)
        top = max(1, min(top, 20))
        # 多命令脚本：用 ; 串联，任一命令缺失不影响其余（保持健壮）。
        # upsilence 缺失命令的 stderr，避免标签被刷屏干扰。
        script = sys_overview_cmd(top)
        ok, output = await run_headless(context, script, timeout=15)
        if not ok:
            return ToolResult(False, output, "系统概览命令执行失败")
        return ToolResult(True, output, priority="low")


class ProcessInspectTool(BaseTool):
    name = "process_inspect"
    description = "按名称或命令行子串匹配进程，输出 PID/CPU%/内存%/状态/命令 表格。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要匹配的进程名称或命令子串",
            },
        },
        "required": ["query"],
    }
    meta = _OBS_META

    async def execute(self, context, query):
        # grep -v grep 排除 grep 自身进程条目
        command = process_inspect_cmd(query)
        ok, raw = await run_headless(context, command, timeout=15)

        rows = []
        for line in raw.splitlines():
            if EXIT_CODE_PREFIX in line or line.startswith("PID "):
                continue
            parts = line.split(None, 5)
            if len(parts) < 5:
                continue
            pid, cpu, mem, stat = parts[0], parts[1], parts[2], parts[3]
            if not pid.isdigit():
                continue
            comm = parts[4]
            args = parts[5] if len(parts) > 5 else comm
            rows.append(
                f"{pid:<8} {cpu:>5} {mem:>5}  {stat:<4} {comm:<12} {args}"
            )

        if not rows:
            return ToolResult(True, "未找到匹配进程", priority="low")

        header = f"{'PID':<8} {'CPU%':>5} {'MEM%':>5}  {'状态':<4} {'命令':<12} 参数"
        body = "\n".join(rows)
        return ToolResult(True, header + "\n" + body, priority="low")


class NetworkProbeTool(BaseTool):
    name = "network_probe"
    description = "探测 host:port TCP 连通性，并报告本地该端口监听状态。"
    parameters = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "目标主机（IP 或主机名）"},
            "port": {"type": "integer", "description": "目标端口（1-65535）"},
            "timeout": {"type": "integer", "description": "探测超时秒数（默认 5）"},
        },
        "required": ["host", "port"],
    }
    meta = _OBS_META

    def _run_probe_tests(self, host: str, port: str, timeout: str):
        """返回两个候选连通性探测命令模板（按 host/port 已引号转义）。

        审查R2修复：host/port/timeout 改为参数直传 —— 工具是注册表单例，
        原实现经 self._host 等实例属性跨方法传参，两个并发 network_probe
        会互踩（后者覆盖属性、前者用后者的值建探测命令）。
        """
        # bash /dev/tcp 探测：host/port 作为独立参数传入 bash -c，$1/$2 引用，
        # 避免把用户输入内联进单引号脚本串导致引号转义错乱。
        return netprobe_bash_cmd(host, port, timeout), netprobe_nc_cmd(host, port, timeout)

    async def execute(self, context, host, port, timeout=5):
        # port 不默认：猜一个端口去探测会回一句「无法连接」，比直接报错更容易误导模型
        port, perr = parse_int_arg(port, "port")
        timeout, terr = parse_int_arg(timeout, "timeout", 5)
        if perr or terr:
            return ToolResult(False, "", perr or terr)
        port = max(1, min(port, 65535))
        timeout = max(1, min(timeout, 30))
        # 审查R2修复：不再经 self._host/_port/_timeout 实例属性中转（见 _run_probe_tests）
        bash_cmd, nc_cmd = self._run_probe_tests(host, str(port), str(timeout))

        connected = None  # None=探测手段不可用，True/False=明确结果
        note = ""
        ok, out = await run_headless(context, bash_cmd, timeout=timeout)
        if ok and "OK" in out:
            connected = True
        elif ok and "FAIL" in out:
            connected = False
        else:
            # bash /dev/tcp 不可用或超时 → 尝试 nc
            ok2, out2 = await run_headless(context, nc_cmd, timeout=timeout)
            if ok2 and ("succeeded" in out2 or "open" in out2):
                connected = True
            elif ok2:
                connected = False
            else:
                note = "（bash /dev/tcp 与 nc 均不可用，连通性探测跳过）"

        # 本地监听状态：优先 ss，无结果时回退 netstat（跨平台健壮性）
        # S6-07：原判据是整串 `EXIT_CODE_PREFIX not in ss_out`，而 run_headless 对
        # 每一次成功执行都无条件追加该哨兵 ⇒ 条件恒假 ⇒ 本段永远输出「（无监听信息）」，
        # 对「端口是否在听」这个核心问题恒答否，netstat 回退支也因此不可达。
        # 改为与本文件进程段（逐行跳过哨兵）同一口径。
        _, ss_out = await run_headless(context, netprobe_listen_cmd(str(port)), timeout=15)
        listen = _without_sentinel(ss_out)
        if not listen:
            _, net_out = await run_headless(
                context, netprobe_listen_fallback_cmd(str(port)), timeout=15
            )
            listen = _without_sentinel(net_out)

        sections = []
        if connected is True:
            sections.append(f"[TCP 连通性] {host}:{port} 可连通")
        elif connected is False:
            sections.append(f"[TCP 连通性] {host}:{port} 无法连接")
        else:
            sections.append(f"[TCP 连通性] 无法探测：{note}")
        sections.append(
            f"[本地监听 {host}:{port}]\n" + (listen or "（无监听信息）")
        )
        return ToolResult(True, "\n".join(sections), priority="low")


class LogReadTool(BaseTool):
    name = "log_read"
    description = "读取日志文件尾部内容，可配合关键词过滤。只读、返回原始日志文本。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "日志文件绝对路径"},
            "lines": {"type": "integer", "description": "读取行数（1-1000，默认 100）"},
            "pattern": {"type": "string", "description": "可选关键词，过滤日志行（留空读取全部尾部）"},
        },
        "required": ["path"],
    }
    meta = _OBS_META

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        """日志尾部按真实模板判定：tail -n 100 /etc/shadow 与手敲同罪。

        meta.risk_level=safe 表达的是"读日志这个动作可逆"，不是"读任何文件都安全"。
        """
        path = args.get("path")
        if not isinstance(path, str) or not path.strip() or is_traversal(path):
            return None
        lines = clamp_int(args.get("lines"), 100, 1, 1000)
        pattern = args.get("pattern") or ""
        return log_grep_cmd(path, str(pattern), lines) if pattern else log_tail_cmd(path, lines)

    async def execute(self, context, path, lines=100, pattern=""):
        path = path or ""
        if not path.strip():
            return ToolResult(False, "", "日志文件路径不能为空")
        if is_traversal(path):
            return ToolResult(False, "", TRAVERSAL_ERROR)
        if any(ord(c) < 32 for c in path):  # 拒绝控制字符/换行，防止破坏模板
            return ToolResult(False, "", "日志文件路径包含非法字符")

        lines, err = parse_int_arg(lines, "lines", 100)
        if err:
            return ToolResult(False, "", err)
        lines = max(1, min(lines, 1000))

        if pattern:
            command = log_grep_cmd(path, pattern, lines)
        else:
            command = log_tail_cmd(path, lines)

        ok, raw = await run_headless(context, command, timeout=15)

        # 文件不存在 / 无权访问 → 明确失败
        lowered = raw.lower()
        if "no such file" in lowered or "cannot open" in lowered or "permission denied" in lowered:
            return ToolResult(
                False,
                raw,
                f"无法读取日志文件: {path}（文件不存在或无权访问）",
            )

        if pattern and not ok:
            # grep 无匹配或 exit_code 非 0
            return ToolResult(True, "未匹配到日志行", priority="low")

        if not ok:
            return ToolResult(False, raw, "日志读取失败，exit_code 非 0")

        return ToolResult(True, raw, priority="low")