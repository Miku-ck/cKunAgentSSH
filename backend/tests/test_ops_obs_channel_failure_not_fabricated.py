"""观测工具不得把「我没看到」说成「那里没有」。

`run_headless` 返回 `False` 有两种完全不同的成因：
① 命令真的在远端执行了、退出码非 0 —— 回传里带 `[exit_code=N]` 哨兵；
② 命令根本没跑起来 —— 缺 SSH 会话 / 桥不可达 / 超时（ops_common 的 fail-closed 拒绝），
   回传里没有哨兵。

本文件钉的是这四个只读观测工具在这两种成因下的读数，因为模型据这些读数下结论：
- `process_inspect` 原先不看 `ok` 也不看退出码：通道拒绝时 `raw` 是一句中文拒绝理由，
  解析不出进程行 → 回 `success=True + "未找到匹配进程"`。模型据此认定进程不存在，
  接着就会去 `systemctl start` 一个本来好好的服务，或告诉用户"进程挂了"。同一文件里
  `sys_overview`（`:146`）早就检查了 `ok` —— 兄弟有、这里漏。
- `log_read` 原先按**正文**里有没有 "no such file" 判定失败：任何记录过缺文件错误的
  日志（极常见）都被读失败，且真实内容被藏起来。而带 pattern 时又把 `not ok` 一律当成
  "grep 无匹配"，于是 grep 退出码 2（文件读不了）与通道根本没跑，都被报成
  `success=True + "未匹配到日志行"`。
- `network_probe` 的 nc 回退支原先要求 stdout 里出现 "succeeded"/"open"，而 `nc -z` 是
  零 I/O 模式（busybox/部分实现成功时什么都不打印）→ 开着的端口被判「无法连接」。
  `nc -z` 的退出码本身就是判据。

反向锁同样钉住：真正的"没有匹配"（grep 退出码 1）必须仍然是 `success=True` 的正常答案，
别把合法的否读改成报错。
"""
import asyncio

import pytest

from agent.remote_protocol import EXIT_CODE_PREFIX, STDERR_SENTINEL
from agent.tools.builtin.ops_obs import (
    LogReadTool,
    NetworkProbeTool,
    ProcessInspectTool,
)

NO_SESSION = "拒绝执行：缺少 SSH 会话，运维工具仅限远程 SSH 会话（未在后端主机本机执行）"


def _wire(body: str, rc: int) -> str:
    """exec_remote 的形态：正文 + 末行退出码哨兵"""
    return f"{body}\n{EXIT_CODE_PREFIX}{rc}]\n"


def _wire_s(body: str, err: str, rc: int) -> str:
    """带 stderr 段的完整形态：正文 → [stderr] 段 → 退出码哨兵（ops_common:140-144）

    命令的报错走 stderr、stdout 为空，才是 `tail`/`grep` 读不了文件时回传的真实形状；
    把报错塞进正文里等于测了一条不存在的分支。
    """
    head = f"{body}\n" if body else ""
    return f"{head}{STDERR_SENTINEL}\n{err}\n{EXIT_CODE_PREFIX}{rc}]\n"


class _Ctx:
    ssh_session_id = "sess-1"


def _fake(monkeypatch, results):
    """按命令子串派发 run_headless 的返回值；未命中即测试写错。"""
    seen = []

    async def _run(context, command, timeout=30, keep_full=False):
        seen.append(command)
        for key, value in results.items():
            if key in command:
                return value
        raise AssertionError(f"未在结果表里的命令: {command[:80]!r}")

    monkeypatch.setattr("agent.tools.builtin.ops_obs.run_headless", _run)
    return seen


# ── process_inspect ─────────────────────────────────────────

def test_进程探测_通道没跑就必须是失败(monkeypatch):
    seen = _fake(monkeypatch, {"ps -eo": (False, NO_SESSION)})

    async def go():
        return await ProcessInspectTool().execute(_Ctx(), query="nginx")

    r = asyncio.run(go())
    assert seen, "用例没走到远程调用"
    assert r.success is False, f"通道拒绝被执行成「未找到匹配进程」：{r.output!r}"
    assert "未找到匹配进程" not in r.output


def test_进程探测_ps_缺失按执行失败(monkeypatch):
    _fake(monkeypatch, {"ps -eo": (False, _wire_s("", "bash: ps: command not found", 127))})

    async def go():
        return await ProcessInspectTool().execute(_Ctx(), query="nginx")

    assert asyncio.run(go()).success is False


