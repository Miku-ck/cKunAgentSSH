# -*- coding: utf-8 -*-
"""远程后台任务（job_run target=remote）测试 — 方案 A：nohup/日志/PID。

全部通过 monkeypatch 替换 ops_job 模块内的 run_headless（桥接远程执行），
不依赖真实 SSH / Electron Bridge / 文件系统远端，Windows 确定性通过。
临时目录遵循 conftest 惯例（tmp_workspace，普通 os.mkdir）。
"""
import pytest

import re

import agent.tools.builtin.ops_job as oj
from agent.tools import ToolContext


def _ctx(**kwargs) -> ToolContext:
    base = dict(
        ssh_session_id="sess-remote",
        mode="ssh",
        environment="test",
        visible_terminal=False,
        agent_run_id="run-1",
    )
    base.update(kwargs)
    return ToolContext(**base)


def _entry(job_id="j1", *, pid=99, run_id="run-1", session="sess-remote",
           status="running", offset=0):
    import time
    e = oj.JobEntry(
        job_id=job_id,
        args=["sleep", "300"],
        start_ts=time.time(),
        status=status,
        remote=True,
        ssh_session_id=session,
        pid=pid,
        log_dir=f"/tmp/ckun_agent_jobs/{job_id}",
        run_id=run_id,
        raw_cmd="sleep 300",
        out_offset=offset,
    )
    oj.JobManager.instance().add(e)
    return e


@pytest.fixture(autouse=True)
def _clean_manager():
    """每个测试独立的 JobManager 内存态（避免跨测试串扰）。"""
    mgr = oj.JobManager.instance()
    with mgr._lock:
        mgr._jobs.clear()
    yield
    with mgr._lock:
        mgr._jobs.clear()


