"""运维文件操作工具集 — SSH 模式专属。

包含五个 BaseTool 子类：
  - file_read  ：带行号读取文件片段（offset/limit）
  - file_write ：新建或整体覆写文件（base64 传输，避免注入）
  - file_edit  ：精确字符串替换（要求 old_string 恰好出现 1 次）
  - file_glob  ：按 glob 模式在目录下递归查找文件
  - file_grep  ：按正则递归搜索文件内容

安全约束：
  1. 所有路径均校验绝对路径归一化后不得包含 `..`，杜绝越界访问；
     `file_glob` 的 pattern 同样是路径参数（`rglob` 不阻止 `..` 分段），与 path
     同一校验、同一拒绝文案；远端 `find -name` 天生锚定在起始目录内，两个分支同口径；
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
from agent.remote_protocol import remote_parts
from agent.tools.builtin.ops_common import (
    TRAVERSAL_ERROR,
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
    truncate_output,
)

# ── 远端命令构建（单一事实来源）──────────────────────────────
# 这些函数是客户端→远端白名单执行器（scripts/deploy/agent-exec.py）的契约源：
# 远端 wrapper 的正则白名单必须逐条接受这里生成的命令串，
# 由 backend/tests/test_remote_whitelist_contract.py 跨端校验。

def file_read_cmd(path: str, offset: int, limit: int) -> str:
    """file_read 远端模板：带行号读取 [offset, offset+limit-1] 片段。

    注意这条是**管道**：退出码属于最后一段（sed），`nl` 打不开文件时整条仍然 0 退出，
    所以 `ok`/`exit_code` 不能当"读到了"的证据。失败证据只在 `[stderr]` 段里，
    调用方必须走 `remote_parts` 拆出来看。
    """
    qpath = shlex.quote(path)
    start, stop = offset, offset + limit - 1
    return f"nl -ba {qpath} | sed -n '{start},{stop}p'"


def file_glob_cmd(path: str, pattern: str) -> str:
    """file_glob 远端模板：递归按 glob 名查找文件。"""
    qpath, qpat = shlex.quote(path), shlex.quote(pattern)
    return f"find {qpath} -name {qpat} -type f"


def file_grep_cmd(path: str, pattern: str, max_lines: int) -> str:
    """file_grep 远端模板：正则递归搜索 + 限行。

    同样是管道：退出码取 `head`（恒 0），`grep` 打不开路径时 `ok` 仍然为真。
    注意 grep **无匹配**时不写 stderr、只是退出码 1（也被管道吞掉），所以
    "无匹配"与"路径不存在"两种结果只能靠 `[stderr]` 段区分。
    """
    qpat, qpath = shlex.quote(pattern), shlex.quote(path)
    return f"grep -rnE --color=never {qpat} {qpath} | head -n {max_lines}"


_WRITE_REQUIRES_SSH = (
    "拒绝执行（fail-closed）：缺少 SSH 会话，文件写入/编辑仅限远程目标服务器，"
    "不会在后端主机本机落盘。请先建立 SSH 连接，或确认操作目标。"
)


async def _verify_remote_write(context, path: str, expected: int, timeout: int) -> tuple[bool, str]:
    """写完回验远端实际字节数。返回 (必须判失败, 必须转达给用户/模型的话)；话为空串 = 对上了。

    为什么 `ok` 不足以证明写成功（本文件 `file_read_cmd` 的 docstring 早就写着这条判据，
    读类工具都走了 `remote_parts`，写类工具此前却只看 `ok`）：
    写盘模板是 `printf '%s' '<b64>' | base64 -d > path`，管道的退出码属于**最后一段**，
    而 `> path` 的重定向在 printf 之前就把既有内容截成 0 字节。payload 撞上 ARG_MAX 时
    printf 失败、base64 收到空输入仍然 0 退出 ⇒ 整条 ok/exit_code 全绿、文件已经空了，
    旧实现在这里回「已写入 N 字节」。撤销腿在预快照不可用时是 NoOp（>200KB 就没有快照），
    所以那条失败路径连"可恢复"都算不上。

    回验命令本身失败/读不到数字时**不判失败**（那是"没读到"，不是"写坏了"），但要把
    没验上的事实带出去 —— 静悄悄地把不确定冒充成已验证，就是这条修复要治的那件事。
    """
    ok, out = await run_headless(context, f"wc -c < {shlex.quote(path)}", timeout)
    body, err, rc = remote_parts(out)
    digits = body.strip()
    if not ok or rc != 0 or not digits.isdigit():
        reason = err or ("通道没有回验读数" if not ok else f"读数不是字节数：{body[:40]!r}")
        return False, f"（未能回验远端字节数：{reason}）"
    actual = int(digits)
    if actual == expected:
        return False, ""
    return True, (
        f"写入后回验不符：远端 {path} 实际 {actual} 字节，应为 {expected} 字节。"
        "重定向已把既有内容截断，本次写入没有生效 —— 不要按已写入继续往下推。"
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
            return TRAVERSAL_ERROR
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
            # 模板是管道，`ok`/`rc` 取的是 sed 的退出码 —— nl 打不开文件时整条仍 0 退出，
            # 唯一的故障证据在 [stderr] 段里。不查它就会把读失败报成"文件为空"，
            # 甚至把 `[stderr] nl: ... No such file` 当成文件正文回给模型。
            body, err, rc = remote_parts(output)
            if rc is None or err or not ok:
                return ToolResult(
                    False, body, f"读取文件失败: {err or '远程通道未返回退出码（未在远端执行）'}"
                )
            if not body:
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
    )

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        return _write_bound_command(args)

    async def execute(self, context, path, content):
        if is_traversal(path):
            return ToolResult(False, "", TRAVERSAL_ERROR)
        if not is_remote(context):
            return ToolResult(False, "", _WRITE_REQUIRES_SSH)
        content = content if isinstance(content, str) else str(content)
        data = content.encode("utf-8")

        ok, output = await run_headless(
            context, base64_write_cmd(path, data), self.meta.timeout
        )
        if not ok:
            return ToolResult(False, output, "远程写入文件失败")
        failed, note = await _verify_remote_write(context, path, len(data), self.meta.timeout)
        if failed:
            return ToolResult(False, "", note)
        return ToolResult(True, f"已写入 {len(data)} 字节 -> {path}{note}")


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
    )

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        return _write_bound_command(args)

    @staticmethod
    def _apply_replace(content, old, new):
        count = content.count(old)
        if count == 0:
            return ToolResult(False, "", "old_string 在文件中出现 0 次，无法替换")
        if count > 1:
            return ToolResult(
                False, "", f"old_string 在文件中出现 {count} 次，要求恰好 1 次，拒绝替换"
            )
        return content.replace(old, new)

    async def execute(self, context, path, old_string, new_string):
        if is_traversal(path):
            return ToolResult(False, "", TRAVERSAL_ERROR)
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
        # surrogateescape 而非 replace：errors="replace" 会把文件里每个非 UTF-8 字节
        # （GBK/Latin-1 运维配置很常见）解码成 U+FFFD，回写即整文件不可逆损坏。
        # surrogateescape 用码位保留原始字节，_apply_replace 只动 old_string 命中段，
        # 回写时字节完全复原；对合法 UTF-8 与逐字节等价，无副作用。
        content = raw.decode("utf-8", errors="surrogateescape")
        new_content = self._apply_replace(content, old_string, new_string)
        if isinstance(new_content, ToolResult):
            return new_content
        data = new_content.encode("utf-8", errors="surrogateescape")
        ok2, out2 = await run_headless(
            context, base64_write_cmd(path, data), self.meta.timeout
        )
        if not ok2:
            return ToolResult(False, out2, "远程写入文件失败")
        failed, note = await _verify_remote_write(context, path, len(data), self.meta.timeout)
        if failed:
            return ToolResult(False, "", note)
        return ToolResult(True, f"已替换 1 处{note}")


# glob 模式的绝对定位前缀：`/x`、`\srv\x`、`C:\x`。两个分支都不支持绝对模式
# （pathlib 对它抛 NotImplementedError，远端 `find -name` 从不匹配带斜杠的文件名）。
# 不用 PurePath(...).is_absolute() 判定：它的结论随平台变（POSIX 上 `C:/x` 不算绝对，
# Windows 上 `/x` 也不算），而这条约束在两平台上是同一条。
_ABSOLUTE_PATTERN_RE = re.compile(r"^(?:[A-Za-z]:[/\\]|[/\\])")


class FileGlobTool(BaseTool):
    name = "file_glob"
    description = "按 glob 模式在指定目录下递归查找文件，返回匹配文件列表。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "起始目录绝对路径"},
            "pattern": {
                "type": "string",
                "description": "glob 匹配模式，必须是相对 path 的模式（如 '*.log'、'**/*.log'），不得含 `..`",
            },
        },
        "required": ["path", "pattern"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        timeout=30.0,
    )

    async def execute(self, context, path, pattern):
        # pattern 同样是路径参数：只校验 path 时，`path=srv, pattern=../x/*.log`
        # 就能列出 srv 之外的文件（远端模板 `find {path} -name {pattern}` 天生锚定
        # 在 path 内，两个分支必须同口径 —— 见 tests/test_ops_file_job_img.py）
        if is_traversal(path):
            return ToolResult(False, "", TRAVERSAL_ERROR)
        if not isinstance(pattern, str) or not pattern.strip():
            return ToolResult(False, "", "pattern 需为 glob 模式（如 '*.log'），收到空值")
        if is_traversal(pattern):
            return ToolResult(False, "", TRAVERSAL_ERROR)

        if is_remote(context):
            cmd = file_glob_cmd(path, pattern)
            ok, output = await run_headless(context, cmd, self.meta.timeout)
            if not ok:
                return ToolResult(False, output, "glob 查找失败")
            lines = drop_sentinel_lines(output)
            if not lines:
                return ToolResult(True, f"未匹配到文件：{pattern}")
            return ToolResult(True, truncate_output("\n".join(lines)))

        # 本地：pathlib 递归查找
        base = pathlib.Path(path)
        if not base.is_dir():
            return ToolResult(False, "", f"起始目录不存在或不是目录: {path}")
        if _ABSOLUTE_PATTERN_RE.match(pattern):
            # pathlib 对绝对模式抛 NotImplementedError（原样透传给模型是一句英文
            # "Non-relative patterns are unsupported"，看不出该怎么改），且远端
            # `find -name` 从不支持绝对模式 —— 提前拒绝并给出可执行的改法
            return ToolResult(
                False, "", f"pattern 必须是相对 {path} 的 glob 模式，收到绝对路径 {pattern!r}"
            )
        root = base.resolve()
        try:
            # 逐条命中后仍要按解析结果复核：3.11 的 rglob 会跟着符号链接目录进去，
            # 而 find 默认不跟随；resolve 后落在 root 之外的一律丢弃，两个口径对齐
            resolved = [p.resolve() for p in base.rglob(pattern) if p.is_file()]
            matches = sorted(str(p) for p in resolved if p.is_relative_to(root))
        except Exception as e:
            return ToolResult(False, "", f"glob 查找失败: {e}")
        if not matches:
            return ToolResult(True, f"未匹配到文件：{pattern}")
        return ToolResult(True, truncate_output("\n".join(matches)))


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
            return ToolResult(False, "", TRAVERSAL_ERROR)
        max_lines, err = parse_int_arg(max_lines, "max_lines", 200)
        if err:
            return ToolResult(False, "", err)
        max_lines = min(5000, max(1, max_lines))

        if is_remote(context):
            cmd = file_grep_cmd(path, pattern, max_lines)
            ok, output = await run_headless(context, cmd, self.meta.timeout)
            # 与 file_read 同一条管道陷阱：退出码属于 head。grep 打不开路径时写 stderr
            # 但整条 0 退出，原先"只在没有输出时才看 ok"的写法会回一句成功态的
            # 「未匹配到内容」—— 路径不存在与真的没匹配是两种结论，只能靠 [stderr] 段分。
            body, err, rc = remote_parts(output)
            if rc is None or err or not ok:
                return ToolResult(
                    False, body, f"grep 查找失败: {err or '远程通道未返回退出码（未在远端执行）'}"
                )
            lines = body.splitlines()
            if not lines:
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