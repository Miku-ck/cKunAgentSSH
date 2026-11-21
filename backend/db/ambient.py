"""群聊环境上下文落库（ambient lines，M9-5）— P2-3 从 database.py 拆出。

write-through：入站群消息与机器人发言两个落点写 `bot_ambient_lines` 表，
重启后按 TTL 窗口恢复。函数体与入口签名不变。
"""
from db import _conn as _conn_mod
from db import _util as _util_mod


def add_bot_ambient_line(
    channel_id: str, group_openid: str, speaker: str, content: str, created_at: str
) -> None:
    """写一条群环境上下文（write-through；失败由调用方降级为仅内存）。"""
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_ambient_lines (id, channel_id, group_openid, speaker, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_util_mod._new_id(), channel_id or "", group_openid or "", speaker or "",
             content or "", created_at),
        )
        conn.commit()
    finally:
        conn.close()


def list_recent_ambient_lines(minutes: float = 10.0) -> "list[dict]":
    """读 TTL 窗口内的全部环境上下文行（重启恢复用，跨 channel 一次灌回）。"""
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            "SELECT channel_id, group_openid, speaker, content, created_at "
            "FROM bot_ambient_lines "
            "WHERE created_at >= datetime('now', ?) "
            "ORDER BY created_at ASC, rowid ASC",
            (f"-{float(minutes)} minutes",),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def purge_ambient_lines(minutes: float = 15.0) -> int:
    """清掉超过 TTL+缓冲 的环境上下文行，返回删除行数。"""
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM bot_ambient_lines WHERE created_at < datetime('now', ?)",
            (f"-{float(minutes)} minutes",),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
