"""run 级一键回滚 — undo 落库与回滚关联存储（功能 B 独占）。

两张表（幂等建表 + 参数绑定，连接复用 ``database._get_conn()``，与主库同一
SQLite 文件、同一 WAL/外键配置）：

- ``run_undo_actions``  一行一个 undo 动作（agent_runs 1:N，FK 级联随
  ``prune_agent_runs`` 清理——``database.py:2920`` 注释明示 tool_calls 同法）。
  放新表而非 agent_runs 加 JSON 列：snapshot_b64 单行可达 ~267KB
  （B64_READ_MAX_BYTES=200KB 原始字节，``ops_common.py:241``），JSON 列会把
  列表投影查询（``database._RUN_LIST_COLUMNS``）一起拖重。
- ``run_rollbacks``     源 run → 回滚 run 的关联行；它是 undo_restore 工具
  归属校验的数据前提（fail-closed：调用 run 必须登记在 rollback_run_id）。

两个不可妥协的写入语义（设计文档 §2.2）：
1. **headless run 防孤儿**：无头 run 不经 ``create_agent_run``（全仓唯一调用点
   ``routes/agent.py:1009``）也会流经 ``driver._emit_done``——写入前先查
   ``agent_runs`` 行存在，不存在则整批跳过（fail-closed）。
2. **prune 竞态防护**：``database.py:80`` 每连接 ``PRAGMA foreign_keys=ON``，
   FK CASCADE 真实生效；同时「存在性检查 → 插入」之间 agent_runs 行可能被
   ``prune_agent_runs`` 竞态删除，INSERT 将抛 ``sqlite3.IntegrityError``——
   按 **run 级整体 catch**，整批放弃 + warning + 返回 False，绝不向上抛。

SQL 书写规约：每条语句都是 ``conn.execute(...)`` 调用内的**内联字符串字面量**
（单一字面量、零插值、零变量间接），值一律 ``?`` 参数绑定 —— 外部输入永不进
SQL 文本，也不存在可被调包的语句变量。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from typing import Any, Optional

import database as db

logger = logging.getLogger(__name__)

# snapshot_b64 落库上限（base64 字符数）。原始字节 200KB → b64 约 267KB；
# 512KB 的余量兜住多块拼接的极端形态，超出拒写该行（防多文件编辑 run 撑爆 DB）
MAX_SNAPSHOT_B64_CHARS = 512 * 1024

# run_undo_actions.status 取值域
STATUS_PENDING = "pending"
STATUS_AUTO_ROLLED_BACK = "auto_rolled_back"   # FAILED run 收尾时已被 undo_all 自动回滚
STATUS_ROLLED_BACK = "rolled_back"             # 回滚 run 内经 undo_restore 还原完成

# 可再回滚的 run 终态：FAILED 的条目已 auto_rolled_back，不再提供再回滚
ROLLBACKABLE_RUN_STATUSES = ("completed", "human_help", "max_reached")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表。每次调用都执行（不用模块级布尔缓存：测试会整体换库，
    缓存会把「旧库已建」误当成「新库已建」）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_undo_actions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_run_id    TEXT NOT NULL,
            seq             INTEGER NOT NULL,
            kind            TEXT NOT NULL,
            tool            TEXT NOT NULL DEFAULT '',
            command         TEXT NOT NULL DEFAULT '',
            reverse_command TEXT,
            path            TEXT,
            snapshot_b64    TEXT,
            existed         INTEGER,
            reason          TEXT,
            confidence      TEXT NOT NULL DEFAULT 'manual',
            ssh_session_id  TEXT NOT NULL DEFAULT '',
            host            TEXT NOT NULL DEFAULT '',
            environment     TEXT NOT NULL DEFAULT 'test',
            run_status      TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'pending',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_run_undo_run ON run_undo_actions(agent_run_id, seq)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_rollbacks (
            id              TEXT PRIMARY KEY,
            source_run_id   TEXT NOT NULL,
            rollback_run_id TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'running',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (source_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (rollback_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_run_rollbacks_source ON run_rollbacks(source_run_id)")


# ──────────────────────────────────────────────────────────────────────────────
# 写入
# ──────────────────────────────────────────────────────────────────────────────

def _record_rows_sync(run_id: str, rows: list[dict[str, Any]], run_status: str) -> bool:
    """单事务批量插入一个 run 的 undo 行（同步；调用方负责 to_thread）。"""
    conn = db._get_conn()
    try:
        _ensure_schema(conn)
        # 防孤儿闸：headless run 没有 agent_runs 行，整批跳过
        exists = conn.execute("SELECT 1 FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if not exists:
            logger.info("undo 落库跳过：run %s 无 agent_runs 行（headless run）", run_id)
            return False
        # 幂等：同 run 已记录过则跳过（done 事件重复投递 / 调用方重试）
        already = conn.execute("SELECT 1 FROM run_undo_actions WHERE agent_run_id = ? LIMIT 1", (run_id,)).fetchone()
        if already:
            logger.debug("undo 落库跳过：run %s 已有记录", run_id)
            return False
        for row in rows:
            snapshot = row.get("snapshot_b64")
            if snapshot and len(snapshot) > MAX_SNAPSHOT_B64_CHARS:
                # 超限拒写该行（不是整批失败）：其余条目照常可回滚
                logger.warning(
                    "undo 落库：run %s seq %s 的快照 %d 字符超上限 %d，拒写该行",
                    run_id, row.get("seq"), len(snapshot), MAX_SNAPSHOT_B64_CHARS,
                )
                snapshot = None
                if row.get("kind") == "file" and row.get("existed"):
                    # 内容级还原依赖快照；快照没了就降为 manual，回滚计划会排除它
                    row["confidence"] = "manual"
            existed = row.get("existed")
            conn.execute("INSERT INTO run_undo_actions (agent_run_id, seq, kind, tool, command, reverse_command, path, snapshot_b64, existed, reason, confidence, ssh_session_id, host, environment, run_status, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                run_id,
                int(row.get("seq", 0)),
                str(row.get("kind") or "noop"),
                str(row.get("tool") or ""),
                str(row.get("command") or ""),
                row.get("reverse_command"),
                row.get("path"),
                snapshot,
                (1 if existed else 0) if existed is not None else None,
                row.get("reason"),
                str(row.get("confidence") or "manual"),
                str(row.get("ssh_session_id") or ""),
                str(row.get("host") or ""),
                str(row.get("environment") or "test"),
                str(run_status or ""),
                STATUS_AUTO_ROLLED_BACK if run_status == "failed" else STATUS_PENDING,
            ))
        conn.commit()
        return True
    except sqlite3.IntegrityError as e:
        # prune 竞态：存在性检查通过后、提交前 agent_runs 行被删（FK ON，
        # database.py:80）→ FK 约束失败。按 run 级整批放弃，绝不向上抛。
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("undo 落库放弃（run %s，agent_runs 可能已被 prune）：%s", run_id, e)
        return False
    finally:
        conn.close()


async def record_run_undo(ctx: Any, final_state: Any) -> bool:
    """run 收尾时把 ctx.undo_stack 持久化（driver._emit_done 的插入点调用）。

    快照必须发生在任何 await 之前 —— 本协程由 _emit_done 在 FAILED 分支
    undo_all 之前 await，栈此刻尚未被执行/清空。持久化是增强：内部任何异常
    都吞掉并返回 False，不允许拖死 run 收尾。
    """
    try:
        from agent.harness.undo_serial import snapshot_stack

        rows = snapshot_stack(ctx)  # 同步快照，必须先于任何 await
        if not rows:
            return False
        run_id = str(getattr(ctx, "run_id", "") or "")
        if not run_id:
            return False
        status_value = str(getattr(final_state, "value", final_state) or "")
        return await asyncio.to_thread(_record_rows_sync, run_id, rows, status_value)
    except Exception as e:  # noqa: BLE001 — 持久化失败不允许阻断 run 收尾
        logger.warning("undo 持久化失败（不阻断 run 收尾）: %s", e)
        return False


def mark_undone(run_id: str, seq: int) -> bool:
    """undo_restore 成功还原后把该条目标记 rolled_back。

    返回值 = 本次是否**发生变化**（已是 rolled_back 的行再标返回 False）：
    幂等簿记语义，调用方（undo_restore）不依赖它做控制流。"""
    conn = db._get_conn()
    try:
        _ensure_schema(conn)
        cur = conn.execute("UPDATE run_undo_actions SET status = ? WHERE agent_run_id = ? AND seq = ? AND status <> ?", (STATUS_ROLLED_BACK, run_id, int(seq), STATUS_ROLLED_BACK))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# 读取（列表/展示路径一律不含 snapshot_b64）
# ──────────────────────────────────────────────────────────────────────────────

def _row_to_public(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if "existed" in d and d["existed"] is not None:
        d["existed"] = bool(d["existed"])
    return d


def get_undo_actions(run_id: str) -> list[dict[str, Any]]:
    """某 run 的全部 undo 行（按 seq 升序），不含 snapshot_b64（大字段不出库）。"""
    conn = db._get_conn()
    try:
        _ensure_schema(conn)
        rows = conn.execute("SELECT seq, kind, tool, command, reverse_command, path, existed, reason, confidence, ssh_session_id, host, environment, run_status, status, created_at FROM run_undo_actions WHERE agent_run_id = ? ORDER BY seq ASC", (run_id,)).fetchall()
        return [_row_to_public(r) for r in rows]
    finally:
        conn.close()


def get_undo_action(run_id: str, seq: int) -> Optional[dict[str, Any]]:
    """单条 undo 行（**含 snapshot_b64**，仅供 undo_restore 工具内部使用）。"""
    conn = db._get_conn()
    try:
        _ensure_schema(conn)
        row = conn.execute("SELECT agent_run_id, seq, kind, tool, command, reverse_command, path, snapshot_b64, existed, reason, confidence, ssh_session_id, host, environment, run_status, status FROM run_undo_actions WHERE agent_run_id = ? AND seq = ? LIMIT 1", (run_id, int(seq))).fetchone()
        return _row_to_public(row) if row else None
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# run_rollbacks 关联
# ──────────────────────────────────────────────────────────────────────────────

def register_rollback(source_run_id: str, rollback_run_id: str) -> dict[str, Any]:
    """登记源 run → 回滚 run 关联（幂等：同对已存在则原样返回）。"""
    conn = db._get_conn()
    try:
        _ensure_schema(conn)
        existing = conn.execute("SELECT id, source_run_id, rollback_run_id, status, created_at FROM run_rollbacks WHERE source_run_id = ? AND rollback_run_id = ?", (source_run_id, rollback_run_id)).fetchone()
        if existing:
            return dict(existing)
        row_id = uuid.uuid4().hex
        conn.execute("INSERT INTO run_rollbacks (id, source_run_id, rollback_run_id) VALUES (?, ?, ?)", (row_id, source_run_id, rollback_run_id))
        conn.commit()
        return {"id": row_id, "source_run_id": source_run_id, "rollback_run_id": rollback_run_id, "status": "running"}
    finally:
        conn.close()


def get_rollback_for_run(rollback_run_id: str) -> Optional[dict[str, Any]]:
    """按回滚 run 反查关联行（undo_restore 归属校验 + 轨迹页展示）。"""
    conn = db._get_conn()
    try:
        _ensure_schema(conn)
        row = conn.execute("SELECT id, source_run_id, rollback_run_id, status, created_at FROM run_rollbacks WHERE rollback_run_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (rollback_run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_rollbacks_for_source(source_run_id: str) -> list[dict[str, Any]]:
    """某源 run 的全部回滚关联（轨迹页展示「已被回滚过」）。"""
    conn = db._get_conn()
    try:
        _ensure_schema(conn)
        rows = conn.execute("SELECT id, source_run_id, rollback_run_id, status, created_at FROM run_rollbacks WHERE source_run_id = ? ORDER BY created_at ASC", (source_run_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
