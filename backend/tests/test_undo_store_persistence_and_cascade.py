"""undo_store 持久化与级联回归（功能 B · run 级一键回滚）。

守护点：
- record/get 幂等与 seq 有序（done 重复投递不产生重复行）；
- agent_runs 行不存在（headless run）时整批跳过，绝不产生 FK 孤儿；
- FK ON DELETE CASCADE 随 prune_agent_runs 级联清理（PRAGMA foreign_keys=ON，
  database.py:80 已核实——undo 行不能比 run 活得久）；
- **存在性检查后、插入前 agent_runs 被 prune 删除的竞态 → run 级 catch
  IntegrityError、整批放弃且不抛**（评审意见 3）；
- snapshot 超限拒写该行（不是整批失败），快照没了的 file 项降为 manual；
- 外部形态诡异的 run id 走参数绑定（行为上：原样存取、不炸库、不注入）。

SQL 书写规约（测试同守）：本文件所有语句内联字面量 + ``?`` 参数绑定，
不拼接、不插值、不把 SQL 文本经变量转发给 execute。
"""
import base64
import os
import sqlite3

import pytest

import database as db
import undo_store
from undo_store import MAX_SNAPSHOT_B64_CHARS



def _ensure_conv() -> str:
    """建一条会话并返回其真实 id（agent_runs.conversation_id 挂 FK，父行必须先存在）"""
    return str(db.create_conversation(title="t", mode="ssh")["id"])


@pytest.fixture
def undo_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "undo-store.db"))
    yield
    db._close_all_connections()


def _mk_run(run_id: str) -> None:
    # conversations 先行：agent_runs.conversation_id 同样挂 FK（ON DELETE CASCADE）
    db.create_agent_run(run_id, _ensure_conv(), task="t")


def _count_rows(run_id: str) -> int:
    conn = db._get_conn()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM run_undo_actions WHERE agent_run_id = ?", (run_id,)).fetchone()["n"]
    finally:
        conn.close()


def _rows(*specs):
    """specs: (kind, seq) 的简写行构造，字段给最小合法值。"""
    out = []
    for kind, seq in specs:
        if kind == "shell":
            out.append({"seq": seq, "kind": "shell", "tool": "shell_exec",
                        "command": "mkdir /a", "reverse_command": "rmdir /a",
                        "path": None, "snapshot_b64": None, "existed": None,
                        "reason": None, "confidence": "derived",
                        "ssh_session_id": "s1", "host": "h1",
                        "environment": "test"})
        elif kind == "file":
            out.append({"seq": seq, "kind": "file", "tool": "file_write",
                        "command": "", "reverse_command": None,
                        "path": "/f", "snapshot_b64": base64.b64encode(b"x").decode(),
                        "existed": True, "reason": None, "confidence": "snapshot",
                        "ssh_session_id": "s1", "host": "h1", "environment": "test"})
        else:
            out.append({"seq": seq, "kind": "noop", "tool": "file_read",
                        "command": "", "reverse_command": None, "path": None,
                        "snapshot_b64": None, "existed": None, "reason": "readonly",
                        "confidence": "manual", "ssh_session_id": "s1",
                        "host": "h1", "environment": "test"})
    return out


# ── record / get：幂等、seq 有序、状态语义 ──

def test_record_then_get_ordered_and_idempotent(undo_db):
    _mk_run("run-a")
    assert undo_store._record_rows_sync("run-a", _rows(("noop", 0), ("shell", 1), ("file", 2)), "completed") is True
    rows = undo_store.get_undo_actions("run-a")
    assert [r["seq"] for r in rows] == [0, 1, 2]
    assert [r["kind"] for r in rows] == ["noop", "shell", "file"]
    assert rows[1]["status"] == "pending"
    # completed run 的条目不携带快照（大字段不出库）
    assert all("snapshot_b64" not in r for r in rows)

    # 幂等：同 run 再 record 不重复插行
    assert undo_store._record_rows_sync("run-a", _rows(("shell", 9)), "completed") is False
    assert len(undo_store.get_undo_actions("run-a")) == 3


def test_failed_run_rows_marked_auto_rolled_back(undo_db):
    _mk_run("run-f")
    assert undo_store._record_rows_sync("run-f", _rows(("shell", 0)), "failed") is True
    rows = undo_store.get_undo_actions("run-f")
    assert rows[0]["status"] == "auto_rolled_back"
    assert rows[0]["run_status"] == "failed"


def test_headless_run_without_agent_runs_row_is_skipped(undo_db):
    """headless run 不经 create_agent_run：没有 agent_runs 行 → 整批跳过，无孤儿"""
    assert undo_store._record_rows_sync("run-ghost", _rows(("shell", 0), ("file", 1)), "completed") is False
    assert undo_store.get_undo_actions("run-ghost") == []
    assert _count_rows("run-ghost") == 0


