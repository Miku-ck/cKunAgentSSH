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


@pytest.mark.asyncio
async def test_remote_output_offset_uses_remote_byte_size(monkeypatch):
    """S6-04：offset 推进量取自远端 `wc -c` 读数，与回传字符串的编码形态无关。

    段里塞满 U+FFFD —— 那是跨 TCP 分片的多字节字符（或非 UTF-8 日志）在 Electron
    侧解码塌掉的产物。按 len(tail.encode('utf-8')) 推进时，三个替换符算成 9 字节，
    比真实字节数虚高/虚低都不受控，下一轮 `tail -c +n` 就永久跳过若干字节，而且
    漂移会被 _persist_offset 落盘、跨重启存活。
    """
    e = _entry(offset=100)
    _fake_headless(monkeypatch, [
        (True, "CKUNJOB_RC=\nCKUNJOB_SZ=137\n\uFFFD\uFFFD\uFFFD\n[exit_code=0]"),
    ])
    res = await oj.JobOutputTool().execute(_ctx(), job_id="j1")
    assert res.success is True
    assert e.out_offset == 137, "offset 必须是远端报的字节总数，而不是替换符重新编码的长度"
    assert "CKUNJOB_SZ" not in res.output, "SZ 行是记账头，不得当日志内容交付"


@pytest.mark.asyncio
async def test_remote_output_size_smaller_than_consumed_repeats_not_skips(monkeypatch):
    """SZ 在 tail 之前测得：读数只会偏小（重复读几个字节），不许跳丢。"""
    e = _entry(offset=100)
    _fake_headless(monkeypatch, [
        (True, "CKUNJOB_RC=\nCKUNJOB_SZ=104\nlog tail\n[exit_code=0]"),
    ])
    await oj.JobOutputTool().execute(_ctx(), job_id="j1")
    assert e.out_offset == 104, "偏小的读数照单采用，下一轮从 105 起重读未被确认的 3 字节"


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


# ── 终止一个其实早就自己跑完的远程任务 ─────────────────────────
# kill 模板回的 CKUNJOB_DEAD 有两种成因：我们把进程组杀了，或者它早就自行退出。
# 远程任务在 job_list 对账之前内存里一直是 running，所以"用户点终止时任务十分钟
# 前就结束了"是常态。原实现一律落 killed + exit_code=None，而 update_status 的
# killed 粘滞防护让它永不回退 —— 真实退出码（run.sh 写下的 rc 文件）就此丢失。
# 判据：run.sh 只有走完最后一步才写 rc；整进程组被 KILL 时 run.sh 自己也死了。
# 所以**有 rc = 它自己跑完的，没 rc = 我们杀的**。

def _kill_then_rc(monkeypatch, rc_out):
    """kill 模板回 DEAD，随后的 rc 读取回 rc_out。"""
    seen = []

    async def fake(context, command, timeout=30, keep_full=False):
        seen.append(command)
        if "kill -" in command:
            return True, "CKUNJOB_DEAD\n[exit_code=0]"
        return True, rc_out

    monkeypatch.setattr(oj, "run_headless", fake)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("rc_out,want", [
    ("0\n[exit_code=0]", ("finished", 0)),
    ("7\n[exit_code=0]", ("failed", 7)),
    ("\n[exit_code=0]", ("killed", None)),      # run.sh 没走完 = 确实被我们杀了
])
async def test_job_kill_按_rc_落真实终态(monkeypatch, rc_out, want):
    _entry("j1", pid=99)
    seen = _kill_then_rc(monkeypatch, rc_out)
    res = await oj.JobKillTool().execute(_ctx(), job_id="j1")
    assert res.success is True
    e = oj.JobManager.instance().get("j1")
    assert (e.status, e.exit_code) == want, f"{e.status}/{e.exit_code} != {want}"
    assert len(seen) == 2, f"没有回读 rc：{seen}"


@pytest.mark.asyncio
async def test_run_取消清理同样不得抹掉已结束的rc(monkeypatch):
    """`kill_remote_jobs_for_run` 是 run 断连/取消时的清理路径，同一判据。"""
    _entry("j1", run_id="run-A")
    _entry("j2", run_id="run-A")
    _kill_then_rc(monkeypatch, "0\n[exit_code=0]")
    done = await oj.kill_remote_jobs_for_run("run-A")
    mgr = oj.JobManager.instance()
    assert done == 2
    assert (mgr.get("j1").status, mgr.get("j1").exit_code) == ("finished", 0)
    assert (mgr.get("j2").status, mgr.get("j2").exit_code) == ("finished", 0)


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


# ── 对账读 rc：位置对齐 + 恢复时按 UTC 解释 created_at ───────────

@pytest.mark.asyncio
async def test_对账两次回传都必须按字节协议取回(monkeypatch):
    """判活清单与 rc 清单都是**按下标/整行**解析的，而 run_headless 默认 truncate_output。

    截断会把标记行插进正文中间（`…[输出已截断，省略中间 N 字符]…`），splitlines 之后
    下标整体错位 —— 结果是把 A 任务的退出码记到 B 任务头上（`_reconcile_remote` 里
    "只跳真正的哨兵行"那条注释防的是哨兵行错位，这是同一根因的另一条路）。rc 每行
    1~3 字节，要约 1300 条同时对账才会越界，所以这是一次性写错、平时不发作的那类隐患：
    用 keep_full 从结构上断掉。
    """
    opts = []

    async def fake(context, command, timeout=30, keep_full=False):
        opts.append(keep_full)
        if "kill -0" in command:
            return True, "11 DEAD\n[exit_code=0]"
        return True, "3\n[exit_code=0]"

    monkeypatch.setattr(oj, "run_headless", fake)
    _entry("j1", pid=11)
    await oj.JobListTool().execute(_ctx())
    # opts = [判活, 读 rc]
    assert len(opts) == 2, opts
    assert opts == [True, True], (
        "对账的两次回传都必须 keep_full：判活清单被截会把活任务判成已退出，"
        "rc 清单被截会整体错位、把 A 的退出码记到 B 头上")


