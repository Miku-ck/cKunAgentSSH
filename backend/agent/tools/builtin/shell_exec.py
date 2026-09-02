"""
Shell 执行工具 — SSH 模式专属

安全约束: 30 秒超时，非破坏性命令自主执行
"""
import asyncio
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.electron_bridge import ElectronBridge

# 大输出保护阈值（与 bridle.tool_result_max_chars 对齐）
_SHELL_OUTPUT_THRESHOLD = 4000
_SHELL_OUTPUT_HEAD = 1500
_SHELL_OUTPUT_TAIL = 1000


class ShellExecTool(BaseTool):
    name = "shell_exec"
    description = "执行 shell 命令并返回输出。超时 1-120 秒（默认 30）。"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "description": "超时秒数（1-120，默认 30）"},
        },
        "required": ["command"],
    }
    meta = ToolMeta(risk_level="moderate", reversible=False, timeout=30.0, mode_scope=["ssh"])

    def _maybe_truncate_output(self, output: str) -> str:
        """大输出保护：超过阈值时保留头尾，中间省略"""
        if len(output) <= _SHELL_OUTPUT_THRESHOLD:
            return output
        head = output[:_SHELL_OUTPUT_HEAD]
        tail = output[-_SHELL_OUTPUT_TAIL:]
        omitted = len(output) - _SHELL_OUTPUT_HEAD - _SHELL_OUTPUT_TAIL
        return head + f"\n... [输出已截断，省略中间 {omitted} 字符]\n" + tail

    async def execute(self, context, command, timeout=30):
        # 超时上限钳制（1-120 秒）
        timeout = max(1, min(int(timeout), 120))
        # SSH 模式：若存在 ssh_session_id 且 Electron Bridge 已配置，则远程执行
        if context.ssh_session_id:
            return await self._execute_remote(context.ssh_session_id, command, timeout)
        return await self._execute_local(context, command, timeout)

    async def _execute_remote(
        self, ssh_session_id: str, command: str, timeout: int
    ) -> ToolResult:
        bridge = ElectronBridge()
        if not bridge.is_available():
            return ToolResult(
                False,
                "",
                "远程执行未就绪：Electron Bridge 未配置（ELECTRON_BRIDGE_PORT/TOKEN 缺失）",
            )

        try:
            result = await bridge.exec_ssh_command(
                ssh_session_id=ssh_session_id,
                command=command,
                timeout=float(timeout),
            )
        except asyncio.CancelledError:
            # C-review M9：客户端断连 → run 被取消。本地 HTTP 等待中止，但远程
            # 命令无法经 bridge 终止（缺 kill 通道），只能让取消继续传播；
            # 已知残留：远程命令可能继续执行，待 bridge 协议补命令中止后修复
            raise
        except RuntimeError as e:
            return ToolResult(False, "", f"远程命令执行失败: {e}")

        output = result.get("output", "") or ""
        error = result.get("error", "") or ""
        exit_code = result.get("exit_code")

        full_output = output
        if error:
            full_output += f"\n[stderr]\n{error}"
        if exit_code is not None:
            full_output += f"\n[exit_code={exit_code}]"

        full_output = self._maybe_truncate_output(full_output)

        success = bool(result.get("success", False)) and exit_code == 0
        if success:
            return ToolResult(True, full_output, priority="low")
        return ToolResult(False, full_output, f"exit_code={exit_code}")

    async def _execute_local(
        self, context, command: str, timeout: int
    ) -> ToolResult:
        proc = None  # C-review M9：预初始化，CancelledError 分支可安全引用
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.workspace_path or None,
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
        except asyncio.CancelledError:
            # C-review M9：客户端断连时 engine task 被取消。CancelledError 是
            # BaseException，不会被下方 except Exception 捕获——不显式 kill
            # 子进程就会孤儿化继续执行（"用户已停止"的 run 仍在改文件）。
            # 此处不等子进程退出，避免拖住断连清理路径。
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass  # 竞态：进程已退出
            raise
        except Exception as e:
            return ToolResult(False, "", str(e))
