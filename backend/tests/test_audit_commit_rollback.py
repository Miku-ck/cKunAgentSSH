"""回归：审计写入的 COMMIT 失败不得留下半截事务。

修复前 `COMMIT` 写在 try/except 的 `else` 分支，只有 INSERT 阶段的异常会 ROLLBACK。
并发写者持共享锁时 COMMIT 自己会抛 `database is locked` 且 `in_transaction` 保持
True，异常被外层 `except` 吞成 None 之后，该线程本地连接一直挂着未提交事务 ——
池化连接的 close 是 rollback-and-reuse，底层连接永不真正关闭，写锁因此长期滞留，
之后每一条经 to_thread 的写都要等 busy_timeout 才失败。
"""
import sqlite3

import database as db
from agent import audit


class _CommitFails:
    """只把 COMMIT 变成失败点，其余操作透传给真实连接。"""

    def __init__(self, inner: sqlite3.Connection) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "armed", True)

    def execute(self, sql, *args):
        if str(sql).strip().upper() == "COMMIT" and self.armed:
            raise sqlite3.OperationalError("database is locked")
        return self._inner.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


def test_failed_commit_leaves_no_open_transaction(monkeypatch):
    inner = _mem_conn()
    conn = _CommitFails(inner)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    assert audit.append_record(
        action="shell_exec", conclusion="executed", command_raw="ls -la"
    ) is None
    # 这一条断言就是缺陷本体：事务开在池化线程连接上 = 写锁楔死
    assert inner.in_transaction is False


def test_writer_recovers_after_a_failed_commit(monkeypatch):
    inner = _mem_conn()
    conn = _CommitFails(inner)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    assert audit.append_record(
        action="shell_exec", conclusion="executed", command_raw="ls -la"
    ) is None

    conn.armed = False
    rec = audit.append_record(
        action="shell_exec", conclusion="executed", command_raw="uptime"
    )
    assert rec is not None and rec["id"]
    rows = inner.execute("SELECT command_raw FROM audit_log ORDER BY id").fetchall()
    assert [r["command_raw"] for r in rows] == ["uptime"]
