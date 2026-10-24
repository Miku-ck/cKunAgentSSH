"""S14-01：写类工具不得只看 `ok` —— 管道的退出码属于最后一段。

写盘模板 `printf '%s' '<b64>' | base64 -d > path` 有两处结构性事实：
① `> path` 在 printf 之前执行，既有内容当场被截成 0 字节；
② 整条命令的退出码是 `base64 -d` 的，而 base64 收到**空输入**仍然 0 退出。

于是 payload 撞上 ARG_MAX（或 printf 因任何原因失败）时：ok=True、exit_code=0、
文件已经是空的，而旧实现回的是「已写入 N 字节」。撤销腿在预快照不可用时是 NoOp
（>200KB 就没有快照），所以这条连"可恢复"都算不上。

同文件的 `file_read_cmd` docstring 早就把这条判据写明白了，读类工具也都走
`remote_parts` —— 写类工具漏了，属"同族防护漏一处"。
"""
import asyncio
import inspect

import pytest

from agent.remote_protocol import EXIT_CODE_PREFIX
from agent.tools.builtin import ops_file
from agent.tools.builtin.ops_file import _verify_remote_write


class _Ctx:
    ssh_session_id = "sess-1"


def _wire(body, rc=None, ok=True):
    """run_headless 的形态：正文 + 末行退出码哨兵（顺序同 ops_common.exec_remote）。"""
    tail = f"\n{EXIT_CODE_PREFIX}{rc}]" if rc is not None else ""
    return ok, f"{body}{tail}\n"


@pytest.fixture
def calls(monkeypatch):
    """按调用顺序派发回验通道的返回值，并记下真实发出去的命令串。"""
    recorded = []
    box = {"resp": _wire("0", 0)}

    async def fake(context, command, timeout=30, keep_full=False):
        recorded.append(command)
        return box["resp"]

    monkeypatch.setattr(ops_file, "run_headless", fake)
    return recorded, lambda resp: box.__setitem__("resp", resp)


def test_matching_size_is_a_clean_success(calls):
    recorded, set_resp = calls
    set_resp(_wire("1024", 0))
    failed, note = asyncio.run(_verify_remote_write(_Ctx(), "/etc/app.conf", 1024, 30))
    assert failed is False
    assert note == "", "验上了还不吭声 —— 每次都带一句废话同样在稀释这条判据"
    assert recorded == ["wc -c < /etc/app.conf"], "回验必须走只读动词，且不重新拼 payload"


def test_zero_byte_result_is_reported_as_failure_not_as_written(calls):
    """这就是原缺陷的现场：printf 没跑起来，文件被截空，而 ok/exit_code 全绿。"""
    _recorded, set_resp = calls
    set_resp(_wire("0", 0))
    failed, note = asyncio.run(_verify_remote_write(_Ctx(), "/etc/app.conf", 4096, 30))
    assert failed is True
    assert "4096" in note and "0 字节" in note
    assert "不要按已写入继续往下推" in note, "模型据这句话决定下一步，结论必须可直接执行"


def test_partial_size_also_fails(calls):
    _recorded, set_resp = calls
    set_resp(_wire("2048", 0))
    failed, note = asyncio.run(_verify_remote_write(_Ctx(), "/etc/app.conf", 4096, 30))
    assert failed is True
    assert "2048" in note


def test_unverified_channel_is_not_fabricated_as_verified_or_as_damage(calls):
    """回验命令本身失败 = **没读到**，既不能冒充"已验证"，也不能反过来说"你把文件写坏了"。"""
    _recorded, set_resp = calls
    set_resp(_wire("", 126, ok=False))
    failed, note = asyncio.run(_verify_remote_write(_Ctx(), "/etc/app.conf", 4096, 30))
    assert failed is False
    assert note.startswith("（未能回验远端字节数")


def test_non_numeric_reading_is_inconclusive_not_zero(calls):
    """wrapper 拒绝、或读到一句提示语时，绝不能 `int()` 崩、也不能当成 0 判失败。"""
    _recorded, set_resp = calls
    set_resp(_wire("command not allowed: wc", 0))
    failed, note = asyncio.run(_verify_remote_write(_Ctx(), "/etc/app.conf", 4096, 30))
    assert failed is False
    assert "未能回验" in note and "command not allowed" in note


def test_both_write_sites_verify_before_claiming_success():
    """漂移守卫：将来再加第三条写腿（file_append 之类）必须同样回验。"""
    src = inspect.getsource(ops_file)
    assert src.count("await _verify_remote_write(") >= 2
    lines = src.split("\n")
    claims = []
    for i, line in enumerate(lines):
        if 'ToolResult(True, f"已写入' in line or 'ToolResult(True, f"已替换' in line:
            claims.append(i + 1)
            above = "\n".join(lines[max(0, i - 8):i])
            assert "_verify_remote_write(" in above, f"第 {i + 1} 行的成功文案没先回验"
    assert len(claims) == 2, f"写类成功出口应当是两条，实测 {claims}"
