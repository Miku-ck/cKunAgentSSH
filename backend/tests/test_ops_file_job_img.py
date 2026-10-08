"""运维新增工具测试 — 文件操作 / 图片查看 / 后台任务。

覆盖新增的 10 个运维工具：file_read / file_write / file_edit / file_glob /
file_grep / read_image / job_run / job_list / job_output / job_kill。

测试刻意避免依赖真实远程桥接：通过置空 ssh_session_id 走本地 Python 分支，
或用 monkeypatch 注入共享执行辅助，保证跨平台确定性（Windows 亦通过）。

临时目录使用 conftest 的 tmp_workspace（普通 os.mkdir）— 不能用
tmp_path/tempfile.mkdtemp：Windows 下 mkdtemp 打限制性 ACL，沙箱
进程随后访问会被拒（见 conftest.py 注释）。
"""
import asyncio
import os
import re
from pathlib import Path

import pytest

from agent.tools import ToolContext, create_registry
from agent.tools.builtin import (
    get_all_tool_classes,
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    FileGlobTool,
    FileGrepTool,
    ReadImageTool,
    JobRunTool,
    JobListTool,
    JobOutputTool,
    JobKillTool,
)

NEW_TOOLS = {
    "file_read", "file_write", "file_edit", "file_glob", "file_grep",
    "read_image", "job_run", "job_list", "job_output", "job_kill",
}


def _spawn_allowed() -> bool:
    """探测本环境能否派生子进程（部分沙箱拒绝 asyncio 的 CreateProcess → WinError 5）。

    必须探测 asyncio.create_subprocess_exec — 与 subprocess.run 走不同路径，
    存在前者被拒而后者可用的沙箱。

    探测本身必须有上界：它在**模块导入期**执行，`p.wait()` 无界时整个文件一条用例
    都跑不到就挂住（实测 `pytest tests/test_ops_file_job_img.py` 静默 400s+，
    连带把同批次里其他文件一起拖停 —— 收集期的挂起没有测试输出，只表现为整批超时）。
    沙箱拒绝派生时给出的应当是 False（跳过 job 用例），不是无限等待。
    """
    import subprocess
    import sys
    try:
        subprocess.run([sys.executable, "-c", "pass"],
                       capture_output=True, timeout=15)
    except Exception:
        return False

    async def _a():
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "pass", stdout=asyncio.subprocess.PIPE)
        try:
            await p.wait()
        finally:
            if p.returncode is None:
                p.kill()
        return p.returncode == 0

    try:
        return asyncio.run(asyncio.wait_for(_a(), timeout=15))
    except Exception:
        return False


REQUIRES_SPAWN = _spawn_allowed()


@pytest.fixture(autouse=True)
def _jobs_run_unsandboxed(monkeypatch):
    """job 测试验证的是工具链本身（run→list→output→kill），旁路沙箱隔离：

    1) 本机装有 docker/podman/bwrap/systemd-run 等后端时，宿主命令会被包裹进
       另一执行环境（宿主 python 路径/解释器不可见 → rc=127、输出丢失）。
    2) 无后端时默认 fail-closed 会直接拒绝本地执行。
    沙箱行为本身由 test_sandbox_exec.py 覆盖，此处只保证工具链确定性。
    """
    import agent.harness.sandbox_exec as se
    monkeypatch.setenv("CKUN_AGENT_ALLOW_UNSANDBOXED_LOCAL", "1")
    monkeypatch.setattr(se, "ALLOW_UNSANDBOXED_LOCAL", True)


def _ctx(*, remote: bool = False, **kwargs) -> ToolContext:
    base = dict(
        ssh_session_id="sess-1" if remote else "",
        environment="test",
        visible_terminal=False,
    )
    base.update(kwargs)
    return ToolContext(**base)


async def _run(coro):
    return await coro


# ── 注册与元数据 ─────────────────────────────
def test_new_tools_registered_in_ssh_scope():
    names = {cls().name for cls in get_all_tool_classes()}
    assert NEW_TOOLS <= names


def test_registry_contains_new_tool_names():
    reg = create_registry()
    assert NEW_TOOLS <= set(reg.names())


