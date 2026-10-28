"""runbook_engine 守卫 — 标记行判读（通过/失败/缺标记 fail-closed）/ 拦截当失败而非成功
（含「拦截+通过标记」不得洗白）/ 失败即停 + 后续 skipped / 诊断 run 触发且 prompt 携带
失败现场 / 并发防重入（含收尸挂起窗口）/ 删除占位拒触发 / 取消落终态。

无头 run 与建连/断连全部打在 agent.headless_run 的模块缝上（run_headless_once /
open_session / close_session / append_record），不触真实 Harness 与 bridge。
"""
import asyncio
import threading

import pytest

import runbook_store as rstore
from agent import headless_run
from agent.harness.contracts import POLICY_DENY_MARKER
from runbook_engine import RunbookEngine, extract_verdict, build_step_prompt


@pytest.fixture
def engine():
    return RunbookEngine()


def _mk_runbook(steps, target_host="conn-1"):
    return rstore.create_runbook({
        "name": "剧本", "description": "", "target_host": target_host,
        "steps": steps, "enabled": True,
    })


def _ok(content="...\n[步骤结论] 通过"):
    return headless_run.HeadlessRunResult(
        content=content, state="completed", host="web-1", environment="test")


def _fail(content="...\n[步骤结论] 失败: 端口未监听", permission=False):
    return headless_run.HeadlessRunResult(
        content=content, state="completed", host="web-1", environment="test",
        permission_requested=permission)


