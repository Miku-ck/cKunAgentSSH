"""
Local 执行工具 — IDE 模式专属

在 IDE 模式下执行受限的本地命令（白名单 + safety 双重检测），
用于运行测试、构建、lint 等开发命令。

与 shell_exec 的区别：
- shell_exec: SSH 模式，无白名单限制（由 safety.py 检测）
- local_exec: IDE 模式，白名单 + safety 双重检测
"""
import asyncio
import os
import re
import shlex
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.harness.safety import classify_command

# shell 元字符：白名单场景不需要这些字符，出现即拒绝
# B-2 修复：加入 \n\r — create_subprocess_shell 按换行拆命令，
# 原正则漏掉换行导致 `git status\nnpx evil` 可注入第二条命令
_META_CHAR_RE = re.compile(r"[;&|<>`${}\n\r]")

# 允许的二进制名（argv[0] 的 basename）
# B-1b 修复：移除 npm/npx（包管理器可间接执行任意脚本，改由 safety 判 dangerous 走审批）
_ALLOWED_BINARIES = frozenset({
    "python", "python3", "pytest",
    "git", "tsc", "eslint", "prettier", "ruff", "black", "mypy",
    "cargo", "go", "ls", "dir", "echo",
})

# 大输出保护阈值（与 shell_exec 对齐）
_LOCAL_OUTPUT_THRESHOLD = 4000
_LOCAL_OUTPUT_HEAD = 1500
_LOCAL_OUTPUT_TAIL = 1000


def _validate_command(command: str) -> tuple[bool, str]:
    """严格白名单校验

    第一道：原始命令含 shell 元字符（; & | < > ` $ { }）→ 直接拒绝
    第二道：shlex 解析 argv，校验 argv[0] basename 与子命令/参数

    Returns:
        (allowed, reason) — allowed=False 时 reason 为拒绝原因
    """
    # 第一道：shell 元字符检查
    if _META_CHAR_RE.search(command):
        return (False, "命令包含 shell 元字符，已被白名单拒绝")

    # 第二道：argv 解析
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return (False, "命令无法解析（引号未闭合等），已被白名单拒绝")
    if not argv:
        return (False, "空命令")

    binary = os.path.basename(argv[0])
    if binary not in _ALLOWED_BINARIES:
        return (False, f"命令不在白名单内: {command}")

    rest = argv[1:]

    if binary in ("python", "python3"):
        if not rest:
            return (False, f"python 需要 -m 模块或 .py 脚本路径: {command}")
        if rest[0] == "-m":
            if len(rest) < 2 or rest[1] not in {"pytest", "unittest"}:
                return (False, f"python -m 仅允许 pytest/unittest: {command}")
        elif not rest[0].endswith(".py"):
            return (False, f"python 仅允许 -m pytest/unittest 或 .py 脚本: {command}")
        else:
            # B-1b 修复：.py 脚本必须是工作区相对路径
            # （绝对路径/家目录/.. 可逃逸工作区执行任意脚本；-m 模块不受此限）
            script = rest[0]
            if script.startswith("/") or script.startswith("~") or ".." in script:
                return (False, f"python 脚本必须是工作区相对路径（禁止绝对路径/家目录/..）: {command}")
    elif binary == "pytest":
        # B-1b 修复：非 flag 参数（路径）禁止绝对路径/家目录/..，flag 放行
        for a in rest:
            if not a.startswith("-") and (a.startswith("/") or a.startswith("~") or ".." in a):
                return (False, f"pytest 路径参数必须是工作区相对路径（禁止绝对路径/家目录/..）: {command}")
    elif binary == "git":
        if not rest or rest[0] not in {"status", "diff", "log", "branch", "show"}:
            return (False, f"git 仅允许 status/diff/log/branch/show 子命令: {command}")
    elif binary in ("ls", "dir"):
        if rest and any(not a.startswith("-") for a in rest):
            return (False, f"{binary} 仅允许 flag 参数（- 开头）: {command}")
    elif binary == "echo":
        pass  # 已无元字符，任意参数
    elif binary in ("cargo", "go"):
        if not rest or rest[0] not in {"build", "test", "check"}:
            return (False, f"{binary} 仅允许 build/test/check 子命令: {command}")
    # tsc/eslint/prettier/ruff/black/mypy：仅校验 argv[0]（保守策略）

    return (True, "")


