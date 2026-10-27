"""第 8 轮审查修复回归测试。

覆盖：
  MAJOR — SSE permission_needed 透传 normalized_command（问题 2 端到端接线）
  MINOR — release-run 审计结论 "released" 不再被静默改写为 executed
  MINOR — agent_runs/tool_calls 保留清理 prune_agent_runs（级联删 tool_calls）

运行: python -m pytest backend/tests/test_sse_permission_and_run_lifecycle.py -v
"""
import asyncio
import sqlite3


import database as db
from agent import audit
from agent.harness.telemetry import SSEAdapter, TraceEvent


# ── MAJOR：SSE 透传 normalized_command ──────────────────────────────────────
def test_sse_permission_needed_carries_normalized_command():
    """reins 写入 payload 的 normalized_command 必须透到前端 SSE dict。

    修复前 to_sse_dict 的 permission_needed 分支只映射 tool/args/risk/reversible/
    message，normalized_command 被丢弃 → 前端授权卡片「授权命令」行永远收不到。
    """
    ev = TraceEvent(
        kind="permission_needed",
        source="reins.filesystem_guard",
        run_id="runX",
        iteration=2,
        payload={
            "tool": "shell_exec",
            "args": {"command": "rm  -rf  /tmp/foo "},
            "risk": "dangerous",
            "reversible": False,
            "message": "需要用户授权",
            "normalized_command": "rm -rf /tmp/foo",
        },
    )
    d = SSEAdapter.to_sse_dict(ev)
    assert d["normalized_command"] == "rm -rf /tmp/foo"
    # run_id 也在 to_sse_dict 层自洽（路由层会再统一注入，二者不冲突）
    assert d["run_id"] == "runX"


def test_sse_permission_needed_without_normalized_command_defaults_empty():
    """非命令工具的 permission_needed 无该字段 → 透传空串（前端 isCommandTool 下隐藏）。"""
    ev = TraceEvent(
        kind="permission_needed",
        source="reins.permission_gate",
        run_id="runY",
        iteration=1,
        payload={
            "tool": "service_manage",
            "args": {"action": "stop", "name": "nginx"},
            "risk": "dangerous",
            "reversible": False,
            "message": "需要用户授权",
        },
    )
    d = SSEAdapter.to_sse_dict(ev)
    assert d["normalized_command"] == ""
    assert d["tool"] == "service_manage"


# ── MINOR：release-run 审计结论不再被改写 ───────────────────────────────────
def _audit_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


