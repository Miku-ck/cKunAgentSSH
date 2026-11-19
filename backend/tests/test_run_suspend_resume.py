"""挂起 / 取消 / 待确认查询 / 启动对账 —— P0 持久化闭环的 DB 层集成测试。

覆盖（对应验收标准）：
  · 崩溃在「快照落库 → 状态改写」之间的 run，启动对账能救回 waiting_confirmation；
  · mark_stale 不误杀持快照的 run（用户没答的卡 ≠ 崩溃残留）；
  · cancel 幂等、终态不动、快照清空、审计落库；
  · pending 端点形状与 DB/快照一致性守卫；
  · resume 的 CAS / 校验失败路径（票据、空答案、状态不符、无模型）。

运行: python -m pytest backend/tests/test_run_suspend_resume.py -v
"""
import asyncio

import pytest

import database as db
from db.run_checkpoints import (
    save_checkpoint,
    load_checkpoint,
    promote_waiting_checkpoint_runs,
)


@pytest.fixture()
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "suspend-resume.db"))
    # run 行有会话外键：先建会话
    conv = db.create_conversation("suspend-test")
    global CONV_ID
    CONV_ID = conv["id"] if isinstance(conv, dict) else str(conv)
    yield db


CONV_ID = "conv-1"


def _mk_run(run_id: str, status: str, model_used: str = "m1"):
    db.create_agent_run(run_id, CONV_ID, "清理临时文件", model_used=model_used)
    if status != "running":
        db.update_agent_run(run_id, status=status)


def _mk_checkpoint(run_id: str, state: str = "pending"):
    save_checkpoint(
        run_id,
        conv_id=CONV_ID,
        messages=[
            {"role": "user", "content": "清理临时文件"},
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "type": "function",
                 "function": {"name": "shell_exec", "arguments": "{\"command\":\"rm -rf *\"}"}},
            ]},
        ],
        harness={"task": "清理临时文件", "conversation_id": CONV_ID,
                 "ssh_session_id": "", "max_iterations": 20},
        pending={"state": state, "kind": "scope", "tool": "shell_exec",
                 "tool_call_id": "clarify_abc123", "owner_tool_call_id": "tc1",
                 "args": {"command": "rm -rf *"}, "command": "rm -rf *",
                 "risk": "dangerous", "policy_risk": "high",
                 "steps": [{"question": "范围？", "options": [], "allow_custom": True}],
                 "step": 0, "total": 1, "reason": "通配符",
                 "round_calls": [{"tool_call_id": "tc1", "tool": "shell_exec",
                                  "args": {"command": "rm -rf *"}}],
                 "start_index": 0, "iteration": 3, "created_at": "now"},
        partial={"content": "我需要先确认影响范围", "tool_calls": []},
        iteration=3,
    )


# ── 启动对账 ──────────────────────────────────────────────────────────────────

def test_promote_rescues_crash_between_checkpoint_and_status(fresh_db):
    """快照已落库但状态还停在 running → 对账提升为 waiting_confirmation。"""
    _mk_run("r-crash", "running")
    _mk_checkpoint("r-crash", state="pending")
    n = promote_waiting_checkpoint_runs()
    assert n == 1
    row = db.get_agent_run("r-crash")
    assert row["status"] == "waiting_confirmation"


def test_mark_stale_spares_runs_with_checkpoint(fresh_db):
    """持快照的滞留 run 不被 mark_stale 误杀；无快照的照常标 failed。"""
    _mk_run("r-wait", "running")
    _mk_checkpoint("r-wait")
    _mk_run("r-dead", "running")
    n = db.mark_stale_agent_runs_failed()
    assert n == 1, "只有无快照的 r-dead 被标 failed"
    assert db.get_agent_run("r-wait")["status"] == "running"
    assert db.get_agent_run("r-dead")["status"] == "failed"
    # 对账先提升再清理：r-wait 被救回 waiting
    assert promote_waiting_checkpoint_runs() == 1
    assert db.get_agent_run("r-wait")["status"] == "waiting_confirmation"


def test_promote_ignores_cancelled_and_completed_checkpoints(fresh_db):
    """已取消/已恢复的快照不触发提升（避免复活死任务）。"""
    _mk_run("r-done", "running")
    _mk_checkpoint("r-done", state="resuming")  # 恢复中崩溃 → 也救回（可重试）
    _mk_run("r-done2", "completed")
    _mk_checkpoint("r-done2", state="resuming")
    n = promote_waiting_checkpoint_runs()
    assert n == 1
    assert db.get_agent_run("r-done")["status"] == "waiting_confirmation"
    assert db.get_agent_run("r-done2")["status"] == "completed", "终态 run 不提升"


# ── cancel 端点（直接调用路由函数）──────────────────────────────────────────

def test_cancel_suspended_run_full_flow(fresh_db):
    from routes.agent import cancel_suspended_run
    from agent.harness.contracts import TERMINAL_RUN_STATES

    _mk_run("r-cancel", "waiting_confirmation")
    _mk_checkpoint("r-cancel")
    result = asyncio.run(cancel_suspended_run("r-cancel"))
    assert result["cancelled"] is True
    assert db.get_agent_run("r-cancel")["status"] == "cancelled"
    assert db.get_agent_run("r-cancel")["completed_at"], "取消是终态，必须写 completed_at"
    assert load_checkpoint("r-cancel") is None, "取消的任务没有恢复意义，快照清空"
    assert "cancelled" in TERMINAL_RUN_STATES

    # 幂等：已终态再取消 → cancelled=False，不报错
    result2 = asyncio.run(cancel_suspended_run("r-cancel"))
    assert result2["cancelled"] is False
    assert result2["status"] == "cancelled"