def test_file_read_tools_meta_safe():
    for cls in (FileReadTool, FileGlobTool, FileGrepTool, ReadImageTool):
        t = cls()
        assert t.meta.risk_level == "safe"
        assert t.meta.reversible is True
        assert t.meta.idempotent is True


def test_mutating_tools_meta_moderate():
    for cls in (JobKillTool,):
        t = cls()
        assert t.meta.risk_level == "moderate"
        assert t.meta.reversible is False
        assert t.meta.idempotent is False


# ── 文件读取：路径越界拒绝 ─────────────────────
@pytest.mark.parametrize("bad_path", [
    "/etc/../proc/self/mem",
    "../secret.txt",
    "..\\..\\windows\\system32",
])
@pytest.mark.asyncio
async def test_file_read_rejects_traversal_local(bad_path):
    res = await FileReadTool().execute(_ctx(), path=bad_path)
    assert res.success is False
    assert ".." in res.error


@pytest.mark.asyncio
async def test_file_read_local_basic(tmp_workspace):
    f = Path(tmp_workspace) / "a.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    res = await FileReadTool().execute(_ctx(), path=str(f), offset=2, limit=2)
    assert res.success is True
    body = res.output
    assert "line2" in body and "line3" in body
    assert "line1" not in body


@pytest.mark.asyncio
async def test_file_read_local_missing():
    res = await FileReadTool().execute(_ctx(), path="Z:/nonexistent.txt")
    assert res.success is False


@pytest.mark.asyncio
async def test_file_read_rejects_traversal_remote(monkeypatch):
    """远程分支：路径校验在构造命令前必须拒绝，无需真实桥接。"""
    async def boom(ctx, command, timeout):
        raise AssertionError("不应执行远程命令")
    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", boom)
    res = await FileReadTool().execute(_ctx(remote=True), path="/etc/../../etc/passwd")
    assert res.success is False
    assert ".." in res.error


@pytest.mark.asyncio
async def test_file_read_remote_success_strips_sentinel(monkeypatch):
    """远程 file_read 成功路径回归。

    回归锁：ops_file 曾在远端成功分支调用 strip_remote_tail 却未 import，
    导致真实 SSH 会话下 file_read 成功时抛 NameError（此前无测试覆盖远端
    成功分支，潜伏 8 轮）。此测试走完整远端成功路径，若 import 缺失会
    NameError 而非返回内容。run_headless 返回 run.sh 哨兵形态，成功分支现在走
    remote_parts 拆正文（strip_remote_tail 只剥尾部、不看 [stderr] 段，管道模板下
    会把 nl 的报错当文件正文回给模型 —— 见下面那组「管道退出码陷阱」用例）。
    """
    remote_body = "     1|root:x:0:0\n     2|bin:x:1:1\n[exit_code=0]\n"
    async def fake_run(ctx, command, timeout):
        assert command.startswith("nl -ba "), f"file_read 远端模板漂移: {command!r}"
        return True, remote_body
    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", fake_run)
    res = await FileReadTool().execute(_ctx(remote=True), path="/etc/passwd", offset=1, limit=10)
    assert res.success is True
    assert "root:x:0:0" in res.output
    assert "[exit_code=" not in res.output  # 哨兵已剥离


# ── 远端读类模板的管道退出码陷阱（成功形状的失败）──────────────
# `nl -ba f | sed -n …` 与 `grep -rnE … | head -n …` 都是管道：shell 的退出码属于
# **最后一段**。nl/grep 打不开路径时非零退出并写 stderr，而 sed/head 照样 0 退出 ——
# 于是 run_headless 的 ok 为真、[exit_code=0] 也在，只有 [stderr] 段说真话。
# 只看 ok 的两个远端分支因此把"读失败"报成"文件为空"/"没有匹配"（实测见下面的用例）。

@pytest.mark.asyncio
async def test_file_read_remote_nl失败不得报成文件为空(monkeypatch):
    wire = "\n[stderr]\nnl: /srv/missing.conf: No such file or directory\n[exit_code=0]\n"

    async def fake(ctx, command, timeout):
        return True, wire

    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", fake)
    res = await FileReadTool().execute(
        _ctx(remote=True), path="/srv/missing.conf", offset=1, limit=10
    )
    assert res.success is False, f"读失败被报成 success={res.success}：{res.output!r}"
    assert "无内容" not in res.output
    assert "No such file" in res.error + res.output