def test_release_run_conclusion_not_rewritten_to_executed(monkeypatch):
    """conclusion='released' 是合法结论，不应被 append_record 静默强制为 executed。

    修复前 "released" 不在白名单 → 落 "executed"，审计链里「释放授权」显示成「执行」。
    """
    conn = _audit_conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    rec = audit.append_record(
        action="permission_release_run:run1",
        conclusion="released",
        target_type="run",
        target_id="run1",
        result="released=2",
    )
    assert rec is not None
    row = conn.execute(
        "SELECT conclusion, action, result FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["conclusion"] == "released"
    assert row["action"] == "permission_release_run:run1"
    assert row["result"] == "released=2"


def test_audit_invalid_conclusion_still_falls_back_executed(monkeypatch):
    """回归保护：非法结论仍被安全地强制为 executed（白名单收紧方向不变）。"""
    conn = _audit_conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    rec = audit.append_record(action="x", conclusion="nonsense")
    assert rec is not None
    row = conn.execute(
        "SELECT conclusion FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["conclusion"] == "executed"


# ── MINOR：agent_runs/tool_calls 保留清理 ───────────────────────────────────
def _fresh_db(path):
    db._thread_local.__dict__.clear()
    db.init_db(str(path))


def test_prune_agent_runs_cascades_tool_calls(tmp_path):
    """prune_agent_runs 删最旧 run，tool_calls 经外键级联删除。

    修复前 agent_runs/tool_calls 只靠 conversation 级联回收，用户不删会话则无界增长。
    """
    _fresh_db(tmp_path / "t.db")
    conn = db._get_conn()
    try:
        conv = db.create_conversation(title="c1")
        cid = conv["id"]
        # 6 个 run，started_at 递增（run0 最旧）
        for i in range(6):
            db.create_agent_run(f"run{i}", cid, f"task{i}")
            # 每个 run 挂 2 条 tool_call
            db.add_tool_call(f"run{i}", 0, "shell_exec", "cmd")
            db.add_tool_call(f"run{i}", 1, "file_read", "cmd")
        # 控制 started_at 递增，确保 run0 最旧
        for i in range(6):
            conn.execute(
                "UPDATE agent_runs SET started_at=? WHERE id=?",
                (f"2026-01-0{i+1} 00:00:00", f"run{i}"),
            )
        conn.commit()

        deleted = db.prune_agent_runs(max_count=3)
        # 保留最近 3 个（run3/run4/run5），删 run0/run1/run2
        assert deleted == 3
        kept = {r[0] for r in conn.execute("SELECT id FROM agent_runs").fetchall()}
        assert kept == {"run3", "run4", "run5"}, kept
        # tool_calls 级联：删掉的 run 的 tool_call 行也消失（只剩 run3..5 的 6 条）
        tc_rows = conn.execute(
            "SELECT agent_run_id FROM tool_calls ORDER BY agent_run_id"
        ).fetchall()
        tc_runs = {r[0] for r in tc_rows}
        assert tc_runs == {"run3", "run4", "run5"}, tc_runs
        assert len(tc_rows) == 6
    finally:
        conn.close()


def test_prune_agent_runs_no_op_under_cap(tmp_path):
    """未超上限时不删（保留全部）。"""
    _fresh_db(tmp_path / "t2.db")
    conv = db.create_conversation(title="c1")
    for i in range(3):
        db.create_agent_run(f"run{i}", conv["id"], f"task{i}")
    assert db.prune_agent_runs(max_count=5) == 0


# ── #1：prod 强化覆盖 non-shell 危险工具（service_manage stop/restart）────────
import agent.harness.reins.gate as _gate_mod
from agent.harness.contracts import AgentConfig, RunContext
from agent.harness.reins import PermissionGate
from agent.harness.reins.core import AWAIT_APPROVAL_KEY
from agent.tools.builtin.ops_service import ServiceManageTool


def _prod_gate(monkeypatch) -> PermissionGate:
    """构造带 service_manage meta 的 PermissionGate（隔离 allowlist/审计 IO）。"""
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)

    async def _noop_audit(**kw):
        return None
    monkeypatch.setattr(_gate_mod, "append_record_async", _noop_audit)

    g = PermissionGate()
    g._confirmed_permanent = set()
    # 让 _lookup_meta 直接返回 ServiceManageTool 的真实 meta（避免搭 saddle/registry）
    g._lookup_meta = lambda tool_name, ctx=None: (
        ServiceManageTool.meta if tool_name == "service_manage" else None
    )
    return g


def _prod_ctx(tool: str, args: dict, environment: str) -> RunContext:
    ctx = RunContext.new(task="t", config=AgentConfig(),
                         environment=environment)
    ctx.tool_calls_log.append({"tool": tool, "args": args,
                               "iteration": 1, "success": None})
    return ctx


def _interactive(ctx: RunContext) -> RunContext:
    ctx.metadata[AWAIT_APPROVAL_KEY] = True
    return ctx


def test_prod_dangerous_tool_cards_when_interactive(monkeypatch):
    """交互 prod：工具级 dangerous 未预登记 → 弹卡（仅本次/始终允许），不再直接拒绝。"""
    g = _prod_gate(monkeypatch)
    ctx = _interactive(_prod_ctx(
        "service_manage", {"action": "stop", "name": "nginx"}, "prod"))
    d = asyncio.run(g.check(ctx))
    assert d.intercepted is True
    assert "生产环境" not in d.intercept_reason
    assert any(e.kind == "permission_needed" for e in d.events)
    assert d.approval_key


def test_prod_dangerous_tool_once_grant_recheck_allows(monkeypatch):
    """「允许本次」后重判：once 池命中，工具放行。"""
    g = _prod_gate(monkeypatch)
    ctx = _interactive(_prod_ctx(
        "service_manage", {"action": "stop", "name": "nginx"}, "prod"))
    asyncio.run(g.check(ctx))
    g.grant("service_manage", scope="once", command=None, run_id=ctx.run_id)
    assert asyncio.run(g.check(ctx)).intercepted is False


def test_prod_interactive_skips_auto_judge(monkeypatch):
    """prod 不走模型裁决（auto 档也一样）。"""
    calls = []

    async def _spy(*_a, **_kw):
        calls.append(1)
        return None

    monkeypatch.setattr(_gate_mod, "try_auto_approve", _spy)
    g = _prod_gate(monkeypatch)
    ctx = _interactive(_prod_ctx(
        "service_manage", {"action": "stop", "name": "nginx"}, "prod"))
    ctx.metadata["permission_mode"] = "auto"
    asyncio.run(g.check(ctx))
    assert calls == []


def test_prod_unattended_tool_deny_says_no_one_can_confirm(monkeypatch):
    """无人值守：仍拒绝，文案说清「没人能点卡」。"""
    g = _prod_gate(monkeypatch)
    ctx = _prod_ctx("service_manage", {"action": "stop", "name": "nginx"}, "prod")
    d = asyncio.run(g.check(ctx))
    assert d.intercepted is True
    inject = "\n".join(m["content"] for m in d.inject_messages)
    assert "无人值守" in inject and "allowlist" in inject


def test_prod_dangerous_tool_requires_preallocation(monkeypatch):
    """无人值守 prod：service_manage stop（升级为 dangerous）未预授权 → 拒绝。

    交互会话（见 test_prod_dangerous_tool_cards_when_interactive）由用户现场确认；
    这里钉住无人值守仍只认预登记 allowlist。
    """
    g = _prod_gate(monkeypatch)
    # 临时 once 授权（非预授权 allowlist）
    g.grant("service_manage", scope="once", command=None, run_id="run1")
    ctx = _prod_ctx("service_manage", {"action": "stop", "name": "nginx"}, "prod")
    decision = asyncio.run(g.check(ctx))
    assert decision.intercepted is True
    assert "生产环境" in decision.intercept_reason
    # 注入消息应提示需预登记 allowlist
    assert any("allowlist" in m["content"] for m in decision.inject_messages)


def test_prod_dangerous_tool_preallocation_allows(monkeypatch):
    """prod 下预授权（permanent allowlist）命中 → 放行。"""
    g = _prod_gate(monkeypatch)
    # 预登记 allowlist（工具级 key）
    g._confirmed_permanent.add("service_manage")
    ctx = _prod_ctx("service_manage", {"action": "stop", "name": "nginx"}, "prod")
    decision = asyncio.run(g.check(ctx))
    assert decision.intercepted is False


def test_prod_non_destructive_tool_not_hardened(monkeypatch):
    """prod 下 service_manage status（moderate，未升级）不走预授权强化 → 正常审批。

    验证强化只作用于升级为 dangerous 的 destructive action，不误伤 status/start。
    """
    g = _prod_gate(monkeypatch)
    # 不预授权、不临时授权 → moderate 不可逆走 session 级 check（未授权 → 拦截）
    # 但拦截原因应是"需要授权"而非"生产环境未预授权"
    ctx = _prod_ctx("service_manage", {"action": "status", "name": "nginx"}, "prod")
    decision = asyncio.run(g.check(ctx))
    assert decision.intercepted is True
    assert "生产环境" not in decision.intercept_reason


def test_test_env_dangerous_tool_temp_grant_allowed(monkeypatch):
    """非 prod 环境：service_manage stop 的临时 once 授权仍可放行（强化仅限 prod）。"""
    g = _prod_gate(monkeypatch)
    g.grant("service_manage", scope="once", command=None, run_id="run1")
    ctx = _prod_ctx("service_manage", {"action": "stop", "name": "nginx"}, "test")
    # 让 check 读到 run_id（ctx.run_id 与授权 run 一致）
    ctx.run_id = "run1"
    decision = asyncio.run(g.check(ctx))
    assert decision.intercepted is False


# ── #2：会话保留清理 prune_conversations（用户数据，默认关闭时才调用）────────
def test_prune_conversations_keeps_recent_completed(tmp_path):
    """保留最近 keep_count 个 completed 会话，按真实最后消息时间排序；in_progress 永不删。"""
    import database as db
    _fresh_db(tmp_path / "t.db")
    conn = db._get_conn()
    try:
        cids = [db.create_conversation(title=f"c{i}")["id"] for i in range(6)]
        # 控制"真实最后活跃时间"：c0 最旧（2026-01-01）… c5 最新（2026-01-06）
        for i, cid in enumerate(cids):
            conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (?, ?, 'user', 'x', ?)",
                (f"m{i}", cid, f"2026-01-0{i+1} 00:00:00"),
            )
        # c0 设为 in_progress（即使最旧也不得删）
        conn.execute("UPDATE conversations SET status='in_progress' WHERE id=?", (cids[0],))
        conn.commit()

        deleted = db.prune_conversations(keep_count=3)
        # completed 会话 5 个（c1..c5），保留最近 3 个（c3/c4/c5），删 c1/c2
        # c0 是 in_progress，即便最旧也不删；返回值是被删会话 id（调用方
        # 靠它回收 PermissionGate 的 session 授权池）
        assert set(deleted) == {cids[1], cids[2]}, deleted
        kept = {r[0] for r in conn.execute("SELECT id FROM conversations").fetchall()}
        # c0(in_progress 保留) + c3/c4/c5(最近 completed 保留)
        assert kept == {cids[0], cids[3], cids[4], cids[5]}, kept
        # 级联：被删会话 c1/c2 的 messages 消失
        m_cids = {r[0] for r in conn.execute("SELECT DISTINCT conversation_id FROM messages").fetchall()}
        assert m_cids == {cids[0], cids[3], cids[4], cids[5]}, m_cids
    finally:
        conn.close()


