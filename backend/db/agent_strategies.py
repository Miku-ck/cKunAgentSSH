"""Agent 策略 CRUD（reflection / evolved_config）— 从 database.py 原样复制的领域模块（P0 阶段 3）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
迁移期两份实现必须逐字一致（AST parity，参照 tests/test_db_schedules_parity.py 的口径）。
"""
import json
from typing import Optional

from db._conn import _get_conn
from db._util import _new_id, _now

__all__ = [
    "add_agent_strategy",
    "get_agent_strategies",
    "update_agent_strategy_enabled",
    "delete_agent_strategy",
]


def add_agent_strategy(
    mode: str,
    strategy_type: str,
    name: str,
    content: str,
    config: Optional[dict] = None,
    score: float = 0.5,
    enabled: int = 1,
) -> str:
    """添加一条 Agent 策略，返回新策略 id"""
    sid = _new_id()
    now = _now()
    cfg_json = json.dumps(config) if config else None
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_strategies "
            "(id, mode, strategy_type, name, content, config, score, enabled, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, mode, strategy_type, name, content, cfg_json, score, enabled, now, now),
        )
        conn.commit()
        return sid
    finally:
        conn.close()


def get_agent_strategies(
    mode: Optional[str] = None,
    strategy_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """获取 Agent 策略列表"""
    conn = _get_conn()
    try:
        conditions = []
        params: list = []
        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        if strategy_type:
            conditions.append("strategy_type = ?")
            params.append(strategy_type)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            "SELECT id, mode, strategy_type, name, content, config, score, enabled, "
            "       created_at, updated_at "
            f"FROM agent_strategies {where_clause} "
            "ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_agent_strategy_enabled(strategy_id: str, enabled: int) -> bool:
    """启用/禁用 Agent 策略"""
    now = _now()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE agent_strategies SET enabled = ?, updated_at = ? WHERE id = ?",
            (enabled, now, strategy_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_agent_strategy(strategy_id: str) -> bool:
    """删除单条 Agent 策略"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM agent_strategies WHERE id = ?", (strategy_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
