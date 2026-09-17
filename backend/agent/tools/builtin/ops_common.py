"""运维工具共享执行辅助 — 远程无头执行与输出保护。

供 ops_obs / ops_service / ops_file / ops_image / shell_exec 及 harness 侧
（saddle 的撤销快照）复用，统一经 Electron Bridge 在 SSH 会话上执行命令。
路径校验（is_traversal / is_remote / strip_remote_tail）也收敛于此，
避免各工具文件各抄一份产生行为漂移。

安全约束（P0 fail-closed）：本模块**不包含任何本地子进程执行分支**。
缺少 SSH 会话或 Bridge 不可用时，run_headless 直接返回失败，
绝不降级到后端主机本机执行。
"""
import asyncio
import base64
import os
from agent.electron_bridge import ElectronBridge
from agent.truncation_limits import TRUNC_THRESHOLD, TRUNC_HEAD, TRUNC_TAIL


def truncate_output(output: str, threshold: int = TRUNC_THRESHOLD) -> str:
    """大输出保护：超过阈值时保留头尾，中间省略。"""
    if len(output) <= threshold:
        return output
    head = output[:TRUNC_HEAD]
    tail = output[-TRUNC_TAIL:]
    omitted = len(output) - TRUNC_HEAD - TRUNC_TAIL
    return head + f"\n... [输出已截断，省略中间 {omitted} 字符]\n" + tail


def is_traversal(path) -> bool:
    """路径是否含 `..` 越界访问。"""
    if not isinstance(path, str) or not path:
        return True
    for sep in (os.sep, "/"):
        if ".." in path.split(sep):
            return True
    return False


def is_remote(context) -> bool:
    """是否远程执行（有 SSH 会话）。"""
    return bool(getattr(context, "ssh_session_id", ""))


def strip_remote_tail(output: str) -> str:
    """剥离 run_headless/exec_remote 追加的 [exit_code=..] / [stderr] 尾部哨兵行。

    base64 字符集（A-Za-z0-9+/=）不含 '['，因此 base64 回传场景中尾部哨兵行
    可被无损剥离，不会与真实文件内容产生歧义。
    """
    lines = output.splitlines()
    while lines:
        last = lines[-1]
        if last.startswith("[exit_code=") or last == "[stderr]":
            lines.pop()
            continue
        break
    return "\n".join(lines)


async def exec_remote(
    ssh_session_id: str, command: str, timeout: int
) -> tuple[bool, str, int | None, str] | None:
    """在指定 SSH 会话上执行命令，返回 (success, 组装后输出, exit_code, 执行异常信息)。

    输出组装规则（单一实现源，run_headless 与 shell_exec 共用）：
    stdout + 可选 "\\n[stderr]\\n..." + 可选 "\\n[exit_code=..]"，再统一截断。
    success 判定：bridge 报 success 且 exit_code == 0。

    返回 None 表示 Bridge 不可用（未配置端口/令牌）——由调用方决定
    面向用户的文案；err 仅在 bridge 调用抛出异常时非空（此时 output 为空），
    调用方按各自契约决定该信息放 output 还是 error 字段。
    """
    bridge = ElectronBridge()
    if not bridge.is_available():
        return None

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
    except Exception as e:
        return False, "", None, f"远程命令执行失败: {e}"

    output = (result.get("output", "") or "")
    error = (result.get("error", "") or "")
    exit_code = result.get("exit_code")
    full = output
    if error:
        full += f"\n[stderr]\n{error}"
    if exit_code is not None:
        full += f"\n[exit_code={exit_code}]"
    success = bool(result.get("success", False)) and exit_code == 0
    return success, truncate_output(full), exit_code, ""


async def run_headless(
    context, command: str, timeout: int = 30
) -> tuple[bool, str]:
    """在远程 SSH 会话上无头执行一条命令，返回 (success, output)。

    安全（P0 fail-closed）：运维工具（ops_*）语义上只面向远程目标机。
    下列任一条件缺失即拒绝执行、返回失败，**绝不降级到后端主机本机执行**：

      - 缺少 SSH 会话（ssh_session_id 为空）
      - Electron Bridge 未就绪（端口/令牌未注入，或回环不可达）

    原实现在 `ssh_session_id 有值但 bridge 不可用` 时静默 fall through 到
    本地子进程执行 —— 即故障态反而比正常态更危险（本该发往远程的命令
    改到了后端主机本机）。改为 fail-closed 后，该降级路径被彻底移除。

    注意：本函数不再包含任何本地子进程执行分支。
    """
    timeout = max(1, min(int(timeout), 120))
    ssh_session_id = getattr(context, "ssh_session_id", "")

    if not ssh_session_id:
        return False, "拒绝执行：缺少 SSH 会话，运维工具仅限远程 SSH 会话（未在后端主机本机执行）"

    result = await exec_remote(ssh_session_id, command, timeout)
    if result is None:
        return False, (
            "拒绝执行（fail-closed）：Electron Bridge 未就绪，"
            "本命令不会降级到后端主机本机执行。请检查 SSH 连接与桥接状态。"
        )
    success, output, _exit_code, err = result
    if err:
        return False, err
    return success, output

def base64_read_cmd(path: str) -> str:
    """构造 base64 读文件命令（remote_read_b64 与 saddle 预变更快照共用模板）。"""
    import shlex
    return f"base64 {shlex.quote(path)}"


def file_delete_cmd(path: str) -> str:
    """构造删除文件命令（FileContentUndo 还原新建文件用，saddle 与契约测试共用）。"""
    import shlex
    return f"rm -f {shlex.quote(path)}"


def base64_write_cmd(path: str, data: bytes) -> str:
    """构造 base64 写文件命令（base64 仅含字母数字+/=+，无需转义；路径 shlex.quote）。"""
    b64 = base64.b64encode(data).decode("ascii")
    return base64_restore_cmd(path, b64)


def base64_restore_cmd(path: str, b64: str) -> str:
    """以既有 base64 文本构造还原写盘命令（ops_file 写入与 saddle 撤销共用模板）。"""
    import shlex
    return f"printf '%s' '{b64}' | base64 -d > {shlex.quote(path)}"


def drop_sentinel_lines(output: str) -> list[str]:
    """剔除 run_headless 输出中的哨兵/空行，保留真实数据行（glob/grep 列表解析共用）。"""
    return [
        line
        for line in output.splitlines()
        if line.strip() and "[exit_code=" not in line and line != "[stderr]"
    ]


async def remote_read_b64(context, path: str) -> tuple[bool, bytes]:
    """远程以 base64 读取文件原始字节（哨兵经 strip_remote_tail 无损剥离：
    base64 字符集不含 '['，与尾部 "[exit_code=..]" 天然无歧义）。

    失败时 payload 为错误信息字节（沿用 run_headless 的失败输出）。
    """
    ok, output = await run_headless(context, base64_read_cmd(path), 30)
    if not ok:
        return False, output.encode("utf-8")
    body = strip_remote_tail(output)
    try:
        data = base64.b64decode(body)
    except Exception:
        return False, b""
    return True, data
