"""S7-09 回归：AI 排的班必须能带上目标主机。

原先 `target_host` 既不在工具 schema 里、`_create` 又硬编码 "" —— 模型无从传，
于是「每天 3 点巡检 web-01」建出来的任务天天跑、天天「已完成」、天天什么都没查
（沉默降级，拼上 S7-02 的「无会话就不假装能执行远程命令」才是完整一条假动作）。
"""
import asyncio

import pytest

import agent.electron_bridge as eb
import agent.schedule_engine as se
import agent.tools.builtin.ops_schedule as osch
import database as db

_CONNS = [
    {"id": "conn-1", "name": "web-01", "host": "10.0.0.1", "port": 22,
     "username": "root", "authType": "key", "environment": "test"},
    {"id": "conn-2", "name": "db-01", "host": "10.0.0.2", "port": 22,
     "username": "admin", "authType": "key", "environment": "prod"},
    {"id": "conn-3", "name": "web-01", "host": "10.0.0.3", "port": 22,
     "username": "root", "authType": "key", "environment": "test"},
]


class _Bridge:
    def __init__(self, *, available=True, raises=None):
        self._available = available
        self._raises = raises

    def is_available(self):
        return self._available

    async def list_connections(self):
        if self._raises:
            raise self._raises
        return list(_CONNS)


def _patch_bridge(monkeypatch, **kw):
    monkeypatch.setattr(eb, "ElectronBridge", lambda *a, **k: _Bridge(**kw))


def test_id_name_and_user_at_host_all_resolve(monkeypatch):
    _patch_bridge(monkeypatch)
    for raw, expected in (("conn-2", "conn-2"), ("db-01", "conn-2"),
                          ("admin@10.0.0.2", "conn-2")):
        host_id, note, err = asyncio.run(osch._resolve_target_host(raw))
        assert err == ""
        assert host_id == expected, f"{raw} 应解析成 {expected}，实际 {host_id}"
        assert note


def test_ambiguous_name_is_rejected_with_the_candidates(monkeypatch):
    """两条同名连接不能让模型随机拿到其中一条。"""
    _patch_bridge(monkeypatch)
    host_id, _note, err = asyncio.run(osch._resolve_target_host("web-01"))
    assert host_id == ""
    assert "命中" in err and "conn-1" in err and "conn-3" in err


def test_unknown_host_lists_what_exists(monkeypatch):
    """查不到时报错并列出可用目标 —— 这就是模型的发现路径。"""
    _patch_bridge(monkeypatch)
    host_id, _note, err = asyncio.run(osch._resolve_target_host("nope"))
    assert host_id == ""
    assert "conn-1" in err and "10.0.0.1" in err


def test_bridge_down_accepts_value_and_says_it_is_unverified(monkeypatch):
    """探活失败不该挡住建任务，但必须写明「未校验」。"""
    _patch_bridge(monkeypatch, available=False)
    host_id, note, err = asyncio.run(osch._resolve_target_host("conn-9"))
    assert err == "" and host_id == "conn-9"
    assert "未校验" in note


def test_empty_target_is_legal_but_says_no_remote_session():
    host_id, note, err = asyncio.run(osch._resolve_target_host(""))
    assert err == "" and host_id == ""
    assert "不会建立 SSH 会话" in note


def test_create_stores_resolved_id(monkeypatch):
    """端到端一条：模型写 db-01，落库的 target_host 必须是 conn-2。"""
    _patch_bridge(monkeypatch)
    captured: dict = {}

    def fake_create(payload):
        captured.update(payload)
        return {"id": "sch-1"}

    monkeypatch.setattr(db, "create_schedule", fake_create)
    monkeypatch.setattr(se, "compute_next_run", lambda _s: "2026-01-01 03:00:00")
    monkeypatch.setattr(se, "get_schedule_engine", lambda: type("_E", (), {"ensure_started": lambda self: None})())

    tool = osch.ScheduleOpTool()
    result = asyncio.run(tool.execute(
        None, action="create", mode="interval", interval_minutes=60,
        task="巡检磁盘水位", target_host="db-01",
    ))
    assert result.success is True, result.error
    assert captured["target_host"] == "conn-2"
    assert "db-01" in result.output or "10.0.0.2" in result.output


@pytest.mark.parametrize("raw", [None, 123, "   "])
def test_non_string_target_host_does_not_crash(raw, monkeypatch):
    _patch_bridge(monkeypatch)
    host_id, note, err = asyncio.run(osch._resolve_target_host(raw))
    assert (host_id, err) == ("", "")
    assert "不会建立 SSH 会话" in note
