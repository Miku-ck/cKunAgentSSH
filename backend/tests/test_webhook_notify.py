"""巡检异常 webhook 推送单测（方案 Phase 4）。

覆盖：未配置不发、配置后异常才发（企微/钉钉 msgtype:text 格式）、
正常巡检不发、HTTP 失败重试 1 次后放弃且不阻塞沉淀主流程。
"""
import asyncio
from pathlib import Path

import pytest

import database as db
import agent.schedule_engine as se
from agent.memory import mstore
from agent.schedule_engine import ScheduleEngine


@pytest.fixture
def memdb():
    """与 test_memory_v2 共用同一个库文件 + 每测试清表（线程本地连接不能换路径）。"""
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    path = base / "memv2-test.db"
    original = db._db_path
    # 全量回归时前面的模块可能把线程绑定到别的库：close-all 让所有线程
    # 在下一次 _get_conn 时按当前 _db_path 重绑（自定义路径测试的既有约定）。
    # 本模块混用主线程写 + to_thread 读写，绑定不一致会直接幽灵数据。
    db._close_all_connections()
    db.init_db(str(path))
    conn = db._get_conn()
    for t in ("memories", "memory_jobs", "memory_audit", "schedules", "app_settings"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    mstore.reset_flags()
    yield
    mstore.reset_flags()
    db._close_all_connections()
    if original:
        db.init_db(original)


class _FakeResp:
    def raise_for_status(self):
        pass


class FakeClient:
    """httpx.AsyncClient 桩：类级记录调用，行为开关见各测试。"""

    calls: list = []
    fail_times: int = 0  # >0 时前 N 次 POST 抛异常

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        FakeClient.calls.append({"url": url, "json": json})
        if FakeClient.fail_times > 0:
            FakeClient.fail_times -= 1
            raise RuntimeError("HTTP 炸了")
        return _FakeResp()


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    FakeClient.calls = []
    FakeClient.fail_times = 0
    # httpx 在 _notify_webhook 里按需导入（启动路径上不付它的 130ms），所以 patch
    # 定义处 httpx.AsyncClient —— 函数体内 `import httpx` 拿到的就是同一模块对象。
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    yield


def _sched(**kw) -> dict:
    data = {"name": "每日巡检", "schedule_type": "interval",
            "interval_minutes": 60, "target_host": "host-a", "task_prompt": ""}
    data.update(kw)
    return db.create_schedule(data)


def _abnormal_report() -> str:
    return "## 总体结论\n有异常。\n[巡检结论] 异常: 根分区 92%"


def _normal_report() -> str:
    return "## 总体结论\n正常。\n[巡检结论] 正常"


def test_no_url_no_request(memdb):
    s = _sched()
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _abnormal_report()))
    assert FakeClient.calls == []
    # 沉淀本身照常完成
    assert mstore.list_memories(kind="inspection_report", limit=10)


def test_abnormal_pushes_text_message(memdb):
    s = _sched()
    db.set_app_setting("notify.webhook_url", "https://example.com/hook")
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _abnormal_report()))
    assert len(FakeClient.calls) == 1
    call = FakeClient.calls[0]
    assert call["url"] == "https://example.com/hook"
    assert call["json"]["msgtype"] == "text"
    content = call["json"]["text"]["content"]
    assert content.startswith("[cKunAgentSSH 巡检异常]")
    assert "host-a" in content and "根分区 92%" in content


def test_normal_verdict_no_push(memdb):
    s = _sched()
    db.set_app_setting("notify.webhook_url", "https://example.com/hook")
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _normal_report()))
    assert FakeClient.calls == []


def test_empty_url_is_off(memdb):
    s = _sched()
    db.set_app_setting("notify.webhook_url", "")
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _abnormal_report()))
    assert FakeClient.calls == []


def test_http_failure_retries_once_and_does_not_block(memdb):
    s = _sched()
    db.set_app_setting("notify.webhook_url", "https://example.com/hook")
    FakeClient.fail_times = 99  # 两次全失败
    # 不抛异常即通过：推送失败不影响巡检主流程
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _abnormal_report()))
    assert len(FakeClient.calls) == 2  # 首次 + 重试 1 次
    # 沉淀与状态回写照常完成
    assert mstore.list_memories(kind="inspection_report", limit=10)
    assert db.get_schedule(s["id"])["last_abnormal_at"]


def test_retry_succeeds_on_second_attempt(memdb):
    s = _sched()
    db.set_app_setting("notify.webhook_url", "https://example.com/hook")
    FakeClient.fail_times = 1  # 首次失败，重试成功
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _abnormal_report()))
    assert len(FakeClient.calls) == 2


def test_notify_webhook_direct_silent_when_setting_read_fails(memdb, monkeypatch):
    """读取配置本身炸掉也静默（降级铁律的全包兜底）。"""
    def boom(*a, **k):
        raise RuntimeError("DB 炸了")
    monkeypatch.setattr(se.db, "get_app_setting", boom)
    asyncio.run(se._notify_webhook("x"))  # 不抛异常即通过
    assert FakeClient.calls == []
