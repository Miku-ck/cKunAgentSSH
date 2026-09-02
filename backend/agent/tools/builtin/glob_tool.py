"""
文件查找工具 — 按 glob 模式递归查找文件

安全约束: 路径必须在 workspace_path 内
"""
import os
import glob as glob_module
import asyncio
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.file_ops import _resolve_path_safe

# 默认排除的目录（glob 结果中过滤）
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", "dist", "build", ".pytest_cache",
}


class GlobTool(BaseTool):
    """递归文件查找工具

    按 glob 模式递归查找文件（如 **/*.py）。
    - 支持 ** 递归通配符
    - 返回相对路径列表
    - 自动排除 node_modules、.git、__pycache__ 等噪声目录
    """

    name = "glob"
    description = "按 glob 模式递归查找文件（如 **/*.py）。返回匹配的文件路径列表。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式（支持 ** 递归）"},
            "path": {"type": "string", "description": "搜索根目录（默认工作区根目录）"},
            "max_results": {"type": "integer", "description": "最大返回文件数（默认 100）"},
        },
        "required": ["pattern"],
    }
    # C-review M14：本地文件查找工具 — 仅本地模式（work/ide）可用（ssh 模式走 shell_exec 的 find）
    meta = ToolMeta(risk_level="safe", reversible=False, idempotent=True,
                    mode_scope=["work", "ide"])  # 只读查找，可安全重试

    async def execute(self, context, pattern, path=None, max_results=100):
        # 审查 MINOR 修复：pattern 逃逸防护。原实现 `os.path.isabs(pattern)` 时直接用
        # 绝对 pattern 搜索 — LLM 传入 C:\\ 或 /etc 下绝对路径即可全盘枚举；
        # 含 .. 的相对 pattern（如 ../../*.txt）同样能跳出 root。
        norm_pattern = (pattern or "").replace("\\", "/")
        if os.path.isabs(pattern) or norm_pattern.startswith("/"):
            return ToolResult(False, "", "pattern 不允许绝对路径，请使用相对模式", priority="high")
        if any(part == ".." for part in norm_pattern.split("/")):
            return ToolResult(False, "", "pattern 不允许包含 ..（无法跳出搜索根目录）", priority="high")
        # 审查 MINOR 修复：max_results 钳制（原实现无上限，LLM 可传巨大值）
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = 100
        max_results = max(1, min(max_results, 500))

        # 解析搜索根目录
        root = path or context.workspace_path or "."
        try:
            full_root = _resolve_path_safe(root, context.workspace_path)
        except ValueError as e:
            return ToolResult(False, "", str(e), priority="high")

        if not os.path.isdir(full_root):
            return ToolResult(False, "", f"目录不存在: {path or '(工作区根目录)'}", priority="high")

        try:
            matches = await asyncio.to_thread(
                self._glob_search, pattern, full_root, max_results, context.workspace_path,
            )
        except Exception as e:
            return ToolResult(False, "", f"查找失败: {e}", priority="high")

        if not matches:
            return ToolResult(True, "未找到匹配文件", priority="low")

        output = "\n".join(matches)
        if len(matches) >= max_results:
            output += f"\n... [已达 max_results={max_results} 上限，可能还有更多匹配]"
        return ToolResult(True, output, priority="low")

    def _glob_search(self, pattern, root, max_results, workspace):
        """执行 glob 查找并过滤排除目录"""
        # 构建完整 glob 模式（审查 MINOR：绝对 pattern 已在 execute 拒绝，此处恒为相对拼接）
        full_pattern = os.path.join(root, pattern.replace("\\", os.sep))

        # 递归查找
        raw_matches = glob_module.glob(full_pattern, recursive=True)

        # 过滤排除目录、去重、转为相对路径
        results = []
        seen = set()
        for m in raw_matches:
            # 只保留文件，跳过目录
            if os.path.isdir(m):
                continue
            # 跳过排除目录内的文件
            parts = m.replace("/", os.sep).split(os.sep)
            if any(skip in parts for skip in _SKIP_DIRS):
                continue
            # 转为相对路径
            rel = m
            if workspace:
                try:
                    rel = os.path.relpath(m, workspace)
                except ValueError:
                    rel = m
            if rel not in seen:
                seen.add(rel)
                results.append(rel)
                if len(results) >= max_results:
                    break
        return results
