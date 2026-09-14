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
    is_remote,
    is_traversal,
    run_headless,
    strip_remote_tail,
)

# 最大允许文件大小（字节，10MB）
MAX_IMAGE_BYTES = 10 * 1024 * 1024

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
        mode_scope=["ssh"],
    )

    def _validate(self, path):
        if is_traversal(path):
            return "非法路径：包含 `..`，禁止越界访问"
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
            qpath = shlex.quote(path)
            ok, output = await run_headless(context, f"base64 -i {qpath}", 20)
            if not ok:
                return ToolResult(False, output, "远程读取图片失败")
            b64 = re.sub(r"\s+", "", strip_remote_tail(output))
            if not b64:
                return ToolResult(False, "", "远程读取图片为空或文件不存在")
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