@pytest.mark.asyncio
async def test_file_read_remote_空文件与越界offset仍是成功(monkeypatch):
    """反向锁：正文空但没有 stderr 时，"无内容"是对的答案，不是故障。"""
    async def fake(ctx, command, timeout):
        return True, "[exit_code=0]\n"

    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", fake)
    res = await FileReadTool().execute(
        _ctx(remote=True), path="/srv/empty.conf", offset=500, limit=10
    )
    assert res.success is True
    assert "无内容" in res.output


@pytest.mark.asyncio
async def test_file_read_remote_未在远端跑起来不得编造读数(monkeypatch):
    """没有 [exit_code=…] 哨兵 = 通道根本没到远端（缺会话/桥不可达/超时）。"""
    async def fake(ctx, command, timeout):
        return False, ""

    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", fake)
    res = await FileReadTool().execute(_ctx(remote=True), path="/srv/a.conf")
    assert res.success is False


@pytest.mark.asyncio
async def test_file_grep_remote_grep失败不得报成没有匹配(monkeypatch):
    wire = "\n[stderr]\ngrep: /srv/logs: No such file or directory\n[exit_code=0]\n"

    async def fake(ctx, command, timeout):
        return True, wire

    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", fake)
    res = await FileGrepTool().execute(_ctx(remote=True), path="/srv/logs", pattern="ERROR")
    assert res.success is False, f"grep 失败被报成 success={res.success}：{res.output!r}"
    assert "未匹配" not in res.output
    assert "No such file" in res.error + res.output


@pytest.mark.asyncio
async def test_file_grep_remote_真的没有匹配仍是成功(monkeypatch):
    """反向锁：grep 无匹配时不写 stderr（退出码 1 也被 head 吞掉），这必须是"没匹配"。"""
    async def fake(ctx, command, timeout):
        return True, "[exit_code=0]\n"

    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", fake)
    res = await FileGrepTool().execute(_ctx(remote=True), path="/srv/app", pattern="NOPE")
    assert res.success is True
    assert "未匹配到内容" in res.output


def test_remote_parts_拆出口令哨兵的三种形态():
    """协议拆解收在 remote_protocol 单一实现源，消费方不再各抄一份正则。"""
    from agent.remote_protocol import remote_parts

    body, err, rc = remote_parts("hello\n[exit_code=0]\n")
    assert (body, err, rc) == ("hello", "", 0)
    body, err, rc = remote_parts("a\n[stderr]\nboom\n[exit_code=-9]\n")
    assert (body, err, rc) == ("a", "boom", -9)
    body, err, rc = remote_parts("")
    assert (body, err, rc) == ("", "", None)
    # 正文里出现 "No such file" 不算 stderr —— 那恰恰说明它被读出来了
    body, err, rc = remote_parts("No such file or directory\n[exit_code=0]\n")
    assert (body, err, rc) == ("No such file or directory", "", 0)



# ── 文件写入（仅限远程）─────────────────────────
@pytest.mark.asyncio
async def test_file_write_local_refused(tmp_workspace):
    """无 SSH 会话 → file_write 必须拒绝，绝不在后端主机本机落盘。"""
    f = Path(tmp_workspace) / "out.txt"
    res = await FileWriteTool().execute(_ctx(), path=str(f), content="hello\n")
    assert res.success is False
    assert "SSH 会话" in res.error
    assert not f.exists()


@pytest.mark.asyncio
async def test_file_write_traversal_rejected():
    res = await FileWriteTool().execute(_ctx(remote=True), path="../x.txt", content="x")
    assert res.success is False


@pytest.mark.asyncio
async def test_file_write_remote_ok(monkeypatch):
    """远程正常路径：base64 写命令 + 字节数回显。"""
    import base64 as _b64
    import re as _re
    import agent.tools.builtin.ops_file as m

    captured: list[str] = []

    async def fake_write(ctx, command, timeout):
        _ = ctx, timeout
        captured.append(command)
        return True, "ok"

    monkeypatch.setattr(m, "run_headless", fake_write)
    res = await FileWriteTool().execute(
        _ctx(remote=True), path="/tmp/out.txt", content="hello\n"
    )
    assert res.success is True
    assert "6 字节" in res.output
    b64 = _re.search(r"'([A-Za-z0-9+/=]+)'", captured[0]).group(1)
    assert _b64.b64decode(b64).decode("utf-8") == "hello\n"
    assert "> /tmp/out.txt" in captured[0]


