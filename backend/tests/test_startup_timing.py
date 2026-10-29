"""启动时序守卫。

锁两条不变量，它们是 main.py 的 lifespan 重构的回归闸门：

1. **单轮分派不退化** —— init_db 之后的七步 DB/配置回填必须并发。任何一步被
   挪回 await 串行位置，耗时就会线性叠加（本文件用「每步 sleep 0.04s，总耗时
   必须远小于求和」把它钉住）。
2. **就绪耗时必须可观测** —— 汇总行在默认日志级别（用户配置默认 warning）下
   落地；明细行只在 CKUN_PERF=1 时出现。原实现的就绪行是 logger.info，跑在
   logconfig.load_from_db 把 root 降到 warning 之后，默认配置下永远不输出。

外部副作用（真库、真渠道、真后台循环）全部替换成可控桩：这里验的是时序与
日志，不是那些组件本身。
"""
import asyncio
import logging
import threading
import time

import pytest

import main


class _FakeBackground:
    """_start_background_tasks 的替身：lifespan 只做转发。"""


class _Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.events: list[tuple[str, float]] = []

    def enter(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self):
        with self.lock:
            self.active -= 1

    def mark(self, name):
        self.events.append((name, time.perf_counter()))

    def at(self, name) -> float:
        return next(at for label, at in self.events if label == name)


def _step(rec: _Recorder, name: str, delay: float):
    """同步桩：跑在 to_thread 的 worker 里，进出各记一次在跑线程数。"""

    def _fn(*_args, **_kwargs):
        rec.enter()
        try:
            if delay:
                time.sleep(delay)
        finally:
            rec.leave()
            rec.mark(name)

    return _fn


def _install_stubs(monkeypatch, rec: _Recorder, delays: dict) -> None:
    """把 lifespan 的每个步骤替换成按 delays 睡眠的桩，并清空打点累积。"""
    monkeypatch.setattr(main, "_marks", [])

    monkeypatch.setattr(main, "_import_permission_modules", lambda: (lambda _v: None,) * 3)
    monkeypatch.setattr(
        main, "_apply_permission_policy",
        _step(rec, "permission", delays.get("permission", 0)),
    )
    monkeypatch.setattr(
        main.db, "init_db", _step(rec, "init_db", delays.get("init_db", 0)),
    )
    monkeypatch.setattr(
        "logconfig.load_from_db", _step(rec, "logconfig", delays.get("logconfig", 0)),
    )
    monkeypatch.setattr(
        "netconfig.load_policy_from_db",
        _step(rec, "net_policy", delays.get("net_policy", 0)),
    )
    monkeypatch.setattr(
        main, "_sync_skills", _step(rec, "skills", delays.get("skills", 0)),
    )
    monkeypatch.setattr(
        main, "_load_post_config", _step(rec, "post_config", delays.get("post_config", 0)),
    )
    monkeypatch.setattr(
        main, "_reset_stale", _step(rec, "reset_stale", delays.get("reset_stale", 0)),
    )

    async def _fake_seed(_db_path):
        rec.enter()
        try:
            if delays.get("seed"):
                await asyncio.sleep(delays["seed"])
        finally:
            rec.leave()
            rec.mark("seed")

    monkeypatch.setattr(main.db, "seed_models_async", _fake_seed)
    monkeypatch.setattr(main, "_start_background_tasks", lambda: _FakeBackground())

    async def _noop_stop(_bg):
        return None

    monkeypatch.setattr(main, "_stop_background_tasks", _noop_stop)


def _run_lifespan() -> float:
    async def _run():
        async with main.lifespan(None):
            pass

    started = time.perf_counter()
    asyncio.run(_run())
    return time.perf_counter() - started


_ALL_STEPS = (
    "logconfig", "init_db", "net_policy", "skills", "post_config", "permission",
    "reset_stale", "seed",
)


def test_db_read_steps_run_in_one_round(monkeypatch):
    """七步并发：总耗时远小于串行求和，且同时在跑的桩不止一个。

    串行下限 8 × 0.04s = 0.32s；单轮分派实测 ≈0.04–0.08s（默认线程池
    min(32, cpu+4) 个 worker）。阈值取 0.15s：既留出调度余量，又能在
    「被改成顺序 await」时稳定失败。
    """
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, {name: 0.04 for name in _ALL_STEPS})

    elapsed = _run_lifespan()

    assert rec.peak >= 3, f"启动步骤没有并发：峰值同时在跑 {rec.peak} 个"
    assert elapsed < 0.15, f"启动步骤疑似退回串行：耗时 {elapsed:.3f}s（串行 ≥0.32s）"


def test_ready_summary_is_always_logged(monkeypatch, caplog):
    """汇总行必须在默认级别（warning）落地 —— 它是后端唯一的自报耗时出口。"""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, {})

    with caplog.at_level(logging.WARNING):
        _run_lifespan()

    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[perf] startup")]
    assert summary, f"就绪汇总行缺失，实际记录：{[r.getMessage() for r in caplog.records]}"
    for field in ("imports=", "assemble=", "boot=", "lifespan=", "total=",
                  "init_db=", "round1=", "background="):
        assert field in summary[0], f"汇总行缺少 {field}: {summary[0]}"


def test_perf_detail_lines_require_env(monkeypatch, caplog):
    """明细行按需：默认一条不刷，CKUN_PERF=1（模块常量置位）才出现。"""
    monkeypatch.setattr(main, "_marks", [])

    monkeypatch.setattr(main, "_PERF_VERBOSE", False)
    with caplog.at_level(logging.WARNING):
        main._perf_mark("detail-off")
    assert "[perf] detail-off" not in caplog.text, "未开 CKUN_PERF 却输出了明细行"

    caplog.clear()
    monkeypatch.setattr(main, "_PERF_VERBOSE", True)
    with caplog.at_level(logging.WARNING):
        main._perf_mark("detail-on")
    assert "[perf] detail-on @" in caplog.text, "开了 CKUN_PERF 仍无明细行"


def test_seed_only_waits_for_net_policy(monkeypatch):
    """模型探测只等 netconfig 那一步，不等整轮 —— 细粒度事件的实际收益。

    netconfig 40ms、技能同步 400ms：若 _seed_models 等整轮，探测会晚于技能
    完成（≥400ms）；只等 net_ready 则应在 40–80ms 之间就发起。
    """
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, {"net_policy": 0.04, "skills": 0.4})

    started = time.perf_counter()
    _run_lifespan()
    total = time.perf_counter() - started

    assert rec.at("seed") < rec.at("skills"), (
        f"模型探测等到了整轮结束（seed@{rec.at('seed') - started:.3f}s, "
        f"skills@{rec.at('skills') - started:.3f}s）"
    )
    assert total >= 0.4, "技能同步没有真正占用时间，本用例失去意义"


def test_lifespan_teardown_runs_on_body_exception(monkeypatch):
    """应用体抛异常时收尾仍执行：裸 yield 之后的代码在 throw 时会被跳过，
    后台循环与渠道就成了孤儿。"""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, {})
    stopped: list[int] = []

    async def _stop(_bg):
        stopped.append(1)

    monkeypatch.setattr(main, "_stop_background_tasks", _stop)

    async def _run():
        with pytest.raises(RuntimeError):
            async with main.lifespan(None):
                raise RuntimeError("body boom")

    asyncio.run(_run())
    assert stopped == [1], "应用体异常时后台任务没有被关停"