@pytest.fixture(autouse=True)
def _fresh_job_manager():
    """每测试前后重置 JobManager 单例。

    单例 _seq 跨测试文件累积：全套件顺序运行时本文件 get("1") 将指向
    其他文件创建的任务，断言随机失败。前置/后置清空保证独立可重跑。
    """
    with oj.JobManager._instance_lock:
        oj.JobManager._instance = None
    yield
    with oj.JobManager._instance_lock:
        oj.JobManager._instance = None


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """屏蔽远程任务持久化（本测试不验证 DB 落库，DB 契约由 database 单测覆盖）。"""
    import database as db
    monkeypatch.setattr(db, "save_agent_job", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(db, "update_agent_job_status", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(db, "get_running_agent_jobs", lambda *a, **k: [], raising=False)


def _fake_headless(monkeypatch, responses):
    """responses: list[(success, output)] 按调用顺序弹出；用尽后重复最后一个。"""
    calls = []

    async def fake(context, command, timeout=30, keep_full=False):
        calls.append(command)
        i = min(len(calls) - 1, len(responses) - 1)
        return responses[i]

    monkeypatch.setattr(oj, "run_headless", fake)
    return calls


# ── job_run 远程启动 ─────────────────────────────
@pytest.mark.asyncio
async def test_remote_launch_ok(monkeypatch):
    calls = _fake_headless(monkeypatch, [(True, "12345\n[exit_code=0]")])
    res = await oj.JobRunTool().execute(_ctx(), cmd="sleep 300", target="remote")
    assert res.success is True
    assert "pid=12345" in res.output
    # 启动命令结构：mkdir → base64 落脚本 → setsid 后台 → echo $!
    cmd = calls[0]
    assert "mkdir -p /tmp/ckun_agent_jobs/" in cmd
    assert "base64 -d" in cmd
    assert "setsid bash" in cmd
    assert "echo $!" in cmd
    entry = oj.JobManager.instance().get("1")
    assert entry is not None
    assert entry.remote and entry.pid == 12345 and entry.run_id == "run-1"


@pytest.mark.asyncio
async def test_remote_launch_requires_session(monkeypatch):
    """fail-closed：无 SSH 会话时拒绝，且不发起任何远程调用。"""
    calls = _fake_headless(monkeypatch, [(True, "1\n[exit_code=0]")])
    res = await oj.JobRunTool().execute(_ctx(ssh_session_id=""), cmd="sleep 1", target="remote")
    assert res.success is False
    assert "SSH 会话" in res.error
    assert calls == []


@pytest.mark.asyncio
async def test_remote_launch_bad_pid(monkeypatch):
    _fake_headless(monkeypatch, [(True, "not-a-pid\n[exit_code=0]")])
    res = await oj.JobRunTool().execute(_ctx(), cmd="sleep 1", target="remote")
    assert res.success is False
    assert "PID" in res.error


@pytest.mark.asyncio
async def test_remote_launch_exec_failure(monkeypatch):
    _fake_headless(monkeypatch, [(False, "bridge 不可达")])
    res = await oj.JobRunTool().execute(_ctx(), cmd="sleep 1", target="remote")
    assert res.success is False
    assert "远程启动失败" in res.error


# ── job_output 远程增量读取 + rc 终态 ─────────────
@pytest.mark.asyncio
async def test_remote_output_then_rc(monkeypatch):
    e = _entry(offset=0)
    # mock 复刻 run_headless 的真实组装：日志段在 RC 行后、末行 [exit_code=] 哨兵前
    _fake_headless(monkeypatch, [
        (True, "CKUNJOB_RC=\nhello world\n\n[exit_code=0]"),
        (True, "CKUNJOB_RC=0\n\n[exit_code=0]"),
    ])
    res1 = await oj.JobOutputTool().execute(_ctx(), job_id="j1")
    assert res1.success is True
    assert "hello world" in res1.output
    assert e.out_offset == len("hello world\n")  # 日志段含行尾换行 = 12 字节
    assert e.status == "running"

    res2 = await oj.JobOutputTool().execute(_ctx(), job_id="j1")
    assert e.status == "finished"
    assert e.exit_code == 0
    assert "exit_code=0" in res2.output


@pytest.mark.asyncio
async def test_remote_output_failed_rc(monkeypatch):
    e = _entry()
    _fake_headless(monkeypatch, [(True, "CKUNJOB_RC=137\n[exit_code=0]")])
    await oj.JobOutputTool().execute(_ctx(), job_id="j1")
    assert e.status == "failed"
    assert e.exit_code == 137


# ── job_kill 远程：TERM→KILL 升级链结果解析 ───────
@pytest.mark.asyncio
async def test_remote_kill_dead(monkeypatch):
    e = _entry()
    calls = _fake_headless(monkeypatch, [(True, "CKUNJOB_DEAD\n[exit_code=0]")])
    res = await oj.JobKillTool().execute(_ctx(), job_id="j1")
    assert res.success is True
    assert e.status == "killed"
    # kill 命令必须是整进程组 + 升级链
    cmd = calls[0]
    assert "kill -TERM -- -99" in cmd
    assert "kill -KILL -- -99" in cmd


@pytest.mark.asyncio
async def test_remote_kill_still(monkeypatch):
    _entry()
    _fake_headless(monkeypatch, [(True, "CKUNJOB_STILL\n[exit_code=0]")])
    res = await oj.JobKillTool().execute(_ctx(), job_id="j1")
    assert res.success is False
    assert "SIGKILL" in res.error


# ── run 取消联动清理 ─────────────────────────────
@pytest.mark.asyncio
async def test_kill_remote_jobs_for_run(monkeypatch):
    _entry("j1", run_id="run-A")
    _entry("j2", run_id="run-A")
    _entry("j3", run_id="run-B")
    _fake_headless(monkeypatch, [(True, "CKUNJOB_DEAD\n[exit_code=0]")])
    killed = await oj.kill_remote_jobs_for_run("run-A")
    mgr = oj.JobManager.instance()
    assert killed == 2
    assert mgr.get("j1").status == "killed"
    assert mgr.get("j2").status == "killed"
    assert mgr.get("j3").status == "running"   # 其他 run 不受影响


# ── job_list 远程对账 ─────────────────────────────
@pytest.mark.asyncio
async def test_reconcile_dead_with_rc(monkeypatch):
    _entry("j1", pid=11)
    _entry("j2", pid=22)
    _fake_headless(monkeypatch, [
        (True, "11 DEAD\n22 ALIVE\n[exit_code=0]"),   # 判活
        (True, "0\n[exit_code=0]"),                    # 读 rc（j1 → 0）
    ])
    res = await oj.JobListTool().execute(_ctx(), job_id=None)
    assert res.success is True
    mgr = oj.JobManager.instance()
    assert mgr.get("j1").status == "finished"
    assert mgr.get("j1").exit_code == 0
    assert mgr.get("j2").status == "running"


@pytest.mark.asyncio
async def test_reconcile_dead_without_rc(monkeypatch):
    _entry("j1", pid=11)
    _fake_headless(monkeypatch, [
        (True, "11 DEAD\n[exit_code=0]"),
        (True, "\n[exit_code=0]"),                     # rc 为空
    ])
    await oj.JobListTool().execute(_ctx())
    assert oj.JobManager.instance().get("j1").status == "exited"


# ── 字节守恒：offset 推进必须与 out.log 真实字节对齐 ─────────────
# 读回内容必须逐字节还原（旧 splitlines+rejoin 会把 CRLF/孤立 CR 归一损坏）；
# offset 每轮必须推进"日志段全长"，否则下一轮 tail -c +n 重复读或跳丢。

@pytest.mark.asyncio
async def test_remote_output_crlf_bytes_preserved(monkeypatch):
    """CRLF/孤立 CR 日志必须逐字节还原（splitlines+rejoin 会损坏）。"""
    log = "line1\r\nline2\r\na\rb\nc\n"
    _entry(offset=0)   # 注册 j1 —— 下面按 job_id="j1" 取的就是它
    calls = {"n": 0}

    async def fake(context, command, timeout=30, keep_full=False):
        n = int(re.search(r"tail -c \+(\d+)", command).group(1))
        rc_val = "0" if calls["n"] >= 2 else None
        return True, f"CKUNJOB_RC={rc_val or ''}\n{log[n - 1:]}\n[exit_code=0]"

    monkeypatch.setattr(oj, "run_headless", fake)
    got = []
    for i in range(3):
        calls["n"] = i
        res = await oj.JobOutputTool().execute(_ctx(), job_id="j1")
        assert res.success is True
        body = res.output.split("\n", 1)[1]
        if body.strip() and not body.startswith("（暂无新输出"):
            got.append(body)
    assert "".join(got) == log, f"CRLF/CR 字节被损坏: {got!r} != {log!r}"


@pytest.mark.asyncio
async def test_remote_offset_advances_exact_bytes(monkeypatch):
    """每轮 offset 必须推进日志段全长（不多不少），最终 == 日志总长。"""
    log = "x" * 100 + "\n"  # 101 字节
    e = _entry(offset=0)

    async def fake(context, command, timeout=30, keep_full=False):
        n = int(re.search(r"tail -c \+(\d+)", command).group(1))
        return True, f"CKUNJOB_RC=\n{log[n - 1:]}\n[exit_code=0]"

    monkeypatch.setattr(oj, "run_headless", fake)
    await oj.JobOutputTool().execute(_ctx(), job_id="j1")
    assert e.out_offset == len(log), f"offset {e.out_offset} != 日志总长 {len(log)}"


@pytest.mark.asyncio
async def test_remote_offset_persisted_on_read(monkeypatch):
    """offset 推进必须落盘（重启后恢复不从头重读）。"""
    _entry(offset=0)   # 只为注册 j1；断言看的是 _persist_offset 的调用
    persisted = []
    monkeypatch.setattr(oj, "_persist_offset", lambda jid, off: persisted.append((jid, off)))
    async def fake(context, command, timeout=30, keep_full=False):
        return True, "CKUNJOB_RC=\nhello\n[exit_code=0]"
    monkeypatch.setattr(oj, "run_headless", fake)
    await oj.JobOutputTool().execute(_ctx(), job_id="j1")
    assert persisted == [("j1", 5)]  # 日志段 "hello"（mock 哨兵紧随 RC 行形态）


# ── JobManager 终态淘汰 ─────────────────────────────
def test_evict_terminal_jobs_keeps_running():
    mgr = oj.JobManager.instance()
    import time
    now = time.time()
    for i in range(oj._MAX_TERMINAL_JOBS + 10):
        mgr.add(oj.JobEntry(
            job_id=f"t{i}", args=["x"], start_ts=now + i,
            status="finished", exit_code=0,
        ))
    mgr.add(oj.JobEntry(job_id="still-running", args=["x"], start_ts=now, status="running"))
    mgr.update_status("t0", "finished", 0)  # 触发淘汰
    jobs = mgr.list()
    ids = {e.job_id for e in jobs}
    assert "still-running" in ids, "running 任务不得被淘汰"
    assert len([j for j in jobs if j.status != "running"]) <= oj._MAX_TERMINAL_JOBS
    assert "t0" not in ids  # 最旧的终态被淘汰


# ── 安全链：job_run 内部命令必须走命令级分类 ──────
def test_job_run_in_command_exec_tools():
    from agent.harness.reins import _COMMAND_EXEC_TOOLS
    from agent.harness.safety import classify_shell_args
    assert "job_run" in _COMMAND_EXEC_TOOLS
    # 内部 dangerous/hardline 命令必须被识别（wrapper 不介入分类）
    level, _ = classify_shell_args({"cmd": "rm -rf /"})
    assert level != "safe"