def test_fk_cascade_with_prune_agent_runs(undo_db):
    """prune_agent_runs 删 run → undo 行级联消失（FK ON，CASCADE 真实生效）"""
    for rid in ("run-old", "run-new"):
        _mk_run(rid)
        assert undo_store._record_rows_sync(rid, _rows(("shell", 0)), "completed") is True
    # prune 的窗口按 started_at 排（秒级分辨率，同秒并列无 id 兜底）——
    # 把 run-old 钉成确定最旧，级联目标才确定
    conn = db._get_conn()
    try:
        conn.execute("UPDATE agent_runs SET started_at = '2000-01-01 00:00:00' WHERE id = ?", ("run-old",))
        conn.commit()
    finally:
        conn.close()
    # 只留 1 个 run：最旧的 run-old 连同其 undo 行被级联清理
    removed = db.prune_agent_runs(max_count=1)
    assert removed == 1
    assert undo_store.get_undo_actions("run-old") == []
    assert len(undo_store.get_undo_actions("run-new")) == 1


def test_prune_race_between_exists_check_and_insert(undo_db):
    """竞态：存在性检查通过后、INSERT 前 agent_runs 行被 prune 删除 →
    run 级 catch IntegrityError、整批放弃且**不向调用方抛**（返回 False）。

    复现方式：在同线程的池化连接上挂 set_trace_callback（每条语句完成后
    回调，SQLite 官方 API，不转发 SQL 文本）—— 回调看到「SELECT 1 FROM
    agent_runs」完成的那一刻，用另一条 FK ON 裸连接参数化删除该 run 行，
    与 prune_agent_runs 的删除同语义。随后 INSERT 撞 FK → IntegrityError。"""
    _mk_run("run-race")
    # _get_conn 是线程本地池化连接：测试线程里拿到的就是被测函数将用的那条
    conn = db._get_conn()

    def _delete_parent_after_exists_check(sql: str) -> None:
        if not sql.lstrip().startswith("SELECT 1 FROM agent_runs"):
            return
        db_path = None
        for r in conn.execute("PRAGMA database_list").fetchall():
            if r[1] == "main":
                db_path = str(r[2])
        assert db_path, "main db path not found"
        raw = sqlite3.connect(db_path, timeout=5.0)
        try:
            raw.execute("PRAGMA foreign_keys=ON")
            raw.execute("DELETE FROM agent_runs WHERE id = ?", ("run-race",))
            raw.commit()
        finally:
            raw.close()

    conn.set_trace_callback(_delete_parent_after_exists_check)
    try:
        # 不抛、返回 False（整批放弃）
        assert undo_store._record_rows_sync("run-race", _rows(("shell", 0), ("file", 1)), "completed") is False
    finally:
        conn.set_trace_callback(None)
    assert undo_store.get_undo_actions("run-race") == []
    # run 行确实没了（竞态前提成立），库仍然健康可用
    assert db.get_agent_run("run-race") is None
    _mk_run("run-after")
    assert undo_store._record_rows_sync("run-after", _rows(("shell", 0)), "completed") is True


def test_snapshot_over_limit_row_rejected_others_kept(undo_db):
    """单行快照超 512KB → 拒写该行并降为 manual；其余条目照常入库"""
    _mk_run("run-big")
    big_b64 = base64.b64encode(b"x" * (MAX_SNAPSHOT_B64_CHARS + 1)).decode()
    rows = _rows(("file", 0), ("shell", 1))
    rows[0]["snapshot_b64"] = big_b64
    assert undo_store._record_rows_sync("run-big", rows, "completed") is True
    got = undo_store.get_undo_actions("run-big")
    assert len(got) == 2
    assert got[0]["confidence"] == "manual"  # 快照没了 → 内容级还原降级
    # 完整行（工具读取路径）里该行也没有快照
    action = undo_store.get_undo_action("run-big", 0)
    assert action is not None and action["snapshot_b64"] is None


def test_oversized_run_id_is_bound_not_interpolated(undo_db):
    """含引号/分号的 run id 原样存取（参数绑定），不炸库、不改变语句语义"""
    weird = "run'; DROP TABLE run_undo_actions; --"
    _mk_run(weird)
    assert undo_store._record_rows_sync(weird, _rows(("shell", 0)), "completed") is True
    rows = undo_store.get_undo_actions(weird)
    assert len(rows) == 1 and rows[0]["kind"] == "shell"
    # 表还在、行数符合预期：注入没发生
    assert _count_rows(weird) == 1


# ── run_rollbacks 关联：幂等、双向查 ──

def test_register_rollback_idempotent_and_lookup(undo_db):
    _mk_run("src-1")
    _mk_run("rb-1")
    first = undo_store.register_rollback("src-1", "rb-1")
    second = undo_store.register_rollback("src-1", "rb-1")
    assert first["id"] == second["id"]
    assert undo_store.get_rollback_for_run("rb-1")["source_run_id"] == "src-1"
    assert undo_store.get_rollback_for_run("rb-2") is None
    assert [r["rollback_run_id"] for r in undo_store.list_rollbacks_for_source("src-1")] == ["rb-1"]


def test_mark_undone_idempotent(undo_db):
    _mk_run("src-2")
    undo_store._record_rows_sync("src-2", _rows(("file", 0)), "completed")
    assert undo_store.mark_undone("src-2", 0) is True
    assert undo_store.get_undo_actions("src-2")[0]["status"] == "rolled_back"
    assert undo_store.mark_undone("src-2", 0) is False  # 再标无变化（幂等语义）
