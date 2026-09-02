# -*- coding: utf-8 -*-
"""回归测试：C-review C3 — completed run 的 /undo 主场景可恢复。

修复前：run 正常完成后备份被 B-8 清理 + DB 不落 before 内容 →
modify/delete 的撤销在主场景（用户看完结果点撤销）必然失败。
修复后：run 结束不清备份 + before/after 内容落库 + undo 备份缺失时 DB 兜底
+ 启动时 7 天 TTL 清理过期备份。
"""
import os
import sqlite3
import time

import database as db
from agent.file_changes import FileChange, _PENDING, _LOCK
from agent.harness.saddle import cleanup_stale_backups


def test_cleanup_stale_backups_removes_only_old(monkeypatch, tmp_workspace):
    old_dir = os.path.join(tmp_workspace, "run-old")
    new_dir = os.path.join(tmp_workspace, "run-new")
    os.makedirs(old_dir)
    os.makedirs(new_dir)
    # 把旧目录 mtime 推到 8 天前
    t = time.time() - 8 * 86400
    os.utime(old_dir, (t, t))
    monkeypatch.setenv("CKUN_AGENT_BACKUP_DIR", tmp_workspace)
    removed = cleanup_stale_backups(max_age_days=7)
    assert removed == 1
    assert not os.path.exists(old_dir)
    assert os.path.exists(new_dir)


class _NoClose:
    """包装 sqlite 连接：吞掉 database.py 函数 finally 里的 conn.close()"""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_persist_and_load_keep_content(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE agent_file_changes (
            id TEXT PRIMARY KEY, agent_run_id TEXT NOT NULL,
            file_path TEXT NOT NULL, abs_path TEXT NOT NULL,
            operation TEXT NOT NULL, added_lines INTEGER DEFAULT 0,
            removed_lines INTEGER DEFAULT 0, before_size INTEGER DEFAULT 0,
            after_size INTEGER DEFAULT 0, accepted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            before_content TEXT, after_content TEXT)"""
    )
    monkeypatch.setattr(db, "_get_conn", lambda: _NoClose(conn))

    with _LOCK:
        _PENDING["run1"] = {
            "/tmp/a.txt": FileChange(
                agent_run_id="run1", file_path="a.txt", abs_path="/tmp/a.txt",
                operation="modify", added_lines=1, removed_lines=1,
                before_size=10, after_size=11,
                before_content=["old line"], after_content=["new line"],
            )
        }
    try:
        from agent.file_changes import persist_file_changes
        persist_file_changes("run1")
        rows = db.get_agent_file_changes("run1")
        assert len(rows) == 1
        assert rows[0]["before_content"] == "old line"
        assert rows[0]["after_content"] == "new line"

        # 模拟进程重启：清空内存缓存后 load_file_changes 应回填内容
        with _LOCK:
            _PENDING.pop("run1", None)
        from agent.file_changes import load_file_changes
        load_file_changes("run1")
        with _LOCK:
            change = _PENDING["run1"]["/tmp/a.txt"]
        assert change.before_content == ["old line"]
        assert change.after_content == ["new line"]
    finally:
        with _LOCK:
            _PENDING.pop("run1", None)


def test_undo_falls_back_to_db_content(monkeypatch, tmp_workspace):
    """modify 且磁盘备份缺失时，/undo 用 DB before_content 恢复原内容"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE agent_file_changes (
            id TEXT PRIMARY KEY, agent_run_id TEXT NOT NULL,
            file_path TEXT NOT NULL, abs_path TEXT NOT NULL,
            operation TEXT NOT NULL, added_lines INTEGER DEFAULT 0,
            removed_lines INTEGER DEFAULT 0, before_size INTEGER DEFAULT 0,
            after_size INTEGER DEFAULT 0, accepted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            before_content TEXT, after_content TEXT)"""
    )
    target = os.path.join(tmp_workspace, "victim.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("被 agent 改过的内容")
    conn.execute(
        "INSERT INTO agent_file_changes "
        "(id, agent_run_id, file_path, abs_path, operation, before_content) "
        "VALUES ('x', 'run9', 'victim.txt', ?, 'modify', ?)",
        (target, "原始内容 line1\n原始内容 line2"),
    )
    conn.commit()

    rows = conn.execute("SELECT * FROM agent_file_changes").fetchall()
    rows = [dict(r) for r in rows]
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    monkeypatch.setattr(db, "get_agent_run", lambda rid: {"id": rid, "status": "completed"})
    monkeypatch.setattr(db, "get_agent_file_changes", lambda rid: rows)
    monkeypatch.setattr(db, "delete_agent_file_changes", lambda rid: None)
    # 备份根指向空目录 → 无磁盘备份，强制走 DB 兜底
    monkeypatch.setenv("CKUN_AGENT_BACKUP_DIR", os.path.join(tmp_workspace, "empty-backups"))
    os.makedirs(os.path.join(tmp_workspace, "empty-backups"), exist_ok=True)

    from routes.agent import undo_agent_run_changes
    result = undo_agent_run_changes("run9")

    assert result["undone"] is True, f"undo 失败: {result}"
    assert target in result["restored"]
    with open(target, "r", encoding="utf-8") as f:
        assert f.read() == "原始内容 line1\n原始内容 line2"