def test_prune_conversations_no_message_falls_back_updated_at(tmp_path):
    """无消息的会话按 updated_at/created_at 兜底排序，不误删较新的。"""
    import database as db
    _fresh_db(tmp_path / "t2.db")
    conn = db._get_conn()
    try:
        cids = [db.create_conversation(title=f"c{i}")["id"] for i in range(4)]
        # 全部无消息；用 updated_at 控制新旧
        for i, cid in enumerate(cids):
            conn.execute(
                "UPDATE conversations SET updated_at=?, created_at=? WHERE id=?",
                (f"2026-01-0{i+1} 00:00:00", f"2026-01-0{i+1} 00:00:00", cid),
            )
        conn.commit()
        deleted = db.prune_conversations(keep_count=2)
        assert set(deleted) == {cids[0], cids[1]}, deleted
        kept = {r[0] for r in conn.execute("SELECT id FROM conversations").fetchall()}
        assert kept == {cids[2], cids[3]}, kept
    finally:
        conn.close()


def test_prune_conversations_disabled_by_default_and_noop(tmp_path):
    """keep_count<=0（未开启/不限制）→ 不删任何会话。"""
    import database as db
    _fresh_db(tmp_path / "t3.db")
    conn = db._get_conn()
    try:
        for i in range(5):
            db.create_conversation(title=f"c{i}")
        conn.commit()
        assert db.prune_conversations(keep_count=0) == []
        assert db.prune_conversations(keep_count=None) == []
        remaining = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        assert remaining == 5
    finally:
        conn.close()


