"""运维工具共享执行辅助 — 无头执行（远程桥接 / 本地子进程）与输出保护。

供 ops_obs / ops_service / ops_schedule 等只读与设施管理工具复用，
避免在每个工具里重复实现 ssh/本地执行与截断逻辑。
"""
import asyncio
from agent.electron_bridge import ElectronBridge
from agent.truncation_limits import TRUNC_THRESHOLD, TRUNC_HEAD, TRUNC_TAIL


class OpsError(Exception):
    """运维工具执行异常（桥接不可用 / 命令失败等）。"""


def truncate_output(output: str, threshold: int = TRUNC_THRESHOLD) -> str:
    """大输出保护：超过阈值时保留头尾，中间省略。"""
    if len(output) <= threshold:
        return output
    head = output[:TRUNC_HEAD]
    tail = output[-TRUNC_TAIL:]
    omitted = len(output) - TRUNC_HEAD - TRUNC_TAIL
    return head + f"\n... [输出已截断，省略中间 {omitted} 字符]\n" + tail


def _bridge_available() -> bool:
    try:
        return ElectronBridge().is_available()
    except Exception:
        return False


async def run_headless(
    context, command: str, timeout: int = 30
) -> tuple[bool, str]:
    """在远程 SSH（如有会话）或本地执行一条命令，返回 (success, output)。

    分支：
      1. context.ssh_session_id 且桥接可用 → 远程 ElectronBridge 执行
      2. 否则 → 本地子进程执行（cwd=workspace_path）
    """
    timeout = max(1, min(int(timeout), 120))
    ssh_session_id = getattr(context, "ssh_session_id", "")

    if ssh_session_id and _bridge_available():
        bridge = ElectronBridge()
        try:
            result = await bridge.exec_ssh_command(
                ssh_session_id=ssh_session_id,
                command=command,
                timeout=float(timeout),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return False, f"远程命令执行失败: {e}"

        output = (result.get("output", "") or "")
        error = (result.get("error", "") or "")
        exit_code = result.get("exit_code")
        full = output
        if error:
            full += f"\n[stderr]\n{error}"
        if exit_code is not None:
            full += f"\n[exit_code={exit_code}]"
        success = bool(result.get("success", False)) and exit_code == 0
        return success, truncate_output(full)

    # 本地子进程执行
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=getattr(context, "workspace_path", None) or None,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        full = ""
        if stdout:
            full += stdout.decode("utf-8", errors="replace")
        if stderr:
            full += f"\n[stderr]\n{stderr.decode('utf-8', errors='replace')}"
        full += f"\n[exit_code={proc.returncode}]"
        return proc.returncode == 0, truncate_output(full)
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        return False, f"命令执行超时（{timeout}秒），已终止"
    except asyncio.CancelledError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    except Exception as e:
        return False, str(e)