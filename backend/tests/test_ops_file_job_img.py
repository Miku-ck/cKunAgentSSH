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
import tempfile
from pathlib import Path

import pytest

from agent.tools import ToolContext, create_registry_for_mode
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
    """
    import subprocess
    import sys
    try:
        subprocess.run([sys.executable, "-c", "pass"],
                       capture_output=True, timeout=15)
    except Exception:
        return False
    try:
        async def _a():
            p = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "pass", stdout=asyncio.subprocess.PIPE)
            await p.wait()
            return p.returncode == 0
        return asyncio.run(_a())
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
        mode="ssh",
        environment="test",
        visible_terminal=False,
    )
    base.update(kwargs)
    return ToolContext(**base)


async def _run(coro):
    return await coro


# ── 注册与元数据 ─────────────────────────────
def test_new_tools_registered_in_ssh_scope():
    names = {cls().name for cls in get_all_tool_classes("ssh")}
    assert NEW_TOOLS <= names


def test_registry_contains_new_tool_names():
    reg = create_registry_for_mode("ssh")
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
    NameError 而非返回内容。run_headless 返回 run.sh 哨兵形态，strip_remote_tail
    须剥掉 [exit_code=..]，只留 nl 行号正文。
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