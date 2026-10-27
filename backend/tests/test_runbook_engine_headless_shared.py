"""headless_run 装配契约守卫（schedule_engine._run_agent_once 的语义对齐独立实现）：
- bridge 不可达明确报错不降级（is_available / probe / 空 sessionId 三道闸）；
- 环境判定走 resolve_run_environment 且只收紧（connection 判 prod 即 prod）；
  查不到主机配置按 prod 兜底（无人值守姿态）；
- 预建会话模式下不自建、不断开（复用由调用方 finally 关闭）；
- 自建模式下任务结束必关 SSH 会话（含异常路径）；
- run_id 传入 harness 供审计贯通；done/error/permission_needed 信号收集。
"""
import asyncio
import os
from types import SimpleNamespace

import pytest

import database as db
from agent import headless_run
from agent.environment_policy import EnvironmentResolution
from agent.electron_bridge import BridgeError, BridgeErrorCode
from prompts import SSH_MODE


def _model_cfg(**over):
    cfg = {
        "id": "m1", "model_id": "glm-test", "base_url": "http://llm.test/v1",
        "api_key": "sk-test", "max_tokens": 8192,
        "temperature": 0.3, "top_p": 0.8, "reasoning_tokens": 0,
    }
    cfg.update(over)
    return cfg


class FakeHarness:
    """记录 assemble/run 入参；事件序列走类属性（测试在调用前注入）。"""
    last: "FakeHarness | None" = None
    last_run_kwargs: dict | None = None
    queue: list = []
    raise_in_run: Exception | None = None

    def __init__(self, cfg):
        self.cfg = cfg
        self.assemble_kwargs: dict | None = None
        FakeHarness.last = self

    def assemble(self, **kwargs):
        self.assemble_kwargs = kwargs

    def run(self, **kwargs):
        FakeHarness.last_run_kwargs = dict(kwargs)

        async def _gen():
            for ev in FakeHarness.queue:
                yield ev
            if FakeHarness.raise_in_run is not None:
                raise FakeHarness.raise_in_run

        return _gen()


def _done(content="ok", state="completed"):
    return SimpleNamespace(kind="done", payload={"content": content, "state": state})


