"""
AuditEngine — 追加式、链式签名防篡改的运维审计

合规目标：
- 对齐等保2.0「安全审计」要求：Agent 对本地/远程主机执行的命令、
  审批结论、拦截决定，全部落一条不可篡改的审计链。
- 每次追加记录前，读取链尾 hash 作为 prev_hash，计算自身 hash，形成单向链。
- 任何一条记录的修改/删除都会让后续校验失败（tamper-evident）。

设计取舍：
- 追加写（append-only）：本模块不提供 UPDATE/DELETE 审计记录的接口；
  即使直接操纵 SQLite 也会破坏 hash 链而被 verify() 检出。
- 单条写入使用事务 + BEGIN IMMEDIATE 串行化，避免并发下链式 hash 竞态。
- 审计失败不阻塞 Agent：append 内部 try/except，写库异常仅记 warning。
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import database

logger = logging.getLogger(__name__)

AUDIT_TABLE = "audit_log"
_DEFAULT_ENV = "test"


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────

def _operator() -> str:
    """单机桌面形态下取操作系统用户名作为『操作人』"""
    for var in ("USERNAME", "USER", "LOGNAME"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        return os.getlogin()
    except (OSError, AttributeError):
        return "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(record: dict[str, Any]) -> str:
    """生成确定性规范串，供 hash 计算使用。字段顺序必须稳定，勿随意增删。"""
    return "|".join([
        str(record.get("ts", "")),
        str(record.get("operator", "")),
        str(record.get("target_type", "")),
        str(record.get("target_id", "")),
        str(record.get("environment", _DEFAULT_ENV)),
        str(record.get("action", "")),
        str(record.get("command_hash", "")),
        str(record.get("command_raw", "")),
        str(record.get("result", "")),
        str(record.get("conclusion", "")),
        str(record.get("run_id", "")),
        str(record.get("iteration", "0")),
    ])


def _chain_hash(canon: str, prev_hash: str) -> str:
    """链式签名：sha256(prev_hash | canon)"""
    return hashlib.sha256(f"{prev_hash}|{canon}".encode("utf-8")).hexdigest()


def command_hash(command: str) -> str:
    """命令规范化哈希（与 PermissionGate 使用同一规范化入口）"""
    if not command:
        return ""
    try:
        from agent.harness.safety import normalize_command_for_detection
        return hashlib.sha256(
            normalize_command_for_detection(str(command)).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(str(command).encode("utf-8")).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────────────
# 表结构（在 database.init_db 中也创建，这里是兜底自检）
# ──────────────────────────────────────────────────────────────────────────────

_AUDIT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    operator     TEXT NOT NULL DEFAULT '',
    target_type  TEXT NOT NULL DEFAULT '',
    target_id    TEXT NOT NULL DEFAULT '',
    environment  TEXT NOT NULL DEFAULT '{_DEFAULT_ENV}',
    action       TEXT NOT NULL DEFAULT '',
    command_hash TEXT NOT NULL DEFAULT '',
    command_raw  TEXT NOT NULL DEFAULT '',
    result       TEXT NOT NULL DEFAULT '',
    conclusion   TEXT NOT NULL DEFAULT '',
    run_id       TEXT NOT NULL DEFAULT '',
    iteration    INTEGER NOT NULL DEFAULT 0,
    prev_hash    TEXT NOT NULL DEFAULT '',
    hash         TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON {AUDIT_TABLE}(ts);
CREATE INDEX IF NOT EXISTS idx_audit_run    ON {AUDIT_TABLE}(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_target ON {AUDIT_TABLE}(target_id);
CREATE INDEX IF NOT EXISTS idx_audit_env    ON {AUDIT_TABLE}(environment);
"""


