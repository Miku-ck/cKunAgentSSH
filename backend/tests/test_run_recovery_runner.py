"""启动对账编排：先分类，再只调度证据完整的运行。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import database as db
from agent import run_recovery_runner as runner
from db import operation_ledger, run_safe_points


@pytest.fixture
def startup_db(tmp_path):
    db.init_db(str(tmp_path / "startup.db"))
    conv = db.create_conversation("startup")
    cid = conv["id"] if isinstance(conv, dict) else str(conv)
    run_safe_points._reset_memo_for_tests()
    operation_ledger._reset_memo_for_tests()
    yield cid
    run_safe_points._reset_memo_for_tests()
    operation_ledger._reset_memo_for_tests()


def _create(cid, run_id, *, safe=True, phase="after_tool", ssh_session_id=""):
    db.create_agent_run(run_id, cid, "task", model_used="model-a")
    if safe:
        run_safe_points.save_safe_point(
            run_id, expected_version=0, phase=phase,
            payload={
                "schema_version": 1, "run_id": run_id, "conversation_id": cid,
                "task": "task", "state": "thinking", "iteration": 1,
                "messages": [{"role": "user", "content": "task"}],
                "config": {"max_iterations": 10}, "ssh_session_id": ssh_session_id,
            },
        )


@pytest.mark.asyncio
async def test_startup_schedules_only_safe_runs(startup_db, monkeypatch):
    _create(startup_db, "safe")
    _create(startup_db, "no-safe", safe=False)
    _create(startup_db, "approval", phase="waiting_approval")
    scheduled = []

    async def fake_execute(run_id):
        scheduled.append(run_id)

    monkeypatch.setattr(runner, "execute_recovery", fake_execute)
    count = await runner.reconcile_stale_runs()
    assert count == 3
    assert scheduled == ["safe"]
    assert db.get_agent_run("safe")["status"] == "running"
    assert db.get_agent_run("no-safe")["status"] == "failed"
    assert db.get_agent_run("approval")["status"] == "human_help"


@pytest.mark.asyncio
async def test_startup_keeps_unknown_side_effect_for_reconciliation(startup_db, monkeypatch):
    _create(startup_db, "unknown")
    operation = operation_ledger.create_operation(
        operation_id="op-unknown", run_id="unknown", tool_call_id="call-1",
        tool_name="shell_exec", arguments={"command": "restart service"},
    )
    operation_ledger.transition_operation(
        operation["operation_id"], from_status="prepared", from_version=1,
        to_status="executing",
    )
    operation = operation_ledger.get_operation(operation["operation_id"])
    operation_ledger.transition_operation(
        operation["operation_id"], from_status="executing", from_version=2,
        to_status="unknown",
    )
    scheduled = []

    async def no_execute(run_id):
        scheduled.append(run_id)

    monkeypatch.setattr(runner, "execute_recovery", no_execute)
    assert await runner.reconcile_stale_runs() == 1
    assert scheduled == []
    assert db.get_agent_run("unknown")["status"] == "human_help"
    assert run_safe_points.load_safe_point("unknown") is not None


@pytest.mark.asyncio
async def test_startup_does_not_auto_resume_stale_ssh_session(startup_db, monkeypatch):
    _create(startup_db, "ssh", ssh_session_id="old-session")
    scheduled = []
    async def no_execute(run_id):
        scheduled.append(run_id)

    monkeypatch.setattr(runner, "execute_recovery", no_execute)
    await runner.reconcile_stale_runs()
    assert scheduled == []
    assert db.get_agent_run("ssh")["status"] == "human_help"


@pytest.mark.asyncio
async def test_execute_recovery_persists_result_and_restricts_original_tools(startup_db, monkeypatch):
    _create(startup_db, "recover")
    row = run_safe_points.load_safe_point("recover")
    row["payload"]["metadata"] = {
        "permission_mode": "plan", "available_tool_names": ["file_read"],
    }
    row["payload"]["iteration"] = 2
    run_safe_points.save_safe_point(
        "recover", expected_version=row["version"], phase="after_tool", payload=row["payload"],
    )

    class Registry:
        def __init__(self):
            self.available = ["file_read", "new_tool"]
        def names(self):
            return list(self.available)
        def unregister(self, name):
            self.available.remove(name)

    registry = Registry()
    captured = {}

    class FakeHarness:
        def __init__(self, config):
            captured["config"] = config
        def assemble(self, **kwargs):
            captured["registry"] = kwargs["tool_registry"]
        async def run(self, **kwargs):
            captured["run"] = kwargs
            yield SimpleNamespace(kind="done", payload={
                "state": "completed", "content": "recovered answer", "iterations": 1,
                "cost": {"total_tokens": 20, "prompt_tokens": 12, "completion_tokens": 8,
                         "cost_usd": 0.01, "tool_call_count": 1,
                         "context_summary": "summary"},
            })

    async def acquire_global():
        return True
    async def acquire_run(cid, rid):
        return False
    monkeypatch.setattr("agent.harness.Harness", FakeHarness)
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", lambda **kwargs: kwargs)
    monkeypatch.setattr("agent.tools.create_registry", lambda: registry)
    monkeypatch.setattr("routes.terminal._get_model_config", lambda model: {
        "base_url": "https://example.com/v1", "api_key": "test-key", "model_id": "model-a",
    })
    monkeypatch.setattr("routes.agent._acquire_global_run_slot", acquire_global)
    monkeypatch.setattr("routes.agent._acquire_run_slot", acquire_run)
    monkeypatch.setattr("routes.agent._release_global_run_slot_sync", lambda: None)
    monkeypatch.setattr("routes.agent._release_run_slot_sync", lambda *args: None)

    await runner.execute_recovery("recover")

    assert registry.names() == ["file_read"]
    assert captured["run"]["resume_safe_point"]["messages"] == [{"role": "user", "content": "task"}]
    assert captured["run"]["permission_mode"] == "plan"
    assert captured["run"]["await_approval"] is False
    finished = db.get_agent_run("recover")
    assert finished["status"] == "completed"
    assert finished["total_tokens"] == 20
    assert finished["iteration"] == 3
    assert run_safe_points.load_safe_point("recover") is None
    conversation = db.get_conversation(startup_db)
    assert conversation["status"] == "completed"


@pytest.mark.asyncio
async def test_execute_recovery_reopens_conversation_before_running(startup_db, monkeypatch):
    _create(startup_db, "reopen", phase="iteration")
    row = run_safe_points.load_safe_point("reopen")
    row["payload"]["iteration"] = 0
    row["payload"]["config"] = {"max_iterations": 1}
    run_safe_points.save_safe_point(
        "reopen", expected_version=row["version"], phase="iteration", payload=row["payload"],
    )
    seen = []
    captured = {}

    class FakeHarness:
        def __init__(self, config):
            pass
        def assemble(self, **kwargs):
            pass
        async def run(self, **kwargs):
            captured.update(kwargs)
            seen.append(db.get_conversation(startup_db)["status"])
            yield SimpleNamespace(kind="done", payload={"state": "completed", "content": "ok", "cost": {}})

    async def acquire_global():
        return True
    async def acquire_run(cid, rid):
        return False
    monkeypatch.setattr("agent.harness.Harness", FakeHarness)
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", lambda **kwargs: kwargs)
    monkeypatch.setattr("agent.tools.create_registry", lambda: SimpleNamespace(names=lambda: [], unregister=lambda name: None))
    monkeypatch.setattr("routes.terminal._get_model_config", lambda model: {
        "base_url": "https://example.com/v1", "api_key": "test-key", "model_id": "model-a",
    })
    monkeypatch.setattr("routes.agent._acquire_global_run_slot", acquire_global)
    monkeypatch.setattr("routes.agent._acquire_run_slot", acquire_run)
    monkeypatch.setattr("routes.agent._release_global_run_slot_sync", lambda: None)
    monkeypatch.setattr("routes.agent._release_run_slot_sync", lambda *args: None)
    db.update_conversation_status(startup_db, "completed")

    await runner.execute_recovery("reopen")
    assert seen == ["in_progress"]
    assert captured["resume_safe_point"]["iteration"] == -1
    assert db.get_conversation(startup_db)["status"] == "completed"


@pytest.mark.asyncio
async def test_execute_recovery_exception_resets_conversation_and_keeps_scene(startup_db, monkeypatch):
    _create(startup_db, "recover-error")

    class FakeHarness:
        def __init__(self, config):
            pass
        def assemble(self, **kwargs):
            pass
        async def run(self, **kwargs):
            assert db.get_conversation(startup_db)["status"] == "in_progress"
            raise RuntimeError("恢复执行中断")
            yield

    async def acquire_global():
        return True
    async def acquire_run(cid, rid):
        return False
    monkeypatch.setattr("agent.harness.Harness", FakeHarness)
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", lambda **kwargs: kwargs)
    monkeypatch.setattr("agent.tools.create_registry", lambda: SimpleNamespace(names=lambda: [], unregister=lambda name: None))
    monkeypatch.setattr("routes.terminal._get_model_config", lambda model: {
        "base_url": "https://example.com/v1", "api_key": "test-key", "model_id": "model-a",
    })
    monkeypatch.setattr("routes.agent._acquire_global_run_slot", acquire_global)
    monkeypatch.setattr("routes.agent._acquire_run_slot", acquire_run)
    monkeypatch.setattr("routes.agent._release_global_run_slot_sync", lambda: None)
    monkeypatch.setattr("routes.agent._release_run_slot_sync", lambda *args: None)

    await runner.execute_recovery("recover-error")

    assert db.get_agent_run("recover-error")["status"] == "human_help"
    assert db.get_conversation(startup_db)["status"] == "completed"
    assert run_safe_points.load_safe_point("recover-error") is not None


@pytest.mark.asyncio
async def test_startup_ignores_terminal_runs(startup_db, monkeypatch):
    _create(startup_db, "done")
    db.update_agent_run("done", status="completed")
    scheduled = []
    async def no_execute(run_id):
        scheduled.append(run_id)

    monkeypatch.setattr(runner, "execute_recovery", no_execute)
    assert await runner.reconcile_stale_runs() == 0
    assert scheduled == []