def test_进程探测_真无匹配仍是正常答案_反向锁(monkeypatch):
    """grep 退出码 1 = 没有匹配，这是合法的否读，不该变成报错。"""
    _fake(monkeypatch, {"ps -eo": (False, _wire("", 1))})

    async def go():
        return await ProcessInspectTool().execute(_Ctx(), query="nginx")

    r = asyncio.run(go())
    assert r.success is True
    assert "未找到匹配进程" in r.output


def test_进程探测_有结果时正常出表格(monkeypatch):
    body = "  801  0.3  1.1 Ss   nginx           nginx: master process /usr/sbin/nginx"
    _fake(monkeypatch, {"ps -eo": (True, _wire(body, 0))})

    async def go():
        return await ProcessInspectTool().execute(_Ctx(), query="nginx")

    r = asyncio.run(go())
    assert r.success is True and "801" in r.output


# ── log_read ────────────────────────────────────────────────

def test_日志正文提到缺文件_不得判成读取失败(monkeypatch):
    """修复前的症状：日志里出现过 "No such file or directory" 就读不到内容了。"""
    body = "2026-10-08 19:00:01 ERROR open /data/x.bin failed: No such file or directory"
    _fake(monkeypatch, {"tail": (True, _wire(body, 0))})

    async def go():
        return await LogReadTool().execute(_Ctx(), path="/var/log/app.log", lines=50)

    r = asyncio.run(go())
    assert r.success is True, f"正文被判据误伤：{r.error or r.output!r}"
    assert "No such file or directory" in r.output


def test_日志文件不存在按退出码报失败(monkeypatch):
    _fake(monkeypatch, {
        "tail": (False, _wire_s("", "tail: cannot open '/var/log/x.log': No such file or directory", 1))})

    async def go():
        return await LogReadTool().execute(_Ctx(), path="/var/log/x.log")

    assert asyncio.run(go()).success is False


def test_日志过滤时_grep_报错不得当成无匹配(monkeypatch):
    """grep 退出码 2（读不了文件）与退出码 1（无匹配）是两件事。"""
    _fake(monkeypatch, {
        "grep": (False, _wire_s("", "grep: /var/log/app.log: Permission denied", 2))})

    async def go():
        return await LogReadTool().execute(
            _Ctx(), path="/var/log/app.log", pattern="ERROR")

    r = asyncio.run(go())
    assert r.success is False, f"报错被说成「未匹配到日志行」：{r.output!r}"


def test_日志过滤_无匹配仍是正常答案_反向锁(monkeypatch):
    _fake(monkeypatch, {"grep": (False, _wire("", 1))})

    async def go():
        return await LogReadTool().execute(
            _Ctx(), path="/var/log/app.log", pattern="ERROR")

    r = asyncio.run(go())
    assert r.success is True and "未匹配到日志行" in r.output


def test_日志读取_通道没跑就必须是失败(monkeypatch):
    _fake(monkeypatch, {"tail": (False, NO_SESSION)})

    async def go():
        return await LogReadTool().execute(_Ctx(), path="/var/log/app.log")

    assert asyncio.run(go()).success is False


# ── network_probe ───────────────────────────────────────────

def _probe(monkeypatch, nc_result, bash_result=(False, NO_SESSION)):
    _fake(monkeypatch, {
        "dev/tcp": bash_result,
        "nc -z": nc_result,
        "ss -ltn": (True, _wire("", 1)),
        "netstat": (True, _wire("", 1)),
    })
    return asyncio.run(NetworkProbeTool().execute(_Ctx(), host="10.0.0.1", port=443))


def test_nc_静默成功不得判成无法连接(monkeypatch):
    """`nc -z` 是零 I/O：busybox 等实现成功时什么都不打印，退出码才是判据。"""
    r = _probe(monkeypatch, nc_result=(True, _wire("", 0)))
    assert "可连通" in r.output, f"开着的端口被判死：{r.output!r}"


def test_nc_明确失败仍是无法连接_反向锁(monkeypatch):
    r = _probe(monkeypatch, nc_result=(False, _wire("", 1)))
    assert "无法连接" in r.output


def test_nc_带文本成功照常可连通(monkeypatch):
    r = _probe(monkeypatch, nc_result=(True, _wire(
        "Connection to 10.0.0.1 (443/tcp) succeeded!", 0)))
    assert "可连通" in r.output


def test_两种手段都没真正执行时不得谎报连通性(monkeypatch):
    r = _probe(monkeypatch, nc_result=(False, NO_SESSION))
    assert "无法连接" not in r.output, f"把「没测」说成「不通」：{r.output!r}"
    assert "无法探测" in r.output


def test_bash_分支成功时优先采信(monkeypatch):
    r = _probe(monkeypatch, nc_result=(False, NO_SESSION),
               bash_result=(True, _wire("OK", 0)))
    assert "可连通" in r.output