def init() -> None:
    """确保审计表存在（database.init_db 已调用；此处兜底幂等）"""
    try:
        conn = database._get_conn()
        conn.executescript(_AUDIT_SCHEMA)
    except Exception as e:
        logger.warning("audit.init 失败: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# 追加写
# ──────────────────────────────────────────────────────────────────────────────

def append_record(
    *,
    action: str,
    conclusion: str,
    run_id: str = "",
    iteration: int = 0,
    target_type: str = "",
    target_id: str = "",
    environment: str = _DEFAULT_ENV,
    command_raw: str = "",
    result: str = "",
    operator: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """追加一条审计记录（审计失败静默，返回 None 不阻塞 Agent）"""
    if conclusion not in ("executed", "failed", "blocked", "denied",
                          "auto_allowed", "approved", "revoked"):
        conclusion = "executed"
    environment = environment or _DEFAULT_ENV
    record = {
        "ts": _now(),
        "operator": operator or _operator(),
        "target_type": target_type or "",
        "target_id": target_id or "",
        "environment": environment,
        "action": action,
        "command_hash": command_hash(command_raw),
        "command_raw": command_raw,
        "result": result,
        "conclusion": conclusion,
        "run_id": run_id,
        "iteration": int(iteration or 0),
    }
    try:
        conn = database._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            prev = _last_hash(conn)
            record["prev_hash"] = prev
            record["hash"] = _chain_hash(_canonical(record), prev)
            cur = conn.execute(
                f"INSERT INTO {AUDIT_TABLE} "
                f"(ts, operator, target_type, target_id, environment, action, "
                f" command_hash, command_raw, result, conclusion, run_id, iteration, "
                f" prev_hash, hash) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["ts"],
                    record["operator"],
                    record["target_type"],
                    record["target_id"],
                    record["environment"],
                    record["action"],
                    record["command_hash"],
                    record["command_raw"],
                    record["result"],
                    record["conclusion"],
                    record["run_id"],
                    record["iteration"],
                    record["prev_hash"],
                    record["hash"],
                ),
            )
            record["id"] = cur.lastrowid
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        return record
    except Exception as e:
        logger.warning("审计写入失败: %s", e)
        return None


def _last_hash(conn) -> str:
    cur = conn.execute(f"SELECT hash FROM {AUDIT_TABLE} ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else ""


# ──────────────────────────────────────────────────────────────────────────────
# 查询 / 校验 / 导出
# ──────────────────────────────────────────────────────────────────────────────

def query(
    environment: Optional[str] = None,
    target_id: Optional[str] = None,
    action: Optional[str] = None,
    conclusion: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按条件查询审计记录（倒序）"""
    conds: list[str] = []
    params: list[Any] = []
    if environment:
        conds.append("environment = ?")
        params.append(environment)
    if target_id:
        conds.append("target_id = ?")
        params.append(target_id)
    if action:
        conds.append("action = ?")
        params.append(action)
    if conclusion:
        conds.append("conclusion = ?")
        params.append(conclusion)
    if run_id:
        conds.append("run_id = ?")
        params.append(run_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    params.append(offset)
    try:
        conn = database._get_conn()
        cur = conn.execute(
            f"SELECT * FROM {AUDIT_TABLE} {where} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("审计查询失败: %s", e)
        return []


def count(
    environment: Optional[str] = None,
    target_id: Optional[str] = None,
    conclusion: Optional[str] = None,
) -> int:
    conds: list[str] = []
    params: list[Any] = []
    if environment:
        conds.append("environment = ?")
        params.append(environment)
    if target_id:
        conds.append("target_id = ?")
        params.append(target_id)
    if conclusion:
        conds.append("conclusion = ?")
        params.append(conclusion)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    try:
        conn = database._get_conn()
        cur = conn.execute(f"SELECT COUNT(*) FROM {AUDIT_TABLE} {where}", params)
        return int(cur.fetchone()[0])
    except Exception as e:
        logger.warning("审计计数失败: %s", e)
        return 0


def verify() -> dict[str, Any]:
    """全链校验：重算每条记录 hash 与 prev_hash 是否一致

    Returns:
        {"intact": bool, "total": int, "checked": int, "broken": [id,...]}
    """
    broken: list[int] = []
    total = 0
    checked = 0
    try:
        conn = database._get_conn()
        cur = conn.execute(
            f"SELECT id, prev_hash, hash, ts, operator, target_type, target_id, "
            f"environment, action, command_hash, command_raw, result, conclusion, "
            f"run_id, iteration FROM {AUDIT_TABLE} ORDER BY id ASC"
        )
        prev_hash = ""
        for row in cur.fetchall():
            total += 1
            record = {
                "ts": row[3], "operator": row[4], "target_type": row[5],
                "target_id": row[6], "environment": row[7], "action": row[8],
                "command_hash": row[9], "command_raw": row[10], "result": row[11],
                "conclusion": row[12], "run_id": row[13], "iteration": row[14],
            }
            expected = _chain_hash(_canonical(record), prev_hash)
            checked += 1
            if row[2] != expected or row[1] != prev_hash:
                broken.append(int(row[0]))
                # 链已断，后续记录也不可信，但继续统计便于定位
            prev_hash = row[2]
    except Exception as e:
        logger.warning("审计校验失败: %s", e)
        return {"intact": False, "total": 0, "checked": 0,
                "broken": [], "error": str(e)}
    return {"intact": not broken, "total": total, "checked": checked,
            "broken": broken}


def export_rows() -> list[dict[str, Any]]:
    """导出全部审计记录（供 CSV/JSON 导出）"""
    try:
        conn = database._get_conn()
        cur = conn.execute(
            f"SELECT * FROM {AUDIT_TABLE} ORDER BY id ASC"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("审计导出失败: %s", e)
        return []