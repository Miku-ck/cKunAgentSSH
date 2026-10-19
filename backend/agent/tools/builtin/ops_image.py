"""运维图片查看工具 — SSH 模式专属（支持远端/本地双模式）。

提供 1 个 BaseTool 子类：
  - read_image ：读取本地/远程图片，返回 base64 数据 URI（data:mime;base64,...）

安全约束：
  1. 所有路径校验不包含 `..`，杜绝越界访问；
  2. 仅允许 png/jpg/jpeg/webp/gif 扩展名（不区分大小写），规避任意文件读取；
  3. 文件大小上限 10MB：本地用 os.path.getsize 检查；远程按取回的 base64
     长度估算（len * 3 / 4）；
  4. 远程命令经 shlex.quote 转义路径。
"""
import base64
import os
import re
import shlex

from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.ops_common import (
    TRAVERSAL_ERROR,
    is_remote,
    is_traversal,
    run_headless,
    strip_remote_tail,
)

# 契约模板：远端白名单 read_image 动词（base64 -i，容忍换行分隔）。
# 与远端执行器 scripts/deploy/agent-exec.py 的白名单对齐，
# 由 backend/tests/test_remote_whitelist_contract.py 跨端校验。
def image_read_cmd(path: str) -> str:
    """read_image 远端模板：base64 -i 读图片（-i 忽略非字母字符，容忍换行）。"""
    return f"base64 -i {shlex.quote(path)}"

# 最大允许文件大小（字节，10MB）
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# base64 字母表之外的任何字符都说明回传不是完整的字节协议（截断标记、控制字符、
_NON_B64_RE = re.compile(r"[^A-Za-z0-9+/=]")

# 扩展名 -> MIME 映射（不含点，小写）
_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


class ReadImageTool(BaseTool):
    name = "read_image"
    description = "读取图片并返回 base64 数据 URI，供模型分析图片内容（支持 png/jpg/jpeg/webp/gif，上限 10MB）。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的图片绝对路径"},
        },
        "required": ["path"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        timeout=20.0,
    )

    def _validate(self, path):
        if is_traversal(path):
            return TRAVERSAL_ERROR
        ext = os.path.splitext(path)[1].lstrip(".").lower() if isinstance(path, str) else ""
        if ext not in _EXT_MIME:
            return f"不支持的图片格式：.{ext}（仅允许 png/jpg/jpeg/webp/gif）"
        return None

    async def execute(self, context, path):
        err = self._validate(path)
        if err:
            return ToolResult(False, "", err)
        mime = _EXT_MIME[os.path.splitext(path)[1].lstrip(".").lower()]

        if is_remote(context):
            # 契约模板：远端白名单 read_image 动词（base64 -i，容忍换行分隔）
            # 审查修复：base64 是字节协议，必须 keep_full=True——否则 exec_remote 的
            # truncate_output 会把 ≳2.9KB 的图片切成"头+省略标记+尾"，本函数随后 re.sub
            # 去空白只删换行删不掉中文标记，得到内嵌乱码的非法 data URI 却以 success 返回；
            # 且下方按已截断串估长的 10MB 上限永不触发。与 remote_read_b64(ops_common:246)
            # 同源，此前漏改。
            ok, output = await run_headless(
                context, image_read_cmd(path), self.meta.timeout, keep_full=True
            )
            if not ok:
                return ToolResult(False, output, "远程读取图片失败")
            b64 = re.sub(r"\s+", "", strip_remote_tail(output))
            if not b64:
                return ToolResult(False, "", "远程读取图片为空或文件不存在")
            # 回传的字节协议必须整段合法才算成功。主进程 exec 通道另有 1 MiB 上限，
            # 撞线时往 stdout 末尾追加一句截断说明（与 keep_full 无关，剥哨兵也剥不掉它），
            # 去空白后就嵌进 base64 里 —— 旧写法既认不出它，按已截断串估长的 10MB 上限又
            # 永不触发，于是拿非法 base64 拼成 data URI 还回 success=True（S14-03）。
            # 判据只看字符集，不去比对那串中文的措辞：跨端约定字符串会随文案漂移而失效。
            if _NON_B64_RE.search(b64):
                return ToolResult(
                    False, "",
                    "远程图片过大：单次 exec 回传上限约 1 MiB（约合 768 KB 图片），"
                    "通道已把内容截断，剩下的不是合法 base64。请改用 shell_exec 分片读取"
                    "（如 `split -b 512k` 后逐片 base64），或先压缩/缩小图片。",
                )
            # 按 base64 长度估算原始字节数（约 len*3/4）
            if len(b64) * 3 // 4 > MAX_IMAGE_BYTES:
                return ToolResult(False, "", "文件过大：超过 10MB 上限")
            return ToolResult(True, f"data:{mime};base64,{b64}")

        # 本地：Python 原生文件 API（Windows）
        if not os.path.exists(path):
            return ToolResult(False, "", f"文件不存在: {path}")
        try:
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                return ToolResult(False, "", "文件过大：超过 10MB 上限")
            with open(path, "rb") as f:
                data = f.read()
        except PermissionError:
            return ToolResult(False, "", f"无权限访问文件: {path}")
        except IsADirectoryError:
            return ToolResult(False, "", f"路径是目录而非文件: {path}")
        except Exception as e:
            return ToolResult(False, "", f"读取图片失败: {e}")
        b64 = base64.b64encode(data).decode("ascii")
        return ToolResult(True, f"data:{mime};base64,{b64}")