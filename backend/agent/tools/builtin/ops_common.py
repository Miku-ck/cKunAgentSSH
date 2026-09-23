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
from agent.remote_protocol import (
    EXIT_CODE_PREFIX,
    STDERR_SENTINEL,
    has_sentinel_fragment,
    is_sentinel_line,
)


def truncate_output(output: str, threshold: int = TRUNC_THRESHOLD) -> str:
    """大输出保护：超过阈值时保留头尾，中间省略。"""
    if len(output) <= threshold:
        return output
    head = output[:TRUNC_HEAD]
    tail = output[-TRUNC_TAIL:]
    omitted = len(output) - TRUNC_HEAD - TRUNC_TAIL
    return head + f"\n... [输出已截断，省略中间 {omitted} 字符]\n" + tail


def parse_int_arg(raw, name: str, default: int | None = None) -> tuple[int | None, str]:
    """把模型填的数字参数转成 int，返回 (值, 错误信息)；错误非空时值为 None。

    各工具的 limit/lines/port/timeout 槽位由模型填写，原先一律裸 `int(raw)`：
    填成 "30s"、"" 或 null 时抛 ValueError/TypeError 冒到驱动，回给模型的是一句
    裸栈而不是一句「这个参数该怎么填」。区间策略仍由各调用方自己决定（有的钳制、
    有的拒绝），这里只负责解析。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if default is None:
            return None, f"{name} 需为整数，收到 {raw!r}"
        return default, ""
    try:
        return int(raw), ""
    except (TypeError, ValueError):
        return None, f"{name} 需为整数，收到 {raw!r}"


def clamp_int(raw, default: int, low: int, high: int) -> int:
    """转 int 并钳到 [low, high]，缺失/非法一律回 default。

    供 bound_command（判定视图）使用：那里绝不许抛，且必须与 execute 生成同一条
    命令串 —— 授权键是 sha256(命令)，两边各写一份钳制规则就会漂移（max_lines=0
    曾一边折成 200、一边钳成 1，预授权因此永不命中）。
    """
    parsed, _err = parse_int_arg(raw, "", default)
    return max(low, min(parsed if parsed is not None else default, high))


def is_traversal(path) -> bool:
    """路径是否含 `..` 越界访问。"""
    if not isinstance(path, str) or not path:
        return True
    for sep in (os.sep, "/"):
        if ".." in path.split(sep):
            return True
    return False


# 越界路径拒绝时的统一用户提示（ops_file/ops_image/ops_obs 各 is_traversal 分支共用，
# 防文案漂移）。裸 return 与 ToolResult(False, "", ...) 两种返回形态都取此值。
TRAVERSAL_ERROR = "非法路径：包含 `..`，禁止越界访问"


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
        if is_sentinel_line(last):
            lines.pop()
            continue
        break
    return "\n".join(lines)


async def exec_remote(
    ssh_session_id: str, command: str, timeout: int, keep_full: bool = False
) -> tuple[bool, str, int | None, str] | None:
    """在指定 SSH 会话上执行命令，返回 (success, 组装后输出, exit_code, 执行异常信息)。

    输出组装规则（单一实现源，run_headless 与 shell_exec 共用）：
    stdout + 可选 "\\n[stderr]\\n..." + 可选 "\\n[exit_code=..]"，再统一截断。
    success 判定：bridge 报 success 且 exit_code == 0。

    keep_full=True 供**字节协议**通道使用（base64 回传文件内容、增量日志 tail）：
    头尾保留截断是为「给人看的命令输出」设计的，插进协议载荷即损坏数据 ——
    base64 串里混入中文标记行会直接解码失败（file_edit 因此对 >2.9KB 的文件报
    「文件不存在」），日志 tail 的 offset 记账会与实际字节错位（下一轮重复读同一段）。
    走这条路等于放弃长度兜底，上限由调用方自己显式设定（见 remote_read_b64）。

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
        full += f"\n{STDERR_SENTINEL}\n{error}"
    if exit_code is not None:
        full += f"\n{EXIT_CODE_PREFIX}{exit_code}]"
    success = bool(result.get("success", False)) and exit_code == 0
    return success, (full if keep_full else truncate_output(full)), exit_code, ""


async def run_headless(
    context, command: str, timeout: int = 30, keep_full: bool = False
) -> tuple[bool, str]:
    """在远程 SSH 会话上无头执行一条命令，返回 (success, output)。

    keep_full=True 的语义见 exec_remote：字节协议通道（base64 回传内容、
    增量日志 tail）必须用它，否则回传载荷会被头尾保留截断损坏。

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

    result = await exec_remote(ssh_session_id, command, timeout, keep_full=keep_full)
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


# 写盘命令的「判定视图」占位 payload。PRE_TOOL_CALL 拦截器只看到 args={path,content}，
# 看不到本模板 — 于是 file_write 写 authorized_keys 曾按工具级 moderate 放行，而等价
# shell 重定向判 hardline。BaseTool.bound_command 把真实命令串交给 FilesystemGuard，
# 两条路径自此共用同一个 classify_* 判定。
_CLASSIFY_PAYLOAD = "BASE64_CONTENT_PAYLOAD"


def base64_write_cmd_for_classification(path: str) -> str:
    """与 base64_restore_cmd 同模板，仅 payload 换成固定占位符。

    用占位符而非真实内容：授权键 = sha256(规范化命令)，含 payload 则改一个字节就要
    重新授权、"始终允许"彻底失效，卡片 payload 也会爆大。风险由**目标路径**决定，
    占位符使授权键的含义收敛为"允许写这个路径"，与产品语义一致。
    """
    import shlex
    return f"printf '%s' '{_CLASSIFY_PAYLOAD}' | base64 -d > {shlex.quote(path)}"


def drop_sentinel_lines(output: str) -> list[str]:
    """剔除 run_headless 输出中的哨兵/空行，保留真实数据行（glob/grep 列表解析共用）。"""
    return [
        line
        for line in output.splitlines()
        if line.strip() and not has_sentinel_fragment(line)
    ]


# base64 回传的显式载荷上限（原始字节）。原先这道保护是「靠 exec_remote 的 4000
# 字符截断兜底」，但对协议载荷而言截断不是限制而是损坏（中文标记行混进 base64 →
# 解码抛异常 → file_edit 报「文件不存在」、saddle 快照静默丢失）。约 200KB 的文本
# 配置已远超真实编辑场景，超出即明确拒绝，让调用方拿到可转述的理由。
B64_READ_MAX_BYTES = 200 * 1024


async def remote_read_b64(context, path: str) -> tuple[bool, bytes]:
    """远程以 base64 读取文件原始字节（keep_full：哨兵经 strip_remote_tail 无损
    剥离，base64 字符集不含 '['，与尾部 "[exit_code=..]" 天然无歧义）。

    失败时 payload 是一段「为什么失败」的文本（run_headless 的失败输出，或
    本函数自己的超限/解码结论），调用方原样转述即可。
    """
    ok, output = await run_headless(context, base64_read_cmd(path), 30, keep_full=True)
    if not ok:
        return False, output.encode("utf-8")
    body = strip_remote_tail(output)
    try:
        data = base64.b64decode(body)
    except Exception:
        return False, "回传内容不是合法 base64（桥接通道返回形态异常），已放弃读取".encode("utf-8")
    if len(data) > B64_READ_MAX_BYTES:
        return False, (
            f"文件 {len(data)} 字节，超过 base64 回读上限 {B64_READ_MAX_BYTES} 字节，"
            "未取回内容（整体改写请用 file_write 或分段处理）"
        ).encode("utf-8")
    return True, data