# ── 文件编辑：唯一性校验（远程路径驱动）──────────
async def _edit_with_content(monkeypatch, original, old, new):
    """把 file_edit 远程读/写接到内存内容上，返回执行结果与写回内容。"""
    import agent.tools.builtin.ops_file as m

    written: list[str] = []

    async def fake_read(ctx, path):
        _ = ctx, path
        return True, original.encode("utf-8")

    async def fake_write(ctx, command, timeout):
        import base64 as _b64
        import re as _re
        _ = ctx, timeout
        b64 = _re.search(r"'([A-Za-z0-9+/=]+)'", command).group(1)
        written.append(_b64.b64decode(b64).decode("utf-8"))
        return True, "ok"

    monkeypatch.setattr(m, "remote_read_b64", fake_read)
    monkeypatch.setattr(m, "run_headless", fake_write)
    res = await FileEditTool().execute(
        _ctx(remote=True), path="/tmp/e.txt", old_string=old, new_string=new
    )
    return res, written


@pytest.mark.asyncio
async def test_file_edit_local_refused():
    """无 SSH 会话 → file_edit 必须拒绝（编辑即写回，属突变操作）。"""
    res = await FileEditTool().execute(
        _ctx(), path="C:/windows/win.ini", old_string="a", new_string="b"
    )
    assert res.success is False
    assert "SSH 会话" in res.error


@pytest.mark.asyncio
async def test_file_edit_unique_success(monkeypatch):
    res, written = await _edit_with_content(
        monkeypatch, "alpha beta alpha2", "beta", "GAMMA"
    )
    assert res.success is True
    assert "替换 1 处" in res.output
    assert written == ["alpha GAMMA alpha2"]


@pytest.mark.asyncio
async def test_file_edit_duplicate_rejected(monkeypatch):
    res, written = await _edit_with_content(
        monkeypatch, "xx yy xx", "xx", "zz"
    )
    assert res.success is False
    assert "2 次" in res.error
    assert written == []  # 未通过校验绝不写回


@pytest.mark.asyncio
async def test_file_edit_zero_rejected(monkeypatch):
    res, written = await _edit_with_content(
        monkeypatch, "hello", "nope", "z"
    )
    assert res.success is False
    assert "0 次" in res.error
    assert written == []


@pytest.mark.asyncio
async def test_file_edit_remote_does_not_strip_bracket_lines(monkeypatch):
    """回归：内容末尾含 '[exit_code=..]' 行时，远程编辑经 base64 读取不得丢失。

    bug: 旧实现用 cat + _strip_remote_tail 误删真实内容行；
    修复后改用 base64 读取，base64 字符集不含 '[' 与哨兵无损区分。
    """
    import base64 as _b64
    import agent.tools.builtin.ops_file as m

    original = "start\ntarget\n[exit_code=5]\n"
    written: list[str] = []

    async def fake_b64_read(ctx, path):
        _ = ctx, path
        return True, original.encode("utf-8")

    async def fake_write(ctx, command, timeout):
        _ = ctx, timeout
        # 从 write 命令还原内容：printf '%s' '<b64>' | base64 -d > path
        b64 = re.search(r"'([A-Za-z0-9+/=]+)'", command).group(1)
        written.append(_b64.b64decode(b64).decode("utf-8"))
        return True, "ok"

    monkeypatch.setattr(m, "remote_read_b64", fake_b64_read)
    monkeypatch.setattr(m, "run_headless", fake_write)

    res = await FileEditTool().execute(
        _ctx(remote=True), path="/tmp/a.conf", old_string="target", new_string="TARGET"
    )
    assert res.success is True
    assert written and written[0] == "start\nTARGET\n[exit_code=5]\n"