class FakeRunner:
    """按序吐 HeadlessRunResult / 异常，并记录每次调用的入参。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, task, **kwargs):
        self.calls.append({"task": task, **kwargs})
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sessions(monkeypatch):
    """建连/断连记录器：默认一次建连返回 ssh-1，断连计数。"""
    rec = {"opened": [], "closed": []}

    async def fake_open(connection_id, *, log_tag=""):
        rec["opened"].append(connection_id)
        return "ssh-1", object()

    async def fake_close(bridge, ssh_session_id, *, log_tag=""):
        rec["closed"].append(ssh_session_id)

    monkeypatch.setattr(headless_run, "open_session", fake_open)
    monkeypatch.setattr(headless_run, "close_session", fake_close)
    return rec


@pytest.fixture
def audits(monkeypatch):
    rec: list[dict] = []

    def fake_append(**kwargs):
        rec.append(kwargs)
        return {"id": len(rec)}

    import agent.audit as audit_mod
    monkeypatch.setattr(audit_mod, "append_record", fake_append)
    return rec


# ── 标记行判读（纯函数） ──────────────────────────────────────────────────────

def test_extract_verdict_matrix():
    assert extract_verdict("前文\n[步骤结论] 通过\n尾文") == ("通过", "")
    assert extract_verdict("[步骤结论] 失败: 服务未起来") == ("失败", "服务未起来")
    # 全角冒号
    assert extract_verdict("[步骤结论] 失败：磁盘 95%") == ("失败", "磁盘 95%")
    # 多次输出取最后一条（与巡检判读同口径）
    twice = "[步骤结论] 失败: a\n[步骤结论] 通过"
    assert extract_verdict(twice) == ("通过", "")
    # 缺标记 → None（fail-closed 的输入）
    assert extract_verdict("一切正常，全部完成。") is None
    assert extract_verdict("") is None


def test_step_prompt_carries_command_and_assert():
    prompt = build_step_prompt({
        "command": "systemctl restart nginx",
        "assertCommand": "systemctl is-active nginx",
        "note": "重启后必须验证",
    })
    assert "systemctl restart nginx" in prompt
    assert "systemctl is-active nginx" in prompt
    assert "重启后必须验证" in prompt
    assert "[步骤结论] 通过" in prompt  # 标记行约定写进 prompt


# ── 执行引擎（逐步骤 / 判停 / 诊断 / 取消） ───────────────────────────────────

def _patch_runner(monkeypatch, fake):
    monkeypatch.setattr(headless_run, "run_headless_once", fake)


async def _trigger_and_wait(engine, rb):
    """trigger 是后台任务（create_task），事件循环退出前必须等它跑完 —— 否则
    loop 关停把在途任务 cancel，行被收成 stopped 而不是真实终态。"""
    rrb_id = await engine.trigger(rb)
    for _ in range(500):
        if not engine.is_executing(rb["id"]):
            break
        await asyncio.sleep(0.01)
    return rrb_id


def _last_run(runbook_db, rid):
    runs = rstore.list_runbook_runs(rid)
    return runs[0] if runs else None


def test_pass_step_marks_success_and_run_success(runbook_db, engine, sessions, audits, monkeypatch):
    rb = _mk_runbook([{"command": "echo ok", "assertCommand": "", "note": ""}])
    fake = FakeRunner([_ok()])
    _patch_runner(monkeypatch, fake)

    rrb_id = asyncio.run(_trigger_and_wait(engine, rb))
    assert rrb_id
    run = _last_run(runbook_db, rb["id"])
    assert run["status"] == "success"
    st = run["steps_state"][0]
    assert st["status"] == "success"
    assert st["verdict"] == "通过"
    # 预建会话复用：步骤 run 收到执行体建的 ssh-1，断连恰一次
    assert fake.calls[0]["ssh_session_id"] == "ssh-1"
    assert sessions["closed"] == ["ssh-1"]
    # 步骤审计 conclusion 只用白名单值
    assert audits[0]["action"] == "runbook_step"
    assert audits[0]["conclusion"] == "executed"
    assert audits[-1]["action"] == "runbook_run"
    assert audits[-1]["conclusion"] == "executed"


def test_missing_marker_is_fail_closed(runbook_db, engine, sessions, audits, monkeypatch):
    """缺标记 = 失败（不是成功更不是跳过）。"""
    rb = _mk_runbook([{"command": "echo hi", "assertCommand": "", "note": ""}])
    _patch_runner(monkeypatch, FakeRunner([headless_run.HeadlessRunResult(
        content="命令执行完了，一切看起来正常。", state="completed")]))
    asyncio.run(_trigger_and_wait(engine, rb))
    run = _last_run(runbook_db, rb["id"])
    assert run["status"] == "failed"
    st = run["steps_state"][0]
    assert st["status"] == "failed"
    assert "标记行" in st["error"]


def test_non_completed_state_fails_even_with_pass_marker(runbook_db, engine, sessions, monkeypatch):
    """S7-01 同款：终态非 completed 的 run 不因正文有「通过」就算成功。"""
    rb = _mk_runbook([{"command": "long task", "assertCommand": "", "note": ""}])
    _patch_runner(monkeypatch, FakeRunner([headless_run.HeadlessRunResult(
        content="前文 [步骤结论] 通过", state="max_reached")]))
    asyncio.run(_trigger_and_wait(engine, rb))
    st = _last_run(runbook_db, rb["id"])["steps_state"][0]
    assert st["status"] == "failed"
    assert "max_reached" in st["error"]


def test_blocked_is_failure_not_success(runbook_db, engine, sessions, audits, monkeypatch):
    """拦截（headless 下 permission_needed）→ 步骤 blocked，仍是失败，绝不当成功。"""
    rb = _mk_runbook([{"command": "rm -rf /data", "assertCommand": "", "note": ""}])
    _patch_runner(monkeypatch, FakeRunner([headless_run.HeadlessRunResult(
        content="该命令需要授权，已放弃。", state="completed", permission_requested=True)]))
    asyncio.run(_trigger_and_wait(engine, rb))
    run = _last_run(runbook_db, rb["id"])
    assert run["status"] == "failed"
    st = run["steps_state"][0]
    assert st["status"] == "blocked"
    assert "拦截" in st["error"]
    step_audits = [a for a in audits if a["action"] == "runbook_step"]
    assert step_audits[0]["conclusion"] == "blocked"


def test_blocked_beats_pass_marker(runbook_db, engine, sessions, audits, monkeypatch):
    """评审意见 2 回归：拦截信号优先于模型结论 —— run 出现过 permission_needed
    时，模型末尾的「[步骤结论] 通过」不能把步骤洗成 success（静默假成功）。"""
    rb = _mk_runbook([
        {"command": "rm -rf /data", "assertCommand": "", "note": ""},
        {"command": "echo next", "assertCommand": "", "note": ""},
    ])
    _patch_runner(monkeypatch, FakeRunner([
        headless_run.HeadlessRunResult(
            content="已执行完毕。\n[步骤结论] 通过", state="completed",
            permission_requested=True),
        _ok("诊断：命令被拦截、实际未执行"),
    ]))
    asyncio.run(_trigger_and_wait(engine, rb))
    run = _last_run(runbook_db, rb["id"])
    assert run["status"] == "failed"
    states = run["steps_state"]
    assert states[0]["status"] == "blocked"
    assert states[1]["status"] == "skipped"  # 失败即停照常生效
    assert run["diagnosis"]  # 照常转诊断
    step_audits = [a for a in audits if a["action"] == "runbook_step"]
    assert step_audits[0]["conclusion"] == "blocked"


def test_deny_signal_covers_prod_warning_and_human_help():
    """评审 M1 回归：prod 未预登记不走发卡路径（gate/guards 只发 warning）、
    连续拒绝停机只发 human_help_needed —— 只认 permission_needed 时这些形态下
    permission_requested 恒 False，模型末尾的「[步骤结论] 通过」照样把没执行的
    步骤洗成 success。判据认机器可读的 POLICY_DENY_MARKER，不认中文文案。"""
    assert headless_run._is_deny_signal("permission_needed", {"tool": "shell_exec"})
    assert headless_run._is_deny_signal(
        "warning",
        {"message": f"[生产强化] 生产环境危险工具{POLICY_DENY_MARKER}: undo_restore 仅接受预授权 allowlist 放行。"},
    )
    assert headless_run._is_deny_signal(
        "warning",
        {"message": f"[生产环境]（{POLICY_DENY_MARKER}）这条命令会递归删除数据。"},
    )
    assert headless_run._is_deny_signal("human_help_needed", {"reason": "连续 3 次生产环境策略拒绝"})
    # 预算/墙钟等常规 warning 不是拦截证据，不得把步骤误判成 blocked
    assert not headless_run._is_deny_signal("warning", {"message": "成本预算已达 80%，注意控制"})
    assert not headless_run._is_deny_signal("done", {"state": "completed"})


def test_model_reported_failure_marks_failed(runbook_db, engine, sessions, monkeypatch):
    rb = _mk_runbook([{"command": "curl localhost", "assertCommand": "", "note": ""}])
    _patch_runner(monkeypatch, FakeRunner([_fail()]))
    asyncio.run(_trigger_and_wait(engine, rb))
    st = _last_run(runbook_db, rb["id"])["steps_state"][0]
    assert st["status"] == "failed"
    assert st["verdict"].startswith("失败")
    assert "端口未监听" in st["verdict"]


def test_failure_stops_and_marks_rest_skipped_with_diagnosis(
        runbook_db, engine, sessions, audits, monkeypatch):
    """失败即停：后续步骤置 skipped；自动诊断 run 触发且 prompt 携带失败现场。"""
    rb = _mk_runbook([
        {"command": "echo one", "assertCommand": "", "note": ""},
        {"command": "systemctl restart nginx", "assertCommand": "is-active", "note": "第2步"},
        {"command": "echo three", "assertCommand": "", "note": ""},
    ])
    unconsumed = headless_run.HeadlessRunResult(
        content="第三步不该跑到", state="completed")
    fake = FakeRunner([_ok(), _fail(), _ok("诊断：nginx 配置写错端口"), unconsumed])
    _patch_runner(monkeypatch, fake)
    asyncio.run(_trigger_and_wait(engine, rb))

    run = _last_run(runbook_db, rb["id"])
    assert run["status"] == "failed"
    states = run["steps_state"]
    assert [s["status"] for s in states] == ["success", "failed", "skipped"]
    assert "第 2 步失败" in run["error"]
    # 一共只跑了 3 次无头 run（步1 + 失败步 + 诊断）；第 3 步的哨兵结果未被消费
    assert len(fake.calls) == 3
    assert fake.results == [unconsumed]
    diag_task = fake.calls[2]["task"]
    assert "诊断任务" in diag_task
    assert "systemctl restart nginx" in diag_task   # 失败命令
    assert "端口未监听" in diag_task                  # 失败差异
    assert "第2步" in diag_task                       # 步骤说明
    assert run["diagnosis"]
    # 断连仍然恰一次（诊断 run 复用同一预建会话）
    assert fake.calls[2]["ssh_session_id"] == "ssh-1"
    assert sessions["closed"] == ["ssh-1"]
    # run_id 语义（设计 §3.2，评审意见 4）：最后一个无头 run = 诊断 run，
    # 历史页按 run_id 追现场应指到诊断 run 而非失败步骤
    assert run["run_id"] == fake.calls[2]["run_id"]
    run_audits = [a for a in audits if a["action"] == "runbook_run"]
    assert run_audits[0]["conclusion"] == "failed"


def test_step_exception_fails_the_step(runbook_db, engine, sessions, monkeypatch):
    rb = _mk_runbook([{"command": "boom", "assertCommand": "", "note": ""}])
    _patch_runner(monkeypatch, FakeRunner([RuntimeError("未配置默认模型")]))
    asyncio.run(_trigger_and_wait(engine, rb))
    run = _last_run(runbook_db, rb["id"])
    assert run["status"] == "failed"
    st = run["steps_state"][0]
    assert st["status"] == "failed"
    assert "RuntimeError" in st["error"]


def test_duplicate_trigger_rejected(runbook_db, engine, sessions, monkeypatch):
    """并发防重入：同一剧本在执行中，二次触发被丢弃（路由据此 409）。"""
    rb = _mk_runbook([{"command": "sleep", "assertCommand": "", "note": ""}])
    gate = asyncio.Event()

    class SlowRunner:
        calls = 0

        async def __call__(self, task, **kwargs):
            SlowRunner.calls += 1
            await gate.wait()
            return _ok()

    _patch_runner(monkeypatch, SlowRunner())
    started = asyncio.Event()
    stop = asyncio.Event()

    async def _scenario():
        # monkeypatch 的 fake 挂在 headless_run 模块上；trigger 内 create_task 起真协程
        task = asyncio.get_running_loop().create_task(engine.trigger(rb))
        # 等 trigger 返回（第一行 run 已在 gate 上等）
        for _ in range(200):
            if SlowRunner.calls:
                break
            await asyncio.sleep(0.01)
        second = await engine.trigger(rb)
        started.set()
        gate.set()
        first = await task
        return first, second

    first, second = asyncio.run(_scenario())
    assert first and second is None, "执行中的剧本必须拒绝重复触发"
    assert SlowRunner.calls == 1
    stop.set()


def test_first_trigger_dedupes_across_reap_window(runbook_db, engine, sessions, monkeypatch):
    """评审意见 1 回归：进程内首次触发的收尸（ensure_reaped → to_thread 真实挂起）
    期间，去重标记必须已经登记 —— 挂起窗口内的第二个 trigger 不得通过检查。
    修复前 add 落在 await ensure_reaped() 之后，并发双触发双双起跑（2 行历史、
    2 次无头 run）。"""
    rb = _mk_runbook([{"command": "echo ok", "assertCommand": "", "note": ""}])
    reap_started = threading.Event()
    reap_release = threading.Event()

    def slow_reap(exclude_runbook_ids=None):
        # 同步函数（to_thread 在线程池里真实阻塞）：把首次触发的挂起窗口拉开
        reap_started.set()
        reap_release.wait(timeout=5)
        return 0

    monkeypatch.setattr(rstore, "reap_stale_runs", slow_reap)
    _patch_runner(monkeypatch, FakeRunner([_ok()]))

    async def _scenario():
        loop = asyncio.get_running_loop()
        t1 = loop.create_task(engine.trigger(rb))
        # 不能用阻塞的 Event.wait 等待（会饿死事件循环、t1 根本不跑）——
        # 让出循环轮询 worker 线程设置的标志
        for _ in range(500):
            if reap_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert reap_started.is_set(), "首次触发应挂进收尸窗口"
        # 窗口内去重标记必须已登记（修复前此断言为 False，第二个 trigger 双双起跑）
        assert engine.is_executing(rb["id"]) is True
        second = await engine.trigger(rb)
        reap_release.set()
        first = await t1
        for _ in range(500):
            if not engine.is_executing(rb["id"]):
                break
            await asyncio.sleep(0.01)
        return first, second

    first, second = asyncio.run(_scenario())
    assert first and second is None, "收尸挂起窗口内的重复触发必须被拒"
    assert len(rstore.list_runbook_runs(rb["id"])) == 1, "同一剧本只落一行执行历史"


def test_begin_delete_blocks_trigger(runbook_db, engine, sessions, monkeypatch):
    """评审意见 3 回归：删除占位窗口内 trigger 的同步检查段被拒 —— 不会建出
    将被 FK CASCADE 吞掉的 run 行（执行体对着已删剧本跑完、历史凭空消失）。"""
    rb = _mk_runbook([{"command": "echo ok", "assertCommand": "", "note": ""}])
    _patch_runner(monkeypatch, FakeRunner([_ok()]))

    assert engine.begin_delete(rb["id"]) is True
    assert engine.begin_delete(rb["id"]) is False  # 占位幂等
    assert asyncio.run(engine.trigger(rb)) is None
    assert rstore.list_runbook_runs(rb["id"]) == [], "删除窗口内不得建 run 行"

    engine.end_delete(rb["id"])
    rrb_id = asyncio.run(_trigger_and_wait(engine, rb))
    assert rrb_id, "释放占位后触发恢复正常"


def test_cancel_lands_stopped_terminal_state(runbook_db, engine, sessions, monkeypatch):
    """取消落终态：CancelledError 后 runbook_runs 落 stopped，不滞留 running。"""
    rb = _mk_runbook([{"command": "hang", "assertCommand": "", "note": ""}])
    release = asyncio.Event()

    async def hang_run(task, **kwargs):
        await release.wait()
        return _ok()

    _patch_runner(monkeypatch, hang_run)

    async def _scenario():
        rrb_id = await engine.trigger(rb)
        for _ in range(200):
            if rstore.list_runbook_runs(rb["id"])[0]["steps_state"]:
                break
            await asyncio.sleep(0.01)
        await engine.stop_all()
        release.set()
        return rrb_id

    rrb_id = asyncio.run(_scenario())
    run = [r for r in rstore.list_runbook_runs(rb["id"]) if r["id"] == rrb_id][0]
    assert run["status"] == "stopped"
    assert engine.is_executing(rb["id"]) is False