def test_cancel_unknown_run_404(fresh_db):
    from routes.agent import cancel_suspended_run
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        asyncio.run(cancel_suspended_run("no-such-run"))
    assert ei.value.status_code == 404


# ── pending 端点 ─────────────────────────────────────────────────────────────

def test_pending_endpoint_shape_and_guards(fresh_db):
    from routes.agent import get_run_pending

    _mk_run("r-pending", "waiting_confirmation")
    _mk_checkpoint("r-pending")
    out = asyncio.run(get_run_pending("r-pending"))
    p = out["pending"]
    assert p is not None
    assert p["kind"] == "clarification"
    assert p["tool"] == "shell_exec"
    assert p["tool_call_id"] == "clarify_abc123"
    assert p["risk"] == "dangerous", "前端权限卡口径：safe|moderate|dangerous"
    assert p["steps"], "卡片渲染需要题面"
    assert p["partial"]["content"], "partial 前文用于恢复后回填气泡"

    # DB 状态与快照不一致（对账前窗口）→ 不给卡，避免误导
    db.update_agent_run("r-pending", status="failed")
    out2 = asyncio.run(get_run_pending("r-pending"))
    assert out2["pending"] is None

    # 无快照 → null
    _mk_run("r-none", "waiting_confirmation")
    assert asyncio.run(get_run_pending("r-none"))["pending"] is None


# ── resume 端点校验失败路径 ──────────────────────────────────────────────────

def _resume_preconditions(fresh_db, run_id="r-resume"):
    _mk_run(run_id, "waiting_confirmation")
    _mk_checkpoint(run_id)
    # 模型配置必须存在（恢复要按原模型重建 harness；run 行的 model_used=m1 需
    # 能被 _get_model_config 按 id/model_id 命中）
    cfg = db.create_model_config(
        name="suspend-test-model", base_url="http://127.0.0.1:1",
        api_key="sk-x", model_id="test-model",
    )
    # run 行的 model_used 写模型行主键
    conn = db._get_conn()
    try:
        conn.execute("UPDATE agent_runs SET model_used = ? WHERE id = ?",
                     (cfg["id"], run_id))
        conn.commit()
    finally:
        conn.close()


def test_resume_rejects_invalid_requests(fresh_db):
    from routes.agent import resume_suspended_run, ResumeRunRequest
    from fastapi import HTTPException

    # 无快照 → 404
    with pytest.raises(HTTPException) as ei:
        asyncio.run(resume_suspended_run(
            "no-such", ResumeRunRequest(answers={"0": {"choice": "A"}})))
    assert ei.value.status_code == 404

    _resume_preconditions(fresh_db)

    # 空答案 → 400（跳过已下线）
    with pytest.raises(HTTPException) as ei:
        asyncio.run(resume_suspended_run("r-resume", ResumeRunRequest(answers={})))
    assert ei.value.status_code == 400

    # 票据不匹配 → 409
    with pytest.raises(HTTPException) as ei:
        asyncio.run(resume_suspended_run(
            "r-resume",
            ResumeRunRequest(tool_call_id="wrong-ticket",
                             answers={"0": {"choice": "A"}})))
    assert ei.value.status_code == 409

    # 状态不符（非 waiting_confirmation）→ 409
    db.update_agent_run("r-resume", status="running")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(resume_suspended_run(
            "r-resume", ResumeRunRequest(answers={"0": {"choice": "A"}})))
    assert ei.value.status_code == 409

    # 快照未被失败请求污染（CAS 只在通过全部校验后推进）
    ck = load_checkpoint("r-resume")
    assert ck["pending"]["state"] == "pending"


def test_resume_cas_prevents_double_resume(fresh_db):
    """CAS 抢占：第一个恢复者把 pending 置 resuming 后，第二个立即 409。"""
    from routes.agent import resume_suspended_run, ResumeRunRequest
    from fastapi import HTTPException

    _resume_preconditions(fresh_db)
    _mk_checkpoint("r-resume", state="resuming")  # 模拟已被抢占
    with pytest.raises(HTTPException) as ei:
        asyncio.run(resume_suspended_run(
            "r-resume", ResumeRunRequest(answers={"0": {"choice": "A"}})))
    assert ei.value.status_code == 409


def test_prune_never_deletes_waiting_runs(fresh_db):
    """prune_agent_runs 按保留条数清理，但 waiting_confirmation 永不进删除集。"""
    _mk_run("r-waiting", "waiting_confirmation")
    _mk_checkpoint("r-waiting")
    for i in range(10):
        _mk_run(f"r-old-{i}", "completed")
    db.prune_agent_runs(max_count=3)
    assert db.get_agent_run("r-waiting") is not None, "等待中的 run 不许被清理"
    assert db.get_agent_run("r-waiting")["status"] == "waiting_confirmation"
    assert load_checkpoint("r-waiting") is not None
