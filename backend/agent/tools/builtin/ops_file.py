"""运维文件操作工具集 — SSH 模式专属。

包含五个 BaseTool 子类：
  - file_read  ：带行号读取文件片段（offset/limit）
  - file_write ：新建或整体覆写文件（base64 传输，避免注入）
  - file_edit  ：精确字符串替换（要求 old_string 恰好出现 1 次）
  - file_glob  ：按 glob 模式在目录下递归查找文件
  - file_grep  ：按正则递归搜索文件内容

安全约束：
  1. 所有路径均校验绝对路径归一化后不得包含 `..`，杜绝越界访问；
  2. 远程命令一律经 shlex.quote 转义（file_write/编辑用 base64，天然不必转义）；
  3. **写/编辑（file_write/file_edit）仅限远程 SSH 会话**：与 shell_exec、
     ops_common.run_headless 同一 fail-closed 口径 — 缺少 SSH 会话即拒绝，
     绝不降级到后端主机本机落盘（本产品 AI 操作的对象是远程服务器，
     在后端主机写文件没有合法场景）；
  4. 读取类（file_read/file_glob/file_grep/read_image）保留本地分支，
     服务本地终端场景的查看/诊断（Python 原生文件 API，只读无突变）；
  5. 大输出经 truncate_output 截断，防刷屏。
"""
import pathlib
import re
import shlex

from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.ops_common import (
    base64_read_cmd,
    base64_write_cmd,
    base64_write_cmd_for_classification,
    clamp_int,
    drop_sentinel_lines,
    is_remote,
    is_traversal,
    parse_int_arg,
    remote_read_b64,
    run_headless,
    strip_remote_tail,
    truncate_output,
)

# ── 远端命令构建（单一事实来源）──────────────────────────────
# 这些函数是客户端→远端白名单执行器（scripts/deploy/agent-exec.py）的契约源：
# 远端 wrapper 的正则白名单必须逐条接受这里生成的命令串，
# 由 backend/tests/test_remote_whitelist_contract.py 跨端校验。

def file_read_cmd(path: str, offset: int, limit: int) -> str:
    """file_read 远端模板：带行号读取 [offset, offset+limit-1] 片段。"""
    qpath = shlex.quote(path)
    start, stop = offset, offset + limit - 1
    return f"nl -ba {qpath} | sed -n '{start},{stop}p'"


def file_glob_cmd(path: str, pattern: str) -> str:
    """file_glob 远端模板：递归按 glob 名查找文件。"""
    qpath, qpat = shlex.quote(path), shlex.quote(pattern)
    return f"find {qpath} -name {qpat} -type f"


def file_grep_cmd(path: str, pattern: str, max_lines: int) -> str:
    """file_grep 远端模板：正则递归搜索 + 限行。"""
    qpat, qpath = shlex.quote(pattern), shlex.quote(path)
    return f"grep -rnE --color=never {qpat} {qpath} | head -n {max_lines}"


_WRITE_REQUIRES_SSH = (
    "拒绝执行（fail-closed）：缺少 SSH 会话，文件写入/编辑仅限远程目标服务器，"
    "不会在后端主机本机落盘。请先建立 SSH 连接，或确认操作目标。"
)


def _write_bound_command(args: dict) -> str | None:
    """file_write / file_edit 共用：把真实落盘的 base64 写模板暴露给内容级判定。

    没有这一步，写 /root/.ssh/authorized_keys 只按 meta.risk_level=moderate 处理
    （会话级授权即可，点"始终允许"后含 prod 永久免审），而等价的手敲重定向会被
    safety.classify_command 判 hardline —— 同一副作用必须同一结论。
    """
    path = args.get("path")
    if not isinstance(path, str) or not path or is_traversal(path):
        return None
    return base64_write_cmd_for_classification(path)


