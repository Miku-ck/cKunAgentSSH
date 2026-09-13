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
from agent.tools.builtin.ops_common import run_headless

# 只读观测结果属于噪声，统一使用 low 优先级
_OBS_META = ToolMeta(
    risk_level="safe",
    reversible=True,
    idempotent=True,
    timeout=15.0,
    mode_scope=["ssh"],
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
        top = max(1, min(int(top), 20))
        # 多命令脚本：用 ; 串联，任一命令缺失不影响其余（保持健壮）。
        # upsilence 缺失命令的 stderr，避免标签被刷屏干扰。
        script = "\n".join(
            [
                'echo "[CPU 负载]"; uptime 2>/dev/null',
                'echo "[内存]"; free -m 2>/dev/null',
                'echo "[磁盘 TOP %d]"; df -h 2>/dev/null | head -n %d'
                % (top, top + 1),
                'echo "[开机时长]"; uptime -s 2>/dev/null',
                'echo "[内核版本]"; uname -r 2>/dev/null',
            ]
        )
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
        q = shlex.quote(query)
        # grep -v grep 排除 grep 自身进程条目
        command = f"ps -eo pid,pcpu,pmem,stat,comm,args | grep {q} | grep -v grep"
        ok, raw = await run_headless(context, command, timeout=15)

        rows = []
        for line in raw.splitlines():
            if "[exit_code=" in line or line.startswith("PID "):
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

    def _run_probe_tests(self):
        """返回两个候选连通性探测命令模板（按 host/port 已引号转义）。"""
        host, port, timeout = self._host, self._port, self._timeout
        hq, pq, tq = shlex.quote(host), shlex.quote(port), shlex.quote(timeout)
        # bash /dev/tcp 探测：host/port 作为独立参数传入 bash -c，$1/$2 引用，
        # 避免把用户输入内联进单引号脚本串导致引号转义错乱。
        bash_cmd = (
            f"bash -c 'exec 3<>/dev/tcp/$1/$2 && echo OK || echo FAIL; "
            f"exec 3<&-' probe {hq} {pq}"
        )
        # nc 回退探测（macOS / 老系统常用）
        nc_cmd = f"nc -zvw{tq} {hq} {pq}"
        return bash_cmd, nc_cmd

    async def execute(self, context, host, port, timeout=5):
        port = max(1, min(int(port), 65535))
        timeout = max(1, min(int(timeout), 30))
        self._host, self._port, self._timeout = host, str(port), str(timeout)

        bash_cmd, nc_cmd = self._run_probe_tests()

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
        pq = shlex.quote(str(port))
        listen = ""
        _, ss_out = await run_headless(context, f"ss -ltn 2>/dev/null | grep :{pq}", timeout=15)
        if ss_out.strip() and "[exit_code=" not in ss_out:
            listen = ss_out.strip()
        else:
            _, net_out = await run_headless(
                context, f"netstat -an 2>/dev/null | grep :{pq}", timeout=15
            )
            if net_out.strip() and "[exit_code=" not in net_out:
                listen = net_out.strip()

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

    async def execute(self, context, path, lines=100, pattern=""):
        path = path or ""
        if not path.strip():
            return ToolResult(False, "", "日志文件路径不能为空")
        if any(ord(c) < 32 for c in path):  # 拒绝控制字符/换行，防止破坏模板
            return ToolResult(False, "", "日志文件路径包含非法字符")

        lines = max(1, min(int(lines), 1000))
        pq = shlex.quote(path)

        if pattern:
            pat = shlex.quote(pattern)
            command = f"grep {pat} {pq} | tail -n {lines}"
        else:
            command = f"tail -n {lines} {pq}"

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
            return ToolResult(False, raw, f"日志读取失败，exit_code 非 0")

        return ToolResult(True, raw, priority="low")