@pytest.fixture
def wired(monkeypatch):
    """装配缝全量打桩：模型/环境可注入，Harness 用 FakeHarness；每测重置类状态。"""
    FakeHarness.last = None
    FakeHarness.last_run_kwargs = None
    FakeHarness.queue = [_done()]
    FakeHarness.raise_in_run = None

    state = {"model": _model_cfg(), "resolution": EnvironmentResolution(
        effective="test", requested="test", source="client")}
    monkeypatch.setattr(
        headless_run, "resolve_model", lambda: state["model"])

    async def fake_resolve_env(requested, ssh_session_id=""):
        return state["resolution"]

    monkeypatch.setattr(headless_run, "resolve_run_environment", fake_resolve_env)
    monkeypatch.setattr(headless_run, "Harness", FakeHarness)
    monkeypatch.setattr(headless_run, "StreamLLMClient",
                        lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(headless_run, "CostTracker", lambda **kw: SimpleNamespace(**kw))
    import agent.tools as tools_mod
    monkeypatch.setattr(tools_mod, "create_registry", lambda: object())
    return state


@pytest.fixture
def base_db(tmp_workspace):
    db_path = os.path.join(tmp_workspace, "runbook_headless.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


# ── done / error / run_id / permission 信号收集 ──────────────────────────────

def test_done_error_and_run_id_collected(wired):
    FakeHarness.queue = [
        SimpleNamespace(kind="error", payload={"message": "LLM 超时一次"}),
        SimpleNamespace(kind="permission_needed", payload={}),
        _done(content="报告正文"),
    ]
    result = asyncio.run(headless_run.run_headless_once(
        "任务", run_id="run-xyz", conversation_id="conv-1", log_tag="t"))
    assert result.content == "报告正文"
    assert result.state == "completed"
    assert result.error == "LLM 超时一次"
    assert result.permission_requested is True
    # run_id 原样传给 harness（审计链按 run_id 贯通，audit_log.idx_audit_run）
    assert FakeHarness.last_run_kwargs["run_id"] == "run-xyz"
    assert FakeHarness.last_run_kwargs["conversation_id"] == "conv-1"


# ── 环境判定：只收紧，不放宽 ─────────────────────────────────────────────────

def test_environment_tightened_to_prod_from_connection(wired):
    wired["resolution"] = EnvironmentResolution(
        effective="prod", requested="test", source="connection",
        host="web-1", connection_id="conn-9")
    result = asyncio.run(headless_run.run_headless_once(
        "任务", run_id="r1", conversation_id="c1", log_tag="t"))
    # 主机连接配置判 prod → 本次 run 必须按 prod 跑（收紧不可放宽）
    assert FakeHarness.last_run_kwargs["environment"] == "prod"
    assert FakeHarness.last_run_kwargs["target_host"] == "web-1"
    assert result.environment == "prod"
    assert result.host == "web-1"


def test_environment_falls_back_to_prod_when_unresolvable(wired):
    """查不到主机配置（source=bridge_unavailable）按 prod 兜底，不放宽为 test。"""
    wired["resolution"] = EnvironmentResolution(
        effective="test", requested="test", source="bridge_unavailable")
    result = asyncio.run(headless_run.run_headless_once(
        "任务", run_id="r1", conversation_id="c1", log_tag="t"))
    assert FakeHarness.last_run_kwargs["environment"] == "prod"
    assert result.environment == "prod"


# ── SSH 会话模式 ─────────────────────────────────────────────────────────────

class FakeBridge:
    def __init__(self, *, available=True, probe_err=None, session_id="ssh-9"):
        self._available = available
        self._probe_err = probe_err
        self._session_id = session_id
        self.connected: list[str] = []
        self.disconnected: list[str] = []

    def is_available(self):
        return self._available

    async def probe(self):
        return self._probe_err

    async def connect_ssh(self, connection_id, timeout=60.0):
        self.connected.append(connection_id)
        return {"sessionId": self._session_id or ""}

    async def disconnect_ssh(self, session_id, timeout=15.0):
        self.disconnected.append(session_id)
        return {}


def test_bridge_unavailable_fails_closed_no_downgrade(base_db, wired, monkeypatch):
    """bridge 不可达 → BridgeError 明确报错；绝不降级、绝不静默跳过建连。"""
    bridge = FakeBridge(available=False)
    monkeypatch.setattr(headless_run, "ElectronBridge", lambda: bridge)

    async def _scenario():
        FakeHarness.queue = []
        return await headless_run.run_headless_once(
            "任务", run_id="r1", conversation_id="c1", connection_id="conn-1", log_tag="t")

    with pytest.raises(BridgeError):
        asyncio.run(_scenario())
    assert bridge.connected == []  # 未配置态直接拒绝，建连从未发生
    # harness.run 从未被调到（装配在会话阶段就 fail-closed 了）
    assert FakeHarness.last_run_kwargs is None


def test_bridge_probe_failure_fails_closed(base_db, wired, monkeypatch):
    bridge = FakeBridge(available=True, probe_err=BridgeErrorCode.WS_LOST)
    monkeypatch.setattr(headless_run, "ElectronBridge", lambda: bridge)

    async def _scenario():
        return await headless_run.run_headless_once(
            "任务", run_id="r1", conversation_id="c1", connection_id="conn-1", log_tag="t")

    with pytest.raises(BridgeError):
        asyncio.run(_scenario())
    assert bridge.connected == []


def test_empty_session_id_is_bridge_error(base_db, wired, monkeypatch):
    bridge = FakeBridge(available=True, probe_err=None, session_id="")
    monkeypatch.setattr(headless_run, "ElectronBridge", lambda: bridge)

    async def _scenario():
        return await headless_run.run_headless_once(
            "任务", run_id="r1", conversation_id="c1", connection_id="conn-1", log_tag="t")

    with pytest.raises(BridgeError):
        asyncio.run(_scenario())


def test_selfbuilt_session_closed_on_success_and_error(base_db, wired, monkeypatch):
    """自建模式：成功与异常路径都必须关闭会话（finally）。"""
    bridge = FakeBridge(available=True, probe_err=None, session_id="ssh-9")
    monkeypatch.setattr(headless_run, "ElectronBridge", lambda: bridge)

    result = asyncio.run(headless_run.run_headless_once(
        "任务", run_id="r1", conversation_id="c1", connection_id="conn-1", log_tag="t"))
    assert FakeHarness.last_run_kwargs["ssh_session_id"] == "ssh-9"
    assert bridge.connected == ["conn-1"]
    assert bridge.disconnected == ["ssh-9"]

    bridge2 = FakeBridge()
    monkeypatch.setattr(headless_run, "ElectronBridge", lambda: bridge2)
    FakeHarness.raise_in_run = RuntimeError("harness 内部炸了")

    async def _error():
        return await headless_run.run_headless_once(
            "任务", run_id="r2", conversation_id="c1", connection_id="conn-1", log_tag="t")

    with pytest.raises(RuntimeError):
        asyncio.run(_error())
    assert bridge2.disconnected == ["ssh-9"], "异常路径泄漏连接"


def test_prebuilt_session_not_opened_not_closed(base_db, wired, monkeypatch):
    """预建会话模式：headless_run 不自建、不断开 —— 建连/断连责任归调用方
    （executor 每执行 1 次建连、finally 统一断开）。"""
    opened: list[str] = []
    closed: list[str] = []

    async def fake_open(connection_id, *, log_tag=""):
        opened.append(connection_id)
        return "ssh-other", object()

    async def fake_close(bridge, ssh_session_id, *, log_tag=""):
        closed.append(ssh_session_id)

    monkeypatch.setattr(headless_run, "open_session", fake_open)
    monkeypatch.setattr(headless_run, "close_session", fake_close)

    result = asyncio.run(headless_run.run_headless_once(
        "任务", run_id="r1", conversation_id="c1", ssh_session_id="ssh-7", log_tag="t"))
    assert opened == []
    assert closed == []
    assert FakeHarness.last_run_kwargs["ssh_session_id"] == "ssh-7"
    assert result.state == "completed"


# ── 模型解析（SSH 档 → 全局档）与未配置 fail-fast ────────────────────────────

def test_resolve_model_prefers_ssh_profile(monkeypatch):
    calls: list = []

    def fake_get_default_model(mode, mask_key=True):
        calls.append(mode)
        if mode == SSH_MODE:
            return None
        return {"id": "m-global", "model_id": "gpt-x"}

    def fake_get_model_config(mid, mask_key=True):
        return _model_cfg(id=mid, model_id="gpt-x")

    monkeypatch.setattr(db, "get_default_model", fake_get_default_model)
    monkeypatch.setattr(db, "get_model_config", fake_get_model_config)
    cfg = headless_run.resolve_model()
    assert calls == [SSH_MODE, None]
    assert cfg["model_id"] == "gpt-x"


def test_run_without_default_model_raises(base_db, wired, monkeypatch):
    monkeypatch.setattr(headless_run, "resolve_model", lambda: None)

    async def _scenario():
        return await headless_run.run_headless_once(
            "任务", run_id="r1", conversation_id="c1", log_tag="t")

    with pytest.raises(RuntimeError, match="未配置默认模型"):
        asyncio.run(_scenario())
