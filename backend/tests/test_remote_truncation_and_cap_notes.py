"""回归：远程这两条腿不能把"被截断"报成"完整"。

S14-03 — 主进程 exec 通道对 stdout 有 1 MiB 上限，撞线时追加
「[输出已截断，超过 1MB 上限]」（ssh-manager.ts 的 TRUNCATED_NOTE）。这一手与
keep_full 无关（keep_full 只挡后端 truncate_output 那 4000 字符）：strip_remote_tail
只剥哨兵行、剥不掉这句说明，re.sub 只删空白、删不掉中文与方括号，于是 ≳786KB 的远端
图片会拼成**非法 base64 的 data URI**，而按已截断串估长的 10MB 上限在这个通道里永不
可能触发 —— 旧行为是 success=True 把坏字节交给模型。

S14-08 — file_grep 本地腿早就声明了「已达 max_lines 上限，可能仍有匹配未列出」，
远程腿没有：「正好 200 条」与「被截到 200 条」读起来一模一样，模型当全量结论用。
"""
import base64
import types

import pytest

from agent.tools.builtin.ops_file import FileGrepTool
from agent.tools.builtin.ops_image import ReadImageTool


def _remote_ctx() -> types.SimpleNamespace:
    # is_remote 只看 ssh_session_id（ops_common.py:79）
    return types.SimpleNamespace(ssh_session_id="sess-1")


SENTINEL = "\n[exit_code=0]\n"


@pytest.mark.asyncio
async def test_remote_truncated_image_is_not_a_success(monkeypatch):
    head = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096).decode("ascii")
    wire = head + "\n[输出已截断，超过 1MB 上限]" + SENTINEL

    async def fake(ctx, command, timeout, keep_full=False):
        return True, wire

    monkeypatch.setattr("agent.tools.builtin.ops_image.run_headless", fake)
    res = await ReadImageTool().execute(_remote_ctx(), path="/srv/shot.png")

    assert res.success is False, f"截断后的坏数据仍被报成成功：{res.output[:60]!r}"
    assert "data:" not in res.output
    assert ("截断" in res.error) or ("base64" in res.error)


@pytest.mark.asyncio
async def test_remote_intact_image_still_succeeds(monkeypatch):
    """反向锁：新判据不能把正常回传也拒掉（base64 字母表 + 换行是合法形状）。"""
    payload = base64.b64encode(b"\x89PNG fake-bytes").decode("ascii")
    wire = payload[:20] + "\n" + payload[20:] + SENTINEL

    async def fake(ctx, command, timeout, keep_full=False):
        return True, wire

    monkeypatch.setattr("agent.tools.builtin.ops_image.run_headless", fake)
    res = await ReadImageTool().execute(_remote_ctx(), path="/srv/shot.png")

    assert res.success is True, res.error
    assert res.output == f"data:image/png;base64,{payload}"


@pytest.mark.asyncio
async def test_remote_grep_declares_the_max_lines_cap(monkeypatch):
    lines = "\n".join(f"app.log:{i}:hit" for i in range(1, 4))

    async def fake(ctx, command, timeout, keep_full=False):
        return True, lines + SENTINEL

    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", fake)
    res = await FileGrepTool().execute(
        _remote_ctx(), path="/srv/logs", pattern="hit", max_lines=3
    )

    assert res.success is True
    assert "max_lines=3" in res.output, "撞上上限必须说出来（与本地腿同口径）"
    assert "可能仍有匹配未列出" in res.output


@pytest.mark.asyncio
async def test_remote_grep_under_the_cap_stays_clean(monkeypatch):
    """反向锁：没截断时不能凭空加一句"可能仍有" —— 注脚本身也不能撒谎。"""
    async def fake(ctx, command, timeout, keep_full=False):
        return True, "app.log:1:hit" + SENTINEL

    monkeypatch.setattr("agent.tools.builtin.ops_file.run_headless", fake)
    res = await FileGrepTool().execute(
        _remote_ctx(), path="/srv/logs", pattern="hit", max_lines=200
    )

    assert res.success is True
    assert "可能仍有匹配未列出" not in res.output
