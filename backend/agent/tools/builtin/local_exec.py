"""
Local 执行工具 — IDE 模式专属

在 IDE 模式下执行受限的本地命令（白名单 + safety 双重检测），
用于运行测试、构建、lint 等开发命令。

与 shell_exec 的区别：
- shell_exec: SSH 模式，无白名单限制（由 safety.py 检测）
- local_exec: IDE 模式，白名单 + safety 双重检测
"""
import asyncio
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.harness.safety import classify_command

# 允许的命令前缀白名单
_ALLOWED_COMMANDS = [
    "npm test", "npm run", "npm ci", "npm install",
    "npx ",
    "python -m pytest", "python -m unittest", "python script",
    "pytest",
    "git status", "git diff", "git log", "git branch", "git show",
    "tsc --noEmit",
    "eslint ", "prettier ",
    "ruff ", "black ", "mypy ",
    "cargo build", "cargo test", "cargo check",
    "go test", "go build",
    "ls ", "dir ",  # 基础查看
    "echo ",  # 调试
]

# 大输出保护阈值（与 shell_exec 对齐）
_LOCAL_OUTPUT_THRESHOLD = 4000
_LOCAL_OUTPUT_HEAD = 1500
_LOCAL_OUTPUT_TAIL = 1000


def _is_allowed(command: str) -> bool:
    """白名单检测 — 命令前缀匹配（大小写不敏感）"""
    cmd = command.strip().lower()
    for allowed in _ALLOWED_COMMANDS:
        if cmd.startswith(allowed.lower()):
            return True
    return False


class LocalExecTool(BaseTool):
    """本地命令执行工具（IDE 模式）

    通过白名单 + safety.classify_command 双重检测，限制只执行安全的开发命令。
    用于运行测试、构建、lint 等本地开发任务。

    安全策略：
    1. 白名单前缀匹配 — 只允许常见开发命令（npm/git/cargo/go/python 等）
    2. safety.classify_command 二次检测 — hardline/dangerous 均拦截
       （防止白名单命令通过管道/重定向注入危险操作，如 `git diff; rm -rf /`）
    """
    name = "local_exec"
    description = "在 IDE 模式下执行受限的本地命令（白名单机制），用于运行测试、构建、lint 等"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "cwd": {"type": "string", "description": "工作目录（默认工作区根目录）"},
            "timeout": {"type": "integer", "description": "超时秒数（默认 30）"},
        },
        "required": ["command"],
    }
    meta = ToolMeta(
        risk_level="moderate",
        reversible=False,
        timeout=30.0,
        mode_scope=["ssh"],
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
        # 1. 白名单检测
        if not _is_allowed(command):
            return ToolResult(
                False,
                "",
                f"命令不在白名单内: {command}",
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
            # 白名单命令可能含管道/重定向，仍用 shell 执行；
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
