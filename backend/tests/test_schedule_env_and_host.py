"""定时任务的环境与目标主机 —— 不能绕过环境判定，也不能漏掉主机限域。

背景：`_run_agent_once` 原先 `environment="prod"` 硬编码、且没传 `target_host`。

- 环境：test 主机上的巡检被当成生产强化（危险命令必须预登记），审计环境列
  也记成 prod —— 「审计页的 test 视图里看不到这次运行」。
- 主机：prod 主机上预登记的 `@prod-db|...` 条目在定时任务里永远不命中，
  主机限域授权对这条路径等于失效。

运行: python -m pytest backend/tests/test_schedule_env_and_host.py -v
"""
import asyncio
from types import SimpleNamespace

import pytest

import agent.schedule_engine as se
import agent.tools as tools_mod
from agent.environment_policy import EnvironmentResolution
from agent.schedule_engine import ScheduleEngine, _schedule_environment

SCHED = {"id": "s-1", "name": "每日巡检", "target_host": "conn-1",
         "task_prompt": "检查磁盘"}


# ── 环境判定：主机配置说了算，查不到按 prod 保守 ────────────────────────────

def test_环境以主机连接配置为准():
    assert _schedule_environment(EnvironmentResolution(
        effective="test", requested="test",
        source="connection", host="test-box")) == "test"
    assert _schedule_environment(EnvironmentResolution(
        effective="prod", requested="test",
        source="connection", host="prod-db", tightening=True)) == "prod"


@pytest.mark.parametrize("source", [
    "bridge_unavailable", "connection_unknown", "client",
])
def test_查不到主机配置时按_prod_保守(source):
    """定时任务无人值守：没有「用户在场兜底」，客户端声明的 test 不可信。

    这条兜底与原先硬编码 prod 同姿态 —— 收紧方向，不会放宽任何判定。
    """
    assert _schedule_environment(EnvironmentResolution(
        effective="test", requested="test", source=source)) == "prod"


# ── 接线：harness.run 必须收到这两个参数 ──────────────────────────────────

# 模块级而非类属性：类属性写成可变默认值会触发 RUF012，本文件也只关心"最近一次调用"
_SEEN: dict = {}


class _FakeHarness:
    """只记录 run(...) 收到了什么，不跑真实驱动。"""

    def __init__(self, _cfg):
        pass

    def assemble(self, **_kw):
        return None

    async def run(self, **kwargs):
        _SEEN.clear()
        _SEEN.update(kwargs)
        yield SimpleNamespace(
            kind="done", payload={"content": "ok", "state": "completed"})


class _FakeBridge:
    def is_available(self):
        return True

    async def probe(self):
        return None

    async def connect_ssh(self, connection_id):
        assert connection_id == "conn-1", "sched.target_host 是连接 ID，不是主机名"
        return {"sessionId": "ssh-9"}

    async def disconnect_ssh(self, _session_id):
        return None


def _run_once(monkeypatch, resolution: EnvironmentResolution):
    engine = ScheduleEngine()
    monkeypatch.setattr(engine, "_resolve_model", lambda: {
        "id": "m1", "model_id": "m1", "base_url": "http://x",
        "api_key": "k", "max_tokens": 8192,
    })
    # 执行栈在 _run_agent_once 内按需导入（启动速度优化），patch 必须打到定义处：
    # 模块内 `from agent.harness import Harness` 会经包级 __getattr__ 取到这里的桩
    monkeypatch.setattr("agent.harness.harness.Harness", _FakeHarness)
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", lambda **_kw: object())
    monkeypatch.setattr("agent.cost_tracker.CostTracker", lambda **_kw: object())
    monkeypatch.setattr(se, "ElectronBridge", _FakeBridge)
    monkeypatch.setattr(se, "_build_schedule_system_prompt", lambda _s: "sp")
    monkeypatch.setattr(tools_mod, "create_registry", lambda: object())

    async def _resolve(_requested, _session_id=""):
        return resolution

    monkeypatch.setattr(se, "resolve_run_environment", _resolve)
    _SEEN.clear()
    content, error, state, host = asyncio.run(engine._run_agent_once(SCHED, "run-1"))
    assert (content, error, state) == ("ok", "", "completed")
    assert host == resolution.host
    return _SEEN


def test_test_主机的定时任务不再按生产强化跑(monkeypatch):
    seen = _run_once(monkeypatch, EnvironmentResolution(
        effective="test", requested="test",
        source="connection", host="test-box"))
    assert seen["environment"] == "test"
    assert seen["target_host"] == "test-box", (
        "主机限域授权按连接配置的 host 匹配，传连接 ID 会永不命中")


def test_prod_主机的定时任务能命中主机限域预登记(monkeypatch):
    seen = _run_once(monkeypatch, EnvironmentResolution(
        effective="prod", requested="test",
        source="connection", host="prod-db"))
    assert seen["environment"] == "prod"
    assert seen["target_host"] == "prod-db"


def test_环境未核实时报_prod(monkeypatch):
    seen = _run_once(monkeypatch, EnvironmentResolution(
        effective="test", requested="test", source="bridge_unavailable"))
    assert seen["environment"] == "prod", "查不到主机配置不得按客户端声明放宽"


# ── 巡检沉淀：标题里的主机名得是人能认、能检索的那个 ─────────────────────

def test_巡检报告标题用真实主机名(monkeypatch):
    """sched['target_host'] 存的是连接 ID（用户在下拉里选的是连接）。

    直接拿它当主机名写进长期记忆，标题就是「巡检异常 conn-3f2a」—— 除了
    那个人谁都看不懂，按主机名检索也搜不到。
    """
    from agent.memory import mstore as mstore_mod

    captured: dict = {}
    monkeypatch.setattr(mstore_mod, "add_memory", lambda **kw: captured.update(kw))
    monkeypatch.setattr(se.db, "set_schedule_last_abnormal", lambda *_a, **_k: None)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(se, "_notify_webhook", _noop)

    sched = {"id": "s-1", "name": "每日巡检", "target_host": "conn-3f2a"}
    asyncio.run(ScheduleEngine._handle_inspection_verdict(
        sched, "报告\n[巡检结论] 异常: 磁盘 92%", "prod-db"))

    assert "prod-db" in captured["title"]
    assert "conn-3f2a" not in captured["title"]
    assert "[主机] prod-db" in captured["content"]
