# -*- coding: utf-8 -*-
"""GET /workflow 读侧语义 + kind 清单漂移守卫（routes/workflow.py 此前只有 DELETE 侧有测试）。

钉住三件事：
- 无会话 / 读取抛异常都降级成空对象，并且**留一条 warning**（静默降级至少要可追查）；
- 空对象每次是新实例（`_empty_workflow` 的注释承诺「不返回共享模块常量」，此前无人对账）；
- 路由的 `_DELETE_KINDS` 必须与 `agent.workflow._KIND_FILES` 同集合 —— 文件头注释写它俩「同域」，
  那是个没有被任何代码检查的声明：将来 workflow 侧加第四种工件而路由没跟上时，
  新工件就删不掉（chip 变成关不上的开关）。
"""
import asyncio
import logging

import pytest

import agent.workflow as wf
import routes.workflow as rw


@pytest.fixture
def empty_snapshot():
    return {"phase": "", "spec": "", "plan": "", "goal": ""}


def test_get_without_conversation_id_returns_empty_and_reads_nothing(monkeypatch, empty_snapshot):
    def _boom(*_a, **_k):
        raise AssertionError("无会话不该去碰文件系统")

    monkeypatch.setattr(wf, "get_phase", _boom)
    monkeypatch.setattr(wf, "read_artifacts", _boom)
    assert asyncio.run(rw.get_workflow(conversation_id="")) == empty_snapshot


def test_get_degrades_to_empty_and_logs_when_read_fails(monkeypatch, caplog):
    def _fail(*_a, **_k):
        raise OSError("disk gone (simulated)")

    monkeypatch.setattr(wf, "get_phase", _fail)
    monkeypatch.setattr(wf, "read_artifacts", _fail)
    with caplog.at_level(logging.WARNING, logger="routes.workflow"):
        out = asyncio.run(rw.get_workflow(conversation_id="conv-1"))
    assert out == {"phase": "", "spec": "", "plan": "", "goal": ""}
    assert any("读取工作流失败" in r.getMessage() for r in caplog.records), \
        "降级可以，但必须留下线索 —— 否则「读不到」与「真的没有工件」在日志里也不可区分"


def test_empty_workflow_is_not_a_shared_dict():
    a = rw._empty_workflow()
    a["phase"] = "tampered"
    assert rw._empty_workflow()["phase"] == ""


def test_get_passes_through_phase_and_none_artifacts_become_empty_strings(monkeypatch):
    monkeypatch.setattr(wf, "get_phase", lambda cid: "goal")
    monkeypatch.setattr(
        wf, "read_artifacts",
        lambda cid: {"spec": None, "plan": "计划正文", "goal": "目标正文"},
    )
    out = asyncio.run(rw.get_workflow(conversation_id="conv-9"))
    assert out == {"phase": "goal", "spec": "", "plan": "计划正文", "goal": "目标正文"}


def test_delete_kind_whitelist_matches_the_artifact_table():
    """注释说两处「同域」—— 这条把它变成可失败的检查，而不是口头承诺。"""
    assert set(rw._DELETE_KINDS) == set(wf._KIND_FILES)