class FileReadTool(BaseTool):
    name = "file_read"
    description = "带行号读取文件指定片段（offset/limit，offset 从 1 开始）。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的文件绝对路径"},
            "offset": {
                "type": "integer",
                "description": "起始行号（>=1，默认 1）",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "读取行数（1-10000，默认 200）",
                "default": 200,
            },
        },
        "required": ["path"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        """读操作按 base64 读模板判定（remote_read_b64/saddle 快照共用该模板）。

        meta.risk_level=safe 只表达"读本身可逆"；读到凭据文件必须与 `cat /etc/shadow`
        同等对待，否则 AI 可用 file_read 免审取走 shadow/私钥，而手敲等价命令要审批。
        """
        path = args.get("path")
        if not isinstance(path, str) or not path or is_traversal(path):
            return None
        return base64_read_cmd(path)

    def _validate(self, path, offset, limit):
        if is_traversal(path):
            return "非法路径：包含 `..`，禁止越界访问"
        if offset < 1:
            return "offset 必须为 >=1 的整数"
        if limit < 1:
            return "limit 必须为 >=1 的整数"
        return None

    async def execute(self, context, path, offset=1, limit=200):
        # 模型可以把 offset/limit 填成 "200"/null/""：裸 int() 会把 ValueError
        # 抛给驱动，模型只看到一句栈；这里换成「这个参数该怎么填」的可读提示
        offset, oerr = parse_int_arg(offset, "offset", 1)
        limit, lerr = parse_int_arg(limit, "limit", 200)
        if oerr or lerr:
            return ToolResult(False, "", oerr or lerr)
        limit = min(10000, limit)
        err = self._validate(path, offset, limit)
        if err:
            return ToolResult(False, "", err)

        if is_remote(context):
            cmd = file_read_cmd(path, offset, limit)
            ok, output = await run_headless(context, cmd, self.meta.timeout)
            if not ok:
                return ToolResult(False, output, "读取文件失败")
            body = strip_remote_tail(output)
            if not body.strip():
                return ToolResult(True, f"无内容（offset {offset} 超出文件末尾或文件为空）")
            return ToolResult(True, body)

        # 本地（Windows）：Python 原生文件 API
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except FileNotFoundError:
            return ToolResult(False, "", f"文件不存在: {path}")
        except PermissionError:
            return ToolResult(False, "", f"无权限访问文件: {path}")
        except IsADirectoryError:
            return ToolResult(False, "", f"路径是目录而非文件: {path}")
        except Exception as e:
            return ToolResult(False, "", f"读取文件失败: {e}")

        begin = offset - 1
        selected = all_lines[begin : begin + limit]
        if not selected:
            return ToolResult(True, f"无内容（offset {offset} 超出文件末尾）")
        out_lines = [
            f"{lineno}: {ln.rstrip()}"
            for lineno, ln in enumerate(selected, start=offset)
        ]
        return ToolResult(True, truncate_output("\n".join(out_lines)))


class FileWriteTool(BaseTool):
    name = "file_write"
    description = "新建或整体覆写文件内容（非追加；覆盖已有内容）。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件绝对路径"},
            "content": {"type": "string", "description": "要写入的完整文件内容"},
        },
        "required": ["path", "content"],
    }
    meta = ToolMeta(
        risk_level="moderate",
        reversible=False,
        idempotent=True,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        return _write_bound_command(args)

    async def execute(self, context, path, content):
        if is_traversal(path):
            return ToolResult(False, "", "非法路径：包含 `..`，禁止越界访问")
        if not is_remote(context):
            return ToolResult(False, "", _WRITE_REQUIRES_SSH)
        content = content if isinstance(content, str) else str(content)
        data = content.encode("utf-8")

        ok, output = await run_headless(
            context, base64_write_cmd(path, data), self.meta.timeout
        )
        if not ok:
            return ToolResult(False, output, "远程写入文件失败")
        return ToolResult(True, f"已写入 {len(data)} 字节 -> {path}")


class FileEditTool(BaseTool):
    name = "file_edit"
    description = "精确字符串替换：old_string 必须恰好出现 1 次，替换为新内容后写回。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要编辑的文件绝对路径"},
            "old_string": {"type": "string", "description": "要被替换的旧字符串（须恰好出现 1 次）"},
            "new_string": {"type": "string", "description": "替换后的新字符串"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    meta = ToolMeta(
        risk_level="moderate",
        reversible=False,
        idempotent=False,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        return _write_bound_command(args)

    @staticmethod
    def _apply_replace(content, old, new):
        count = content.count(old)
        if count == 0:
            return ToolResult(False, "", f"old_string 在文件中出现 0 次，无法替换")
        if count > 1:
            return ToolResult(
                False, "", f"old_string 在文件中出现 {count} 次，要求恰好 1 次，拒绝替换"
            )
        return content.replace(old, new)

    async def execute(self, context, path, old_string, new_string):
        if is_traversal(path):
            return ToolResult(False, "", "非法路径：包含 `..`，禁止越界访问")
        if not isinstance(old_string, str) or old_string == "":
            return ToolResult(False, "", "old_string 不能为空")
        if not is_remote(context):
            return ToolResult(False, "", _WRITE_REQUIRES_SSH)

        ok, raw = await remote_read_b64(context, path)
        if not ok:
            # 把远端/协议给出的真实理由转述给模型：固定成「或文件不存在」会把
            # 超限、权限、通道异常一律误导成文件不存在，模型随即放弃编辑或改用
            # file_write 整体覆写（它手里只有 file_read 截断过的片段 → 覆盖即丢内容）
            reason = raw.decode("utf-8", errors="replace").strip()
            return ToolResult(False, "", f"远程读取原文件失败：{reason or '文件不存在或不可读'}")
        content = raw.decode("utf-8", errors="replace")
        new_content = self._apply_replace(content, old_string, new_string)
        if isinstance(new_content, ToolResult):
            return new_content
        data = new_content.encode("utf-8")
        ok2, out2 = await run_headless(
            context, base64_write_cmd(path, data), self.meta.timeout
        )
        if not ok2:
            return ToolResult(False, out2, "远程写入文件失败")
        return ToolResult(True, "已替换 1 处")


class FileGlobTool(BaseTool):
    name = "file_glob"
    description = "按 glob 模式在指定目录下递归查找文件，返回匹配文件列表。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "起始目录绝对路径"},
            "pattern": {"type": "string", "description": "glob 匹配模式（相对该目录或绝对均可）"},
        },
        "required": ["path", "pattern"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    async def execute(self, context, path, pattern):
        if is_traversal(path):
            return ToolResult(False, "", "非法路径：包含 `..`，禁止越界访问")

        if is_remote(context):
            cmd = file_glob_cmd(path, pattern)
            ok, output = await run_headless(context, cmd, self.meta.timeout)
            if not ok:
                return ToolResult(False, output, "glob 查找失败")
            lines = drop_sentinel_lines(output)
            if not lines:
                return ToolResult(True, f"未匹配到文件：{pattern}")
            return ToolResult(True, "\n".join(lines))

        # 本地：pathlib 递归查找
        base = pathlib.Path(path)
        if not base.is_dir():
            return ToolResult(False, "", f"起始目录不存在或不是目录: {path}")
        pat = pattern.lstrip("/\\")
        try:
            matches = sorted(
                str(p.resolve()) for p in base.rglob(pat) if p.is_file()
            )
        except Exception as e:
            return ToolResult(False, "", f"glob 查找失败: {e}")
        if not matches:
            return ToolResult(True, f"未匹配到文件：{pattern}")
        return ToolResult(True, "\n".join(matches))


class FileGrepTool(BaseTool):
    name = "file_grep"
    description = "按正则（RE2 风格）在文件或目录下递归搜索内容，返回匹配行。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要搜索的文件或目录绝对路径"},
            "pattern": {"type": "string", "description": "正则表达式"},
            "max_lines": {
                "type": "integer",
                "description": "最多返回匹配行数（1-5000，默认 200）",
                "default": 200,
            },
        },
        "required": ["path", "pattern"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        """grep 会把匹配行原样返回 —— 与 file_read 同一条凭据外泄通道，同口径判定。

        file_glob 不纳入：`find -name` 只返回路径名，不外泄文件内容。
        """
        path = args.get("path")
        if not isinstance(path, str) or not path or is_traversal(path):
            return None
        return file_grep_cmd(
            path, str(args.get("pattern") or ""), clamp_int(args.get("max_lines"), 200, 1, 5000)
        )

    async def execute(self, context, path, pattern, max_lines=200):
        if is_traversal(path):
            return ToolResult(False, "", "非法路径：包含 `..`，禁止越界访问")
        max_lines, err = parse_int_arg(max_lines, "max_lines", 200)
        if err:
            return ToolResult(False, "", err)
        max_lines = min(5000, max(1, max_lines))

        if is_remote(context):
            cmd = file_grep_cmd(path, pattern, max_lines)
            ok, output = await run_headless(context, cmd, self.meta.timeout)
            lines = drop_sentinel_lines(output)
            if not lines:
                if not ok and output.strip():
                    return ToolResult(False, output, "grep 查找失败")
                return ToolResult(True, f"未匹配到内容：{pattern}")
            return ToolResult(True, truncate_output("\n".join(lines)))

        # 本地：Python 正则逐行搜索
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(False, "", f"正则表达式无效: {e}")

        target = pathlib.Path(path)
        if target.is_file():
            files = iter([target])
        elif target.is_dir():
            files = (p for p in sorted(target.rglob("*")) if p.is_file())
        else:
            return ToolResult(False, "", f"路径不存在: {path}")
        base = target if target.is_dir() else target.parent

        results = []
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        text = line.rstrip("\n")
                        if regex.search(text):
                            try:
                                rel = str(fp.relative_to(base))
                            except ValueError:
                                rel = str(fp)
                            results.append(f"{rel}:{lineno}:{text}")
                            if len(results) >= max_lines:
                                break
            except (IsADirectoryError, PermissionError, OSError):
                continue
            if len(results) >= max_lines:
                break

        if not results:
            return ToolResult(True, f"未匹配到内容：{pattern}")
        return ToolResult(True, truncate_output("\n".join(results)))