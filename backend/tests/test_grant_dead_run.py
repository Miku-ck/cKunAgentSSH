# -*- coding: utf-8 -*-
"""P2 回归（审批审查 2026-10-11）：scope=once 授权必须校验目标 run 非终态。

修复前：run 已终态后（审批兜底到期、SSE 迟到/断连、settling 竞态窗口）点审批卡
「允许本次」，once 授权落进死 run 键 —— cleanup_run_once 已清过该 run 的池，
此刻写入的键永不被消费也不再被清理（死键泄漏），前端却显示「已授权」。

修复后语义（本文件四组用例钉住）：
  · run 行缺失 / 状态 ∈ TERMINAL_RUN_STATES + scope=once → 409；
  · run 运行中 + scope=once → 照常授权；
  · 终态 run + scope=session / always → 不拦（按会话键控 / 永久，跨 run 有意义）；
  · 状态查询本身异常 → 告警放行（这是完整性守卫不是安全闸门，拿不到证据
    不等于 run 已死；once 键消费时 PermissionGate 本就还要查表）。
"""
import asyncio

import pytest
from fastapi import HTTPException

import database as db
import routes.agent as agent_mod
from agent.harness.reins import PermissionGate
from routes.agent import PermissionGrantRequest, grant_tool_permission


@pytest.fixture
def gate_env(monkeypatch):
    """屏蔽 allowlist.txt 宿主文件 IO 与审计落库（本文件只关心端点裁决本身）。"""
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())

    async def _noop_audit(*a, **k):
        return None

    monkeypatch.setattr(agent_mod, "append_record_async", _noop_audit)


def _patch_run(monkeypatch, row):
    monkeypatch.setattr(db, "get_agent_run", lambda run_id: row)


def test_once_授权_目标run已终态_409(monkeypatch, gate_env):
    _patch_run(monkeypatch, {"run_id": "run-dead", "status": "completed"})
    with pytest.raises(HTTPException) as ei:
        asyncio.run(grant_tool_permission(PermissionGrantRequest(
            tool="shell_exec", scope="once", command="rm -f /tmp/x", run_id="run-dead")))
    assert ei.value.status_code == 409


def test_once_授权_目标run运行中_照常放行(monkeypatch, gate_env):
    _patch_run(monkeypatch, {"run_id": "run-live", "status": "running"})
    body = asyncio.run(grant_tool_permission(PermissionGrantRequest(
        tool="shell_exec", scope="once", command="rm -f /tmp/x", run_id="run-live")))
    assert body["granted"] is True


def test_once_授权_run行缺失_409(monkeypatch, gate_env):
    # 审批卡只在 run 运行中产生，届时 DB 行必已存在；缺行说明 run_id 本身不可信
    _patch_run(monkeypatch, None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(grant_tool_permission(PermissionGrantRequest(
            tool="shell_exec", scope="once", command="rm -f /tmp/x", run_id="run-ghost")))
    assert ei.value.status_code == 409


def test_终态run的session与always授权不拦(monkeypatch, gate_env):
    # session 按会话键控、always 落永久 allowlist —— run 结束后仍可被后续 run 消费
    _patch_run(monkeypatch, {"run_id": "run-dead", "status": "failed"})
    session_body = asyncio.run(grant_tool_permission(PermissionGrantRequest(
        tool="shell_exec", scope="session", command="rm -f /tmp/x",
        run_id="run-dead", conversation_id="conv-1")))
    assert session_body["granted"] is True
    always_body = asyncio.run(grant_tool_permission(PermissionGrantRequest(
        tool="shell_exec", scope="always", command="rm -f /tmp/x", run_id="run-dead")))
    assert always_body["granted"] is True


def test_状态查询异常时放行_不阻断用户授权(monkeypatch, gate_env):
    # 完整性守卫不是安全闸门：拿不到证据不等于 run 已死，fail-open + 告警
    def _boom(run_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(db, "get_agent_run", _boom)
    body = asyncio.run(grant_tool_permission(PermissionGrantRequest(
        tool="shell_exec", scope="once", command="rm -f /tmp/x", run_id="run-x")))
    assert body["granted"] is True
