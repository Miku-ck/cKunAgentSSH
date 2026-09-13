"""
Shell 执行工具 — SSH 模式专属

安全约束: 30 秒超时，非破坏性命令自主执行
"""
import asyncio
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.electron_bridge import ElectronBridge
from agent.pending_delivery import PendingDeliveries

# 大输出保护阈值（C-review NIT：单一数据源 agent.truncation_limits）
from agent.truncation_limits import TRUNC_THRESHOLD, TRUNC_HEAD, TRUNC_TAIL
_SHELL_OUTPUT_THRESHOLD = TRUNC_THRESHOLD
_SHELL_OUTPUT_HEAD = TRUNC_HEAD
_SHELL_OUTPUT_TAIL = TRUNC_TAIL


def _compute_requires_confirm(command: str, environment: str) -> bool:
    """计算可见终端模式下该命令是否需要用户确认。

    规则：非 safe 命令（dangerous/hardline）或生产环境（prod）都需要确认。
    """
    from agent.harness.safety import classify_command
    level, _reason = classify_command(command)
    return (level != "safe") or (environment == "prod")


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
        # 分支顺序：
        #   1. 可见终端模式 + 存在 SSH 会话 → 交付到可见终端（不 headless 执行）
        #   2. SSH 会话 → 原有远程执行（ElectronBridge）
        #   3. 否则 → 原有本地子进程执行
        visible = getattr(context, "visible_terminal", False)
        if visible and context.ssh_session_id:
            return self._execute_visible(context, command)
        if context.ssh_session_id:
            return await self._execute_remote(context.ssh_session_id, command, timeout)
        return await self._execute_local(context, command, timeout)

    def _execute_visible(self, context, command: str) -> ToolResult:
        """可见终端模式：登记待交付命令，返回 pending_delivery 让 Driver 挂起等待。

        不在此处执行命令（headless 关闭），命令由前端写入真实 PTY/SSH 会话后，
        经 POST /api/agent/tool-result 回传结果恢复循环。
        """
        environment = getattr(context, "environment", "test")
        requires_confirm = _compute_requires_confirm(command, environment)
        pending = PendingDeliveries.create(
            command=command,
            session_id=context.ssh_session_id,
            requires_confirm=requires_confirm,
            run_id=getattr(context, "agent_run_id", ""),
        )
        return ToolResult(
            success=False,
            output="",
            priority="high",
            pending_delivery={
                "tool_id": pending.tool_id,
                "command": command,
                "session_id": pending.session_id,
                "requires_confirm": requires_confirm,
            },
        )

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