# ── 文件查找 / 内容搜索（本地） ────────────────
@pytest.mark.asyncio
async def test_file_glob_local(tmp_workspace):
    with open(os.path.join(tmp_workspace, "a.log"), "w", encoding="utf-8") as fh:
        fh.write("x")
    with open(os.path.join(tmp_workspace, "b.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    res = await FileGlobTool().execute(_ctx(), path=tmp_workspace, pattern="*.log")
    assert res.success is True
    assert "a.log" in res.output
    assert "b.txt" not in res.output


# ── file_glob 本地分支的锚定（pattern 也是路径参数）──────────
# 模块 docstring 的安全约束第 1 条写的是「所有路径均校验…不得包含 `..`，杜绝越界访问」，
# 第 5 条写的是「大输出经 truncate_output 截断」。实测两条在 file_glob 的本地分支上都不成立：
# `path=srv, pattern=../x_outside/*.log` 返回了 srv 之外的文件（`rglob` 不阻止 `..` 分段），
# 而远端分支 `find {path} -name {pattern}` 天生锚定在 path 内 —— 同一条约束两个分支一个有一个没有。
# 参数描述还写着「相对该目录或绝对均可」，而 pathlib 对绝对模式直接抛
# NotImplementedError("Non-relative patterns are unsupported")：模型照着说明填就一定报错。

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "../x_outside/*.log",
    "inner/../x_outside/*.log",
])
async def test_file_glob_local_模式含_点分段必须拒绝(tmp_workspace, bad):
    srv = Path(tmp_workspace) / "srv"
    (srv / "inner").mkdir(parents=True)
    (srv / "inner" / "a.log").write_text("x", encoding="utf-8")
    outside = Path(tmp_workspace) / "x_outside"
    outside.mkdir()
    (outside / "secret.log").write_text("x", encoding="utf-8")

    res = await FileGlobTool().execute(_ctx(), path=str(srv), pattern=bad)
    assert res.success is False, f"模式 {bad!r} 返回了 {res.output!r}：越界读取"
    assert "越界" in (res.output + res.error)


@pytest.mark.asyncio
async def test_file_glob_local_合法相对模式照旧可用(tmp_workspace):
    """反向锁：收紧 pattern 校验不能把正常用法一起挡掉。"""
    srv = Path(tmp_workspace) / "srv"
    (srv / "inner").mkdir(parents=True)
    (srv / "inner" / "a.log").write_text("x", encoding="utf-8")
    for keep in ("*.log", "**/*.log", "inner/*.log"):
        res = await FileGlobTool().execute(_ctx(), path=str(srv), pattern=keep)
        assert res.success is True, f"{keep!r} 被判失败：{res.error}"
        assert "a.log" in res.output


@pytest.mark.asyncio
async def test_file_glob_local_绝对模式给中文契约而非_pathlib_报错(tmp_workspace):
    srv = Path(tmp_workspace) / "srv"
    srv.mkdir()
    res = await FileGlobTool().execute(
        _ctx(), path=str(srv), pattern=str(srv / "*.log")
    )
    assert res.success is False
    assert "相对" in res.error, f"模型收到 {res.error!r}：不知道该怎么改参数"
    # 参数说明不得再承诺「绝对均可」——说明与实现不一致时模型会照着说明填
    desc = FileGlobTool.parameters["properties"]["pattern"]["description"]
    assert "绝对" not in desc, desc


@pytest.mark.asyncio
async def test_file_glob_local_大输出照旧截断(tmp_workspace):
    from agent.truncation_limits import TRUNC_THRESHOLD

    srv = Path(tmp_workspace) / "srv"
    srv.mkdir()
    n = TRUNC_THRESHOLD // 20 + 40
    for i in range(n):
        (srv / f"long_file_name_{i:05d}.log").write_text("x", encoding="utf-8")
    res = await FileGlobTool().execute(_ctx(), path=str(srv), pattern="*.log")
    assert res.success is True
    assert len(res.output) < TRUNC_THRESHOLD, (
        f"本地分支返回 {len(res.output)} 字符未截断：远端分支截、本地不截")
    assert "输出已截断" in res.output


@pytest.mark.asyncio
async def test_file_grep_local(tmp_workspace):
    f = Path(tmp_workspace) / "g.txt"
    f.write_text("apple\nbanana\napple pie\n", encoding="utf-8")
    res = await FileGrepTool().execute(_ctx(), path=str(f), pattern="apple", max_lines=10)
    assert res.success is True
    body = res.output
    assert body.count("apple") == 2