class LocalExecTool(BaseTool):
    """本地命令执行工具（IDE 模式）

    通过严格白名单 + safety.classify_command 双重检测，限制只执行安全的开发命令。
    用于运行测试、构建、lint 等本地开发任务。

    安全策略：
    1. 严格白名单校验 — 拒绝 shell 元字符（; & | < > ` $ { }），
       再用 shlex 解析 argv 校验二进制名与子命令/参数
    2. safety.classify_command 二次检测 — hardline/dangerous 均拦截
       （即使白名单命令也禁止管道/重定向/命令替换）
    """
    name = "local_exec"
    description = (
        "在 IDE 模式下执行受限的本地命令。仅允许简单白名单命令，"
        "禁止 shell 元字符/管道/命令替换（如 git status、python -m pytest、pytest tests/、tsc --noEmit）"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "cwd": {"type": "string", "description": "工作目录（默认工作区根目录）"},
            "timeout": {"type": "integer", "description": "超时秒数（1-120，默认 30）"},
        },
        "required": ["command"],
    }
    meta = ToolMeta(
        risk_level="moderate",
        reversible=False,
        timeout=30.0,
        mode_scope=["ide"],  # IDE 模式专属（与文档描述一致）
        cacheable=False,
    )

    def _maybe_truncate_output(self, output: str) -> str:
        """大输出保护：超过阈值时保留头尾，中间省略"""
        if len(output) <= _LOCAL_OUTPUT_THRESHOLD:
            return output
        head = output[:_LOCAL_OUTPUT_HEAD]
        tail = output[-_LOCAL_OUTPUT_TAIL:]
        omitted = len(output) - _LOCAL_OUTPUT_HEAD - _LOCAL_OUTPUT_TAIL
        return head + f"\n... [输出已截断，省略中间 {omitted} 字符]\n" + tail

    async def execute(self, context, command, cwd=None, timeout=30):
        # 0. 超时上限钳制（1-120 秒）
        timeout = max(1, min(int(timeout), 120))

        # 1. 严格白名单校验
        allowed, reason = _validate_command(command)
        if not allowed:
            return ToolResult(
                False,
                "",
                reason,
            )

        # 2. safety 二次检测 — hardline/dangerous 均拦截
        #    防御白名单命令通过管道/重定向注入危险操作
        level, reason = classify_command(command)
        if level in ("hardline", "dangerous"):
            return ToolResult(
                False,
                "",
                f"命令被安全检测拦截（{level}）: {reason}",
            )

        # 3. 确定工作目录 — cwd 优先，否则用工作区根目录
        work_dir = cwd or context.workspace_path or None

        try:
            # 白名单命令经严格校验无 shell 元字符，直接用 shell 执行即可；
            # 安全性由白名单 + safety 双重检测保证
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            output = ""
            if stdout:
                output += stdout.decode("utf-8", errors="replace")
            if stderr:
                output += f"\n[stderr]\n{stderr.decode('utf-8', errors='replace')}"

            output += f"\n[exit_code={proc.returncode}]"

            # 大输出保护：返回前应用头尾保留截断
            output = self._maybe_truncate_output(output)

            success = proc.returncode == 0
            if success:
                # 命令输出：通常是噪声，设为 low
                return ToolResult(True, output, priority="low")
            # 失败分支由 Saddle 自动提升为 high，不在此显式设置
            return ToolResult(False, output, f"exit_code={proc.returncode}")

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # 竞态：进程在超时和 kill 之间已退出
            await proc.wait()
            return ToolResult(False, "", f"命令执行超时（{timeout}秒），已终止")
        except Exception as e:
            return ToolResult(False, "", str(e))
