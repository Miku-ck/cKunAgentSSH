"""bot 社交域：计划钩子 / 主动性 / 画廊 / 消息 / 关系 / 状态（P2-3 从 database.py 拆出，函数体与入口不变）。"""

import json
import sqlite3
from typing import Optional

from db import _conn as _conn_mod
from db._util import _new_id, _now

# ── P3 主动性：寄存队列与日志 ──

# ── 计划钩子（spec 2026-10-31-plan-followup）──
#
# 状态四态：pending → asked（问出去了）/ superseded（同一件事改了时间）/ expired
# （超窗没来得及问）。时间戳一律 UTC 字符串，可按字典序比较。
#
# due_start 目前只写不读（留给「没说准哪天」的宽窗判断）；memory_id 目前恒写
# NULL —— record_hooks 在 _write_facts 之前跑，那时记忆行还不存在。这一列留
# 给「记忆行被维护淘汰后仍能回指」，真要溯源时再把 _write_facts 提前并回传 id。


def add_plan_hook(
    persona_id: str,
    conversation_id: Optional[str],
    memory_id: Optional[str],
    plan_text: str,
    when_text: str,
    due_start: str,
    due_end: str,
    created_at: str,
) -> str:
    """写一条计划钩子；同人格同文本的 pending 先作废 —— 新时间才是对的。

    作废与插入放在同一个事务里：两次 commit 之间断掉会丢一条已经改期的计划。
    """
    # supersede 按文本匹配（spec §6）：不 strip 的话「看展」「看展 」会各活一条
    plan_text = (plan_text or "").strip()
    hook_id = _new_id()
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE bot_plan_hooks SET state = 'superseded' "
            "WHERE persona_id = ? AND plan_text = ? AND state = 'pending'",
            (persona_id, plan_text),
        )
        conn.execute(
            "INSERT INTO bot_plan_hooks (id, persona_id, conversation_id, memory_id, "
            "plan_text, when_text, due_start, due_end, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,'pending',?)",
            (
                hook_id, persona_id, conversation_id, memory_id, plan_text,
                when_text, due_start, due_end, created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return hook_id


def due_plan_hook(
    persona_id: str, now_iso: str, earliest_iso: str,
    conversation_id: Optional[str] = None,
) -> Optional[dict]:
    """可回溯的钩子：窗口已结束（due_end <= now）且没超过回溯期，最早到期优先。

    conversation_id 给了就只认这个会话里说过的计划 —— 群里问「你不是说周末要去看展吗」
    而那件事是他私聊里说的，等于把他的行程广播给一整个群。
    """
    conn = _conn_mod._get_conn()
    sql = (
        "SELECT * FROM bot_plan_hooks "
        "WHERE persona_id = ? AND state = 'pending' "
        "AND due_end <= ? AND due_end >= ?"
    )
    params: list = [persona_id, now_iso, earliest_iso]
    if conversation_id is not None:
        sql += " AND conversation_id = ?"
        params.append(conversation_id)
    sql += " ORDER BY due_end ASC LIMIT 1"
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_plan_asked(hook_id: str, asked_at: str) -> bool:
    """把钩子标记为已问。只允许 pending → asked，重复调用返回 False。"""
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            "UPDATE bot_plan_hooks SET state = 'asked', asked_at = ? "
            "WHERE id = ? AND state = 'pending'",
            (asked_at, hook_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def expire_plan_hooks(cutoff_iso: str) -> int:
    """超窗没问出去的 pending 置 expired（保留审计行，不物理删）。"""
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            "UPDATE bot_plan_hooks SET state = 'expired' "
            "WHERE state = 'pending' AND due_end < ?",
            (cutoff_iso,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def add_bot_proactive_log(
    persona_id: str,
    conversation_id: str,
    desire: float,
    action: str,
    reason: str = "",
    text: str = "",
    expires_at: Optional[str] = None,
    delivered_at: Optional[str] = None,
) -> str:
    log_id = _new_id()
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_proactive_log (id, persona_id, conversation_id, desire, "
            "action, reason, text, expires_at, delivered_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (log_id, persona_id, conversation_id, float(desire), action,
             reason, text, expires_at, delivered_at),
        )
        conn.commit()
    finally:
        conn.close()
    return log_id


def get_pending_proactive(conversation_id: str) -> Optional[dict]:
    """该会话最早一条未过期、未投递的寄存心里话；没有则 None。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bot_proactive_log WHERE conversation_id = ? AND action = 'queued' "
            "AND delivered_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at ASC LIMIT 1",
            (conversation_id, _now()),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def mark_proactive_delivered(log_id: str) -> None:
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE bot_proactive_log SET delivered_at = ?, action = 'send' WHERE id = ?",
            (_now(), log_id),
        )
        conn.commit()
    finally:
        conn.close()


def last_user_message_at(conversation_id: str) -> Optional[str]:
    """该会话里用户最后一次说话的时间：召回分档的锚点（会话级，不能用关系级 last_at）。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(created_at) AS last FROM bot_messages "
            "WHERE conversation_id = ? AND role = 'user'",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["last"] if row else None


def has_wakeup_period(conversation_id: str, period: int) -> bool:
    """这一档召回（当天/1-3/3-7/7-30 天）是否已经用掉。

    子串匹配而不是前缀匹配：手动探口那句的 reason 带着 manual: 前缀，钉在开头
    就会把「靠试发用掉的召回档」当成没用过，下一轮自动开口再撞同一档被平台拒。
    """
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM bot_proactive_log WHERE conversation_id = ? "
            "AND reason LIKE ? LIMIT 1",
            (conversation_id, f"%wakeup{int(period)}:%"),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def expire_stale_proactive(now: str) -> int:
    """过期寄存改记为 expired（保留审计行，不物理删）。返回过期条数。"""
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            "UPDATE bot_proactive_log SET action = 'expired' "
            "WHERE action = 'queued' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def list_proactive_targets() -> "list[dict]":
    """开启主动性的巡检清单：local 会话（可直投）+ 30 天内活跃的 QQ 会话，
    且会话上的人格仍是该机器人当前绑定的那个人格。

    窗口按官方召回口径取 30 天：7 天会把「7-30 天」那一档整段砍掉；更久没动静的
    会话寄存心里话也没意义，还白撑队列。

    群会话还要再过一道群参与档：只有开到 proactive 的人格才允许在群里自己开口。
    闸口留在 SQL 里而不是调度循环：这里拦掉就不会白跑一次状态快照与意愿计算。
    两处 NULL 都要先兜住：LIKE 与 <> 遇上 NULL 结果都是 NULL，NOT 之后会把
    external_id 还是 NULL 的本地会话整条误删（三值逻辑不报错，只是静默少行）。
    """
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            "SELECT p.id AS persona_id, p.name, p.relationship_speed, "
            "c.id AS conversation_id, c.channel, c.channel_id AS bot_channel_id, "
            "c.external_id "
            "FROM bot_personas p JOIN bot_conversations c ON c.persona_id = p.id "
            "WHERE p.proactive_enabled = 1 AND ("
            "  c.channel = 'local'"
            "  OR (c.channel = 'qq' AND c.updated_at >= datetime('now', '-30 days'))) "
            # 会话上的人格必须还是那个机器人当前的人格。换人格后旧会话行留着
            # 当可读历史，但它们不再有权用同一个账号主动开口 —— 否则老人格和
            # 新人格共用一张嘴。渠道行没了（子查询返回 NULL）同样不投：没句可发。
            "AND (c.channel_id IS NULL OR c.persona_id = ("
            "  SELECT b.persona_id FROM bot_channels b WHERE b.id = c.channel_id)) "
            "AND NOT (IFNULL(c.external_id, '') LIKE 'group:%' "
            "     AND IFNULL(p.group_mode, 'mention_only') <> 'proactive')"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_proactive_target(conversation_id: str) -> "Optional[dict]":
    """指定会话的探口行：与 list_proactive_targets 同一形状，但不带任何行为闸。

    手动「试一句」要绕过的正是总闸/群档/30 天窗口这三道「该不该自动开口」的判据，
    但调度器读的字段必须一字不少 —— 所以复用同一段 SELECT，只把 WHERE 换成按 id 定位。
    """
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT p.id AS persona_id, p.name, p.relationship_speed, "
            "c.id AS conversation_id, c.channel, c.channel_id AS bot_channel_id, "
            "c.external_id "
            "FROM bot_personas p JOIN bot_conversations c ON c.persona_id = p.id "
            "WHERE c.id = ?",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def latest_proactive_row(conversation_id: str) -> "Optional[dict]":
    """该会话最近一条主动消息账本行：手动探口的回显就念这一行。

    不给调度器开「自己拼回显结果」的口 —— 屏幕上的那句结论与落库的那一行同源，
    不会对不上。created_at 只到秒，同秒写入时用 rowid 定序。
    """
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bot_proactive_log WHERE conversation_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def proactive_sent_today(persona_id: str, day_start: str, *, exclude_groups: bool = False) -> int:
    # 统计口径 = 「已开口」三种动作（send/push/queued）。漏掉后两种的话，
    # QQ 侧每次 tick 都当没说过话，会每 60 秒重说一遍。
    # exclude_groups：调度器算单聊配额时要排掉群开口，否则两个群各发一条
    # 就能把这个人格当天的单聊主动性全饿死（每群独立，spec 2026-10-30-qq-group-ambient §3）。
    group_filter = (
        " AND conversation_id NOT IN ("
        "SELECT id FROM bot_conversations WHERE external_id LIKE 'group:%')"
        if exclude_groups
        else ""
    )
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM bot_proactive_log "
            "WHERE persona_id = ? AND action IN ('send','push','queued') "
            "AND created_at >= ?" + group_filter,
            (persona_id, day_start),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"] or 0)


def last_proactive_at(persona_id: str, *, exclude_groups: bool = False) -> Optional[str]:
    """人设口径的上次开口时间（口径与 exclude_groups 含义见 proactive_sent_today）。"""
    group_filter = (
        " AND conversation_id NOT IN ("
        "SELECT id FROM bot_conversations WHERE external_id LIKE 'group:%')"
        if exclude_groups
        else ""
    )
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(created_at) AS last FROM bot_proactive_log "
            "WHERE persona_id = ? AND action IN ('send','push','queued')" + group_filter,
            (persona_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["last"] if row else None


def last_proactive_push_failure_at(conversation_id: str) -> Optional[str]:
    """上次「生成好了但平台没收下」的时间（action=skip 且 reason 以 push: 开头）。

    这类失败不推进任何开口闸，所以调度器拿它做退避：频控的恢复尺度是小时级，
    按 60s 一轮重试只会反复撞同一墙，还每轮白烧一次模型。
    """
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(created_at) AS last FROM bot_proactive_log "
            "WHERE conversation_id = ? AND action = 'skip' AND reason LIKE 'push:%'",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["last"] if row else None


def proactive_sent_today_for_conversation(conversation_id: str, day_start: str) -> int:
    """单会话口径的当日开口数（群聊每群独立配额，不与人设名下的其它会话互占）。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM bot_proactive_log "
            "WHERE conversation_id = ? AND action IN ('send','push','queued') "
            "AND created_at >= ?",
            (conversation_id, day_start),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"] or 0)


def last_proactive_at_for_conversation(conversation_id: str) -> Optional[str]:
    """单会话口径的上次开口时间（群聊每群独立 12h 间隔）。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(created_at) AS last FROM bot_proactive_log "
            "WHERE conversation_id = ? AND action IN ('send','push','queued')",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["last"] if row else None


def list_unchecked_proactive(older_than: str) -> "list[dict]":
    """已投递超过 older_than、尚未做负反馈检查的主动消息（主动消息与本地直投）。"""
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM bot_proactive_log WHERE action IN ('send','push') "
            "AND delivered_at IS NOT NULL "
            "AND checked_at IS NULL AND delivered_at <= ?",
            (older_than,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def mark_proactive_checked(log_id: str) -> None:
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE bot_proactive_log SET checked_at = ? WHERE id = ?", (_now(), log_id)
        )
        conn.commit()
    finally:
        conn.close()


def bot_conversation_has_user_reply_since(conversation_id: str, since: str) -> bool:
    """主动消息投递之后，用户有没有说过话（负反馈判定的一半：回了就不算冷场）。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM bot_messages WHERE conversation_id = ? AND role = 'user' "
            "AND created_at >= ? LIMIT 1",
            (conversation_id, since),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def set_bot_proactive_cooldown(persona_id: str, until: str) -> None:
    """负反馈冷却落库（bot_relations.proactive_cooldown_until）。行不存在时建行。"""
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_relations (persona_id, proactive_cooldown_until) VALUES (?, ?) "
            "ON CONFLICT(persona_id) DO UPDATE SET proactive_cooldown_until = excluded.proactive_cooldown_until",
            (persona_id, until),
        )
        conn.commit()
    finally:
        conn.close()


def update_bot_conversation_display(
    conversation_id: str,
    display_name: Optional[str] = None,
    display_avatar: Optional[str] = None,
) -> Optional[dict]:
    """远端（QQ / 微信）会话显示层覆盖。None = 不改，空串 = 清除回落默认。

    刻意不动 updated_at：改显示名不该把一条沉睡会话顶到列表最前。
    """
    # 跨子域调用（会话域在 db/bot_core.py）：局部导入避免模块循环依赖。
    from db.bot_core import get_bot_conversation

    assignments: list[str] = []
    params: list = []
    if display_name is not None:
        assignments.append("display_name = ?")
        params.append(display_name)
    if display_avatar is not None:
        assignments.append("display_avatar = ?")
        params.append(display_avatar)
    if not assignments:
        return get_bot_conversation(conversation_id)
    params.append(conversation_id)
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            f"UPDATE bot_conversations SET {', '.join(assignments)} WHERE id = ?", params
        )
        conn.commit()
        if not cur.rowcount:
            return None
        return get_bot_conversation(conversation_id)
    finally:
        conn.close()


def create_bot_gallery_item(data_url: str, label: str = "") -> dict:
    gallery_id = _new_id()
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO avatar_gallery (id, label, data_url, created_at) VALUES (?, ?, ?, ?)",
            (gallery_id, label, data_url, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM avatar_gallery WHERE id = ?", (gallery_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_bot_gallery(limit: int = 200) -> list[dict]:
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM avatar_gallery ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def delete_bot_gallery_item(gallery_id: str) -> bool:
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute("DELETE FROM avatar_gallery WHERE id = ?", (gallery_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _loads_images(value) -> list:
    """bot_messages.images 列是 JSON 文本或 NULL；读回统一归一成 list，脏值吐 []。"""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def add_bot_message(
    conversation_id: str,
    role: str,
    content: str,
    external_msg_id: Optional[str] = None,
    reasoning: Optional[str] = None,
    images: Optional[list] = None,
) -> dict:
    message_id = _new_id()
    now = _now()
    images_json = json.dumps(images) if images else None
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bot_messages "
            "(id, conversation_id, role, content, external_msg_id, reasoning, images, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, role, content, external_msg_id, reasoning, images_json, now),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT * FROM bot_messages WHERE conversation_id = ? AND external_msg_id = ?",
                (conversation_id, external_msg_id),
            ).fetchone()
            deduped = dict(row)
            deduped["images"] = _loads_images(deduped.get("images"))
            return deduped
        conn.execute(
            "UPDATE bot_conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        if role == "user":
            _title_bot_conversation_from_first_message(conn, conversation_id, content)
        conn.commit()
        return {
            "id": message_id,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "external_msg_id": external_msg_id,
            "reasoning": reasoning,
            "images": images or [],
            "answered_at": None,
            "created_at": now,
        }
    finally:
        conn.close()


_BOT_TITLE_MAX = 24


def _bot_conversation_title(content: str) -> str:
    text = " ".join((content or "").split())
    return text if len(text) <= _BOT_TITLE_MAX else text[:_BOT_TITLE_MAX] + "…"


def _title_bot_conversation_from_first_message(
    conn: sqlite3.Connection, conversation_id: str, content: str
) -> None:
    """首条用户消息替换掉创建时的人格名标题；只改一次，后续消息不覆盖。"""
    row = conn.execute(
        "SELECT title, persona_snapshot FROM bot_conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    if not row:
        return
    try:
        persona_name = json.loads(row["persona_snapshot"] or "{}").get("name", "")
    except (TypeError, ValueError):
        persona_name = ""
    if row["title"] and row["title"] != persona_name:
        return
    first = conn.execute(
        "SELECT COUNT(*) FROM bot_messages WHERE conversation_id = ? AND role = 'user'",
        (conversation_id,),
    ).fetchone()[0]
    if first != 1:
        return
    title = _bot_conversation_title(content)
    if title:
        conn.execute(
            "UPDATE bot_conversations SET title = ? WHERE id = ?",
            (title, conversation_id),
        )


def get_bot_messages(conversation_id: str, limit: int = 100) -> list[dict]:
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM (SELECT rowid AS sort_id, * FROM bot_messages WHERE conversation_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?) "
            "ORDER BY created_at ASC, sort_id ASC",
            (conversation_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("sort_id", None)
            item["images"] = _loads_images(item.get("images"))
            result.append(item)
        return result
    finally:
        conn.close()


def get_bot_message_by_external_id(conversation_id: str, external_msg_id: str) -> Optional[dict]:
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bot_messages WHERE conversation_id = ? AND external_msg_id = ?",
            (conversation_id, external_msg_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_bot_message_answered(conversation_id: str, external_msg_id: str) -> bool:
    """QQ 回复真正送达后才标记；未标记的事件允许平台重投时再处理一次。"""
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            "UPDATE bot_messages SET answered_at = ? "
            "WHERE conversation_id = ? AND external_msg_id = ? AND answered_at IS NULL",
            (_now(), conversation_id, external_msg_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_recent_bot_messages(conversation_id: str, limit: int = 40) -> list[dict]:
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            # created_at 必须带出来：chat_service 用它给状态层喂 user_created_at
            # （burst 判定要真时间戳）。漏掉这一列会让 burst 永远静默失效 ——
            # history 里每项都缺该键，turn_events 数不到任何一条 60s 内的消息。
            "SELECT role, content, images, created_at FROM bot_messages WHERE conversation_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["images"] = _loads_images(item.get("images"))
            result.append(item)
        return result
    finally:
        conn.close()


# ── Bot 关系账本 ──


def get_bot_relation(persona_id: str) -> dict:
    """读关系账本。score 是历史累计值，时间衰减由 bot.relation 在读时计算。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bot_relations WHERE persona_id = ?", (persona_id,)
        ).fetchone()
        if not row:
            return {"persona_id": persona_id, "score": 0.0, "turns": 0, "last_at": None}
        data = dict(row)
        data["score"] = float(data.get("score") or 0)
        data["turns"] = int(data.get("turns") or 0)
        return data
    finally:
        conn.close()


def list_bot_relations() -> dict[str, dict]:
    """一次性取全部人格的关系账本，供列表页避免 N+1 查询。"""
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute("SELECT * FROM bot_relations").fetchall()
        return {
            row["persona_id"]: {
                "persona_id": row["persona_id"],
                "score": float(row["score"] or 0),
                "turns": int(row["turns"] or 0),
                "last_at": row["last_at"],
            }
            for row in rows
        }
    finally:
        conn.close()


def bump_bot_relation(persona_id: str, delta: float, turns: int = 1) -> dict:
    """累加熟悉度。只增不减：衰减在读取侧算，写侧不留痕迹。"""
    now = _now()
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_relations (persona_id, score, turns, last_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(persona_id) DO UPDATE SET "
            "score = bot_relations.score + excluded.score, "
            "turns = bot_relations.turns + excluded.turns, "
            "last_at = excluded.last_at, updated_at = excluded.updated_at",
            (persona_id, float(delta), int(turns), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_bot_relation(persona_id)


def update_bot_state(
    persona_id: str,
    *,
    trust: float,
    intimacy: float,
    tension: float,
    novelty: float,
    valence: float,
    energy: float,
    patience: float,
    last_event_kind: str | None,
    last_event_at: str | None,
    last_event_count: int,
    state_at: str,
) -> None:
    """写回状态轴（bot.state 事件驱动更新后的落库点）。

    只写状态列，不碰 score/turns/last_at —— 那三列是 relation.record_turn 的
    领地，两个写入者互不越界。行不存在时补一行（score 从 0 起）。
    """
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_relations (persona_id, score, turns, trust, intimacy, tension, "
            "novelty, valence, energy, patience, last_event_kind, last_event_at, "
            "last_event_count, state_at, updated_at) "
            "VALUES (?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(persona_id) DO UPDATE SET "
            "trust = excluded.trust, intimacy = excluded.intimacy, tension = excluded.tension, "
            "novelty = excluded.novelty, valence = excluded.valence, energy = excluded.energy, "
            "patience = excluded.patience, last_event_kind = excluded.last_event_kind, "
            "last_event_at = excluded.last_event_at, last_event_count = excluded.last_event_count, "
            "state_at = excluded.state_at, updated_at = excluded.updated_at",
            (persona_id, trust, intimacy, tension, novelty, valence, energy, patience,
             last_event_kind, last_event_at, int(last_event_count), state_at, state_at),
        )
        conn.commit()
    finally:
        conn.close()