@pytest.mark.asyncio
async def test_file_grep_invalid_regex(tmp_workspace):
    res = await FileGrepTool().execute(
        _ctx(), path=tmp_workspace, pattern="([", max_lines=10
    )
    assert res.success is False
    assert "正则" in res.error


# ── 图片查看：扩展名 / 大小校验 ────────────────
@pytest.mark.asyncio
async def test_read_image_bad_extension(tmp_workspace):
    f = Path(tmp_workspace) / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    res = await ReadImageTool().execute(_ctx(), path=str(f))
    assert res.success is False
    assert "格式" in res.error


@pytest.mark.asyncio
async def test_read_image_traversal_rejected():
    res = await ReadImageTool().execute(_ctx(), path="../a.png")
    assert res.success is False


@pytest.mark.asyncio
async def test_read_image_local_ok(tmp_workspace):
    f = Path(tmp_workspace) / "pix.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfakedata")
    res = await ReadImageTool().execute(_ctx(), path=str(f))
    assert res.success is True
    assert res.output.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_read_image_too_large(tmp_workspace):
    f = Path(tmp_workspace) / "big.png"
    f.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    res = await ReadImageTool().execute(_ctx(), path=str(f))
    assert res.success is False
    assert "过大" in res.error


@pytest.mark.asyncio
async def test_read_image_remote_requests_keep_full(monkeypatch):
    """回归锁（Critical）：远端 read_image 走 base64 字节协议，必须以 keep_full=True
    调用 run_headless。否则 exec_remote.truncate_output 会把 ≳2.9KB 的图片切成
    "头+[输出已截断…]+尾"，本工具随后 re.sub 只删空白删不掉中文标记，得到内嵌乱码的
    非法 data URI 却以 success 返回；且按截断串估长的 10MB 上限永不触发。与
    remote_read_b64(ops_common:246) 同源，此前漏改。"""
    import base64 as _b64

    real_b64 = _b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"Z" * 6000).decode("ascii")
    wrapped = "\n".join(real_b64[i:i + 76] for i in range(0, len(real_b64), 76)) + "\n[exit_code=0]\n"
    captured = {}

    async def fake_run(ctx, command, timeout, keep_full=False):
        captured["keep_full"] = keep_full
        assert command.startswith("base64 -i "), f"read_image 远端模板漂移: {command!r}"
        return True, wrapped

    monkeypatch.setattr("agent.tools.builtin.ops_image.run_headless", fake_run)
    res = await ReadImageTool().execute(_ctx(remote=True), path="/srv/logo.png")
    assert captured.get("keep_full") is True, "base64 字节协议必须 keep_full=True"
    assert res.success is True
    assert res.output.startswith("data:image/png;base64,")
    body = res.output.split("base64,", 1)[1]
    assert body == real_b64  # 无损，哨兵/换行已剥离，未内嵌截断标记
    assert "省略" not in body and "截断" not in body and "[exit_code" not in body


# ── 后台任务：run → list → output → kill ──────
@pytest.mark.skipif(not REQUIRES_SPAWN, reason="当前沙箱禁止派生子进程")
@pytest.mark.asyncio
async def test_job_lifecycle_local(tmp_workspace):
    script = Path(tmp_workspace) / "job.py"
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n", encoding="utf-8")

    # shlex.split 会把 Windows 反斜杠当转义符，故用正斜杠路径
    script_posix = script.as_posix()
    run_tool = JobRunTool()
    res = await run_tool.execute(
        _ctx(),
        cmd=f"python {script_posix}",
    )
    assert res.success is True
    assert "job_id" in res.output
    job_id = res.output.split("job_id=")[1].splitlines()[0].strip()

    lst = await JobListTool().execute(_ctx())
    assert lst.success is True
    assert job_id in lst.output

    out = await JobOutputTool().execute(_ctx(), job_id=job_id, wait=1)
    assert out.success is True
    assert "started" in out.output

    kill = await JobKillTool().execute(_ctx(), job_id=job_id)
    assert kill.success is True
    assert "已终止" in kill.output


@pytest.mark.asyncio
async def test_job_output_unknown_id():
    res = await JobOutputTool().execute(_ctx(), job_id="99999")
    assert res.success is False


@pytest.mark.asyncio
async def test_job_run_empty_cmd():
    res = await JobRunTool().execute(_ctx(), cmd="   ")
    assert res.success is False