def test_restore_按_UTC_解释_created_at(monkeypatch):
    """`database._now()` 落库的是 UTC 字符串，而 `time.mktime` 按**本地时区**解释 struct_time。

    实测在 UTC+8 主机上：刚创建的远程任务被重启恢复后，job_list 报「已经跑了
    28800 秒」—— 八小时凭空出现，且这条读数会进模型上下文影响后续判断。
    """
    import calendar
    import time as _time
    from datetime import datetime, timezone

    import database as db

    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = [{
        "job_id": "j9", "created_at": created, "cmd": "sleep 1", "pid": 777,
        "log_dir": "/tmp/ckun_agent_jobs/j9", "status": "running",
        "session_id": "sess-remote", "run_id": "run-1", "out_offset": 0,
    }]
    monkeypatch.setattr(db, "get_running_agent_jobs", lambda: rows)
    monkeypatch.setattr(oj.JobManager, "_restored", False, raising=False)

    mgr = oj.JobManager.instance()
    mgr._ensure_restored()
    e = mgr.get("j9")
    assert e is not None, "恢复分支没跑起来，测试无效"
    expected = calendar.timegm(_time.strptime(created, "%Y-%m-%d %H:%M:%S"))
    assert abs(e.start_ts - expected) < 2, (
        f"start_ts={e.start_ts} 与 UTC 口径差 {e.start_ts - expected:.0f}s：按本地时区解了 UTC 串")
    assert oj._running_seconds(e, _time.time()) < 5


def test_终态清单只许有一处字面量():
    """状态字面量在三处各抄一遍时必然漂移：`update_status` 判终态、
    `update_agent_job_status` 落 finished_at、`get_running_agent_jobs` 选恢复行。

    实测漂移的后果：`exited` 只在内存侧算终态，库里它永远 `finished_at IS NULL`，
    而且每次后端重启都被"恢复"成在跑任务（本文件上面那条 exited 用例就是它的来源）。
    所以清单收进 `database.JOB_TERMINAL_STATUSES` 单一事实源，这里钉"不许再出现第二份
    字面量"—— 行为侧的锁在 test_agent_job_carryover_and_legacy_migrate.py。
    """
    import pathlib

    import database as db

    assert set(db.JOB_TERMINAL_STATUSES) == {"finished", "failed", "killed", "exited"}
    here = pathlib.Path(__file__).resolve().parents[1] / "agent" / "tools" / "builtin" / "ops_job.py"
    src = here.read_text(encoding="utf-8")
    assert "JOB_TERMINAL_STATUSES" in src, "ops_job 没有复用库侧终态清单"
    assert '"finished", "failed", "killed", "exited"' not in src, (
        "ops_job 里又抄了一份终态字面量清单 —— 两份清单必然漂移")


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


# ── S6-02 / S6-03 / S6-05 回归：号占位、进程组判活、陈旧 rc ──────
def test_next_id_reserves_before_the_await_window():
    """S6-02：号一经发出就占坑，不等 await 远程启动之后。

    原实现 mgr.add(entry) 落在 await _remote_launch（最长 20s）之后，两个并发
    job_run 会拿到同一个号 → 撞进同一个 <root>/<id>：启动模板的 `>` 截断前一份
    out.log、覆写 run.sh/rc，且后一次 add 把前一个作业的 pid 句柄与 kill 通道
    整体替换掉。
    """
    mgr = oj.JobManager()
    first = mgr._next_id()
    second = mgr._next_id()          # 未 add()，第二个并发调用必须看到坑位
    assert first != second
    assert mgr.get(first) is None, "坑位不是条目：启动失败只跳号，不造出假作业"


def test_job_launch_cmd_clears_stale_rc():
    """S6-05：复用号必须先清掉上一任的 rc。

    终态行不参与启动恢复 ⇒ 那些号在重启后就是空闲号，而远端 rc 还躺着旧退出码，
    新作业第一次 job_output 就被判「已完成」并挂上死作业的码，随后 job_kill 拒止。
    """
    cmd = oj.job_launch_cmd("7", "sleep 5")
    rc_line = f"rm -f {oj._JOBS_ROOT}/7/rc"
    assert rc_line in cmd.splitlines()
    assert cmd.index(rc_line) < cmd.index("setsid")


def test_job_kill_cmd_verifies_the_group_it_signalled():
    """S6-03：发信号按进程组，验证生死也必须按进程组。

    命令自我 daemon 化（nginx、redis-server --daemonize yes、以 & 收尾）后组长已
    退出而组成员还活着 ⇒ `kill -0 <leader>` 判成 DEAD ⇒ 作业标 killed 并回「已终止
    （进程组已退出）」，而 SIGKILL 一次都没发出。
    """
    cmd = oj.job_kill_cmd(12345)
    assert "kill -0 -- -12345" in cmd
    assert "kill -0 12345" not in cmd
    assert "kill -0 -- -$p" in oj.job_reconcile_alive_cmd([111, 222])


def test_job_kill_cmd_rejects_non_numeric_pid():
    """S6-21 / S6-16：pid 取自 DB 且要拼进远端命令串，脏值必须抛错而非送进 shell。"""
    with pytest.raises(ValueError):
        oj.job_kill_cmd("12345; rm -rf /")
