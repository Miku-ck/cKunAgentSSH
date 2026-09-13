"""运维新增工具测试 — 文件操作 / 图片查看 / 后台任务。

覆盖新增的 10 个运维工具：file_read / file_write / file_edit / file_glob /
file_grep / read_image / job_run / job_list / job_output / job_kill。

测试刻意避免依赖真实远程桥接：通过置空 ssh_session_id 走本地 Python 分支，
或用 monkeypatch 注入共享执行辅助，保证跨平台确定性（Windows 亦通过）。
"""
import asyncio
import os
import re
import tempfile

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


def _ctx(*, remote: bool = False, **kwargs) -> ToolContext:
    base = dict(
        workspace_path=os.getcwd(),
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
async def test_file_read_local_basic(tmp_path):
    f = tmp_path / "a.txt"
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


# ── 文件写入 ─────────────────────────────────
@pytest.mark.asyncio
async def test_file_write_local(tmp_path):
    f = tmp_path / "out.txt"
    res = await FileWriteTool().execute(_ctx(), path=str(f), content="hello\n")
    assert res.success is True
    assert f.read_text(encoding="utf-8") == "hello\n"
    assert "6 字节" in res.output


@pytest.mark.asyncio
async def test_file_write_traversal_rejected():
    res = await FileWriteTool().execute(_ctx(), path="../x.txt", content="x")
    assert res.success is False


# ── 文件编辑：唯一性校验 ──────────────────────
@pytest.mark.asyncio
async def test_file_edit_unique_success(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("alpha beta alpha2", encoding="utf-8")
    res = await FileEditTool().execute(
        _ctx(), path=str(f), old_string="beta", new_string="GAMMA"
    )
    assert res.success is True
    assert "替换 1 处" in res.output
    assert f.read_text(encoding="utf-8") == "alpha GAMMA alpha2"


@pytest.mark.asyncio
async def test_file_edit_duplicate_rejected(tmp_path):
    f = tmp_path / "dup.txt"
    f.write_text("xx yy xx", encoding="utf-8")
    res = await FileEditTool().execute(
        _ctx(), path=str(f), old_string="xx", new_string="zz"
    )
    assert res.success is False
    assert "2 次" in res.error
    assert f.read_text(encoding="utf-8") == "xx yy xx"  # 文件保持不变


@pytest.mark.asyncio
async def test_file_edit_zero_rejected(tmp_path):
    f = tmp_path / "zero.txt"
    f.write_text("hello", encoding="utf-8")
    res = await FileEditTool().execute(
        _ctx(), path=str(f), old_string="nope", new_string="z"
    )
    assert res.success is False
    assert "0 次" in res.error


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

    monkeypatch.setattr(m, "_remote_b64_read", fake_b64_read)
    monkeypatch.setattr(m, "run_headless", fake_write)

    res = await FileEditTool().execute(
        _ctx(remote=True), path="/tmp/a.conf", old_string="target", new_string="TARGET"
    )
    assert res.success is True
    assert written and written[0] == "start\nTARGET\n[exit_code=5]\n"


# ── 文件查找 / 内容搜索（本地） ────────────────
@pytest.mark.asyncio
async def test_file_glob_local(tmp_path):
    (tmp_path / "a.log").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    res = await FileGlobTool().execute(_ctx(), path=str(tmp_path), pattern="*.log")
    assert res.success is True
    assert "a.log" in res.output
    assert "b.txt" not in res.output


@pytest.mark.asyncio
async def test_file_grep_local(tmp_path):
    f = tmp_path / "g.txt"
    f.write_text("apple\nbanana\napple pie\n", encoding="utf-8")
    res = await FileGrepTool().execute(_ctx(), path=str(f), pattern="apple", max_lines=10)
    assert res.success is True
    body = res.output
    assert body.count("apple") == 2


@pytest.mark.asyncio
async def test_file_grep_invalid_regex(tmp_path):
    res = await FileGrepTool().execute(
        _ctx(), path=str(tmp_path), pattern="([", max_lines=10
    )
    assert res.success is False
    assert "正则" in res.error


# ── 图片查看：扩展名 / 大小校验 ────────────────
@pytest.mark.asyncio
async def test_read_image_bad_extension(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    res = await ReadImageTool().execute(_ctx(), path=str(f))
    assert res.success is False
    assert "格式" in res.error


@pytest.mark.asyncio
async def test_read_image_traversal_rejected():
    res = await ReadImageTool().execute(_ctx(), path="../a.png")
    assert res.success is False


@pytest.mark.asyncio
async def test_read_image_local_ok(tmp_path):
    f = tmp_path / "pix.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfakedata")
    res = await ReadImageTool().execute(_ctx(), path=str(f))
    assert res.success is True
    assert res.output.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_read_image_too_large(tmp_path):
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    res = await ReadImageTool().execute(_ctx(), path=str(f))
    assert res.success is False
    assert "过大" in res.error


# ── 后台任务：run → list → output → kill ──────
@pytest.mark.asyncio
async def test_job_lifecycle_local(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n", encoding="utf-8")

    # shlex.split 会把 Windows 反斜杠当转义符，故用正斜杠路径
    script_posix = script.as_posix()
    run_tool = JobRunTool()
    res = await run_tool.execute(
        _ctx(workspace_path=str(tmp_path)),
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