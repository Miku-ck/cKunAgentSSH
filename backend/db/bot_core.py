"""bot 核心域：人格 / 渠道 / 会话（P2-3 从 database.py 拆出，函数体与入口不变）。"""

import json
import sqlite3
from typing import Optional

from db import _conn as _conn_mod
from db._secrets import unprotect_secret
from db._util import _new_id, _now

_PERSONA_TEXT_FIELDS = ("likes_landmines", "memory_directive", "dialogue_samples", "advanced_play")


def _bot_persona_dict(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["is_builtin"] = int(result.get("is_builtin") or 0)
    result["enable_thinking"] = int(result.get("enable_thinking") or 0)
    result["proactive_enabled"] = int(result.get("proactive_enabled") or 0)
    result.setdefault("group_mode", "mention_only")
    # 不能用 `or 1`：关掉的 0 会被当成缺字段读成 1，等于开关拨了没反应。
    raw_mention = result.get("mention_sender")
    result["mention_sender"] = 1 if raw_mention is None else int(raw_mention)
    result.setdefault("reply_style", "moderate")
    # expression_dna 以对象形态进出：库里是 JSON 文本，读出来就当 dict 用，
    # 脏数据（手工写坏/半截 JSON）一律退回空对象，宁可少一层约束也不能让陪聊 500。
    raw_dna = result.get("expression_dna")
    if isinstance(raw_dna, str):
        try:
            parsed = json.loads(raw_dna) if raw_dna.strip() else {}
        except ValueError:
            parsed = {}
        result["expression_dna"] = parsed if isinstance(parsed, dict) else {}
    elif not isinstance(raw_dna, dict):
        result["expression_dna"] = {}
    result.setdefault("reaction_style", "warm")
    result.setdefault("refusal_style", "soft")
    result.setdefault("address_user", "")
    result.setdefault("relationship_speed", "normal")
    for field in _PERSONA_TEXT_FIELDS:
        result.setdefault(field, "")
    return result


def create_bot_persona(
    name: str,
    avatar: str = "",
    personality: str = "",
    background: str = "",
    greeting: str = "",
    model_id: Optional[str] = None,
    reply_style: str = "moderate",
    enable_thinking: int = 0,
    expression_dna: Optional[dict] = None,
    reaction_style: str = "warm",
    refusal_style: str = "soft",
    address_user: str = "",
    relationship_speed: str = "normal",
    likes_landmines: str = "",
    memory_directive: str = "",
    dialogue_samples: str = "",
    advanced_play: str = "",
    proactive_enabled: int = 0,
    group_mode: str = "mention_only",
    mention_sender: int = 1,
) -> dict:
    persona_id = _new_id()
    now = _now()
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_personas "
            "(id, name, avatar, personality, background, greeting, model_id, "
            "reply_style, enable_thinking, expression_dna, reaction_style, "
            "refusal_style, address_user, relationship_speed, "
            "likes_landmines, memory_directive, dialogue_samples, advanced_play, "
            "proactive_enabled, group_mode, mention_sender, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (persona_id, name, avatar, personality, background, greeting, model_id,
             reply_style, int(bool(enable_thinking)),
             json.dumps(expression_dna or {}, ensure_ascii=False),
             reaction_style, refusal_style, address_user, relationship_speed,
             likes_landmines, memory_directive, dialogue_samples, advanced_play,
             int(bool(proactive_enabled)), group_mode, int(bool(mention_sender)),
             now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_bot_persona(persona_id)


def list_bot_personas() -> list[dict]:
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM bot_personas ORDER BY is_builtin DESC, created_at ASC, id ASC"
        ).fetchall()
        return [_bot_persona_dict(row) for row in rows]
    finally:
        conn.close()


def get_bot_persona(persona_id: str) -> Optional[dict]:
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bot_personas WHERE id = ?", (persona_id,)
        ).fetchone()
        return _bot_persona_dict(row) if row else None
    finally:
        conn.close()


def update_bot_persona(persona_id: str, **fields) -> Optional[dict]:
    allowed = {"name", "avatar", "personality", "background", "greeting", "model_id",
               "reply_style", "enable_thinking", "expression_dna", "reaction_style",
               "refusal_style", "address_user", "relationship_speed",
               "proactive_enabled", "group_mode", "mention_sender",
               *_PERSONA_TEXT_FIELDS}
    data = {key: value for key, value in fields.items() if key in allowed}
    # expression_dna 对外是对象、对内是 JSON 文本：入口统一序列化，
    # 调用方（路由/PATCH 局部更新）不必关心存储形态。
    if isinstance(data.get("expression_dna"), dict):
        data["expression_dna"] = json.dumps(data["expression_dna"], ensure_ascii=False)
    if isinstance(data.get("enable_thinking"), bool):
        data["enable_thinking"] = int(data["enable_thinking"])
    if isinstance(data.get("proactive_enabled"), bool):
        data["proactive_enabled"] = int(data["proactive_enabled"])
    if isinstance(data.get("mention_sender"), bool):
        data["mention_sender"] = int(data["mention_sender"])
    if not data:
        return get_bot_persona(persona_id)
    assignments = ", ".join(f"{key} = ?" for key in data)
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            f"UPDATE bot_personas SET {assignments}, updated_at = ? WHERE id = ?",
            (*data.values(), _now(), persona_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    persona = get_bot_persona(persona_id)
    if persona:
        revise_persona_snapshots(persona)
    return persona


def revise_persona_snapshots(persona: dict) -> None:
    """把改过的人格重新写回它名下所有会话的快照。

    快照在开会话时冻结，而生成读的就是这份快照：不回填的话「编辑人格」只对新建会话
    生效，正在聊的那段会一直用旧的性格说话。删除人格不调这里 —— 历史要停在被删那一刻。
    """
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE bot_conversations SET persona_snapshot = ? WHERE persona_id = ?",
            (_persona_snapshot_payload(persona), persona["id"]),
        )
        conn.commit()
    finally:
        conn.close()


def copy_bot_persona(persona_id: str) -> Optional[dict]:
    persona = get_bot_persona(persona_id)
    if not persona:
        return None
    return create_bot_persona(
        name=f"{persona['name']} 副本",
        avatar=persona["avatar"],
        personality=persona["personality"],
        background=persona["background"],
        greeting=persona["greeting"],
        model_id=persona["model_id"],
        reply_style=persona.get("reply_style", "moderate"),
        enable_thinking=persona.get("enable_thinking", 0),
        # 复制人格要连「说话方式」一起复制，否则副本一上来就换了个腔调。
        expression_dna=persona.get("expression_dna") or {},
        reaction_style=persona.get("reaction_style") or "warm",
        refusal_style=persona.get("refusal_style") or "soft",
        address_user=persona.get("address_user") or "",
        relationship_speed=persona.get("relationship_speed") or "normal",
        # @ 不 @ 是「说话分寸」不是「开口资格」：与 reply_style 同口径跟着人格走。
        mention_sender=persona.get("mention_sender", 1),
        **{field: persona.get(field) or "" for field in _PERSONA_TEXT_FIELDS},
    )


def delete_bot_persona(persona_id: str) -> bool:
    # 默认人格（is_builtin）不可删除：它同时承担列表置顶与渠道迁移兜底。
    # 路由层先给可读的 409；这里再挡一道，任何调用方都删不掉。
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM bot_personas WHERE id = ? AND is_builtin = 0", (persona_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 机器人渠道：一行 = 一个机器人账号 = 一个人格 ──
# spec 2026-10-28-bot-multi-channel。所有读出口都走 _BOT_CHANNEL_SELECT，
# 里面用 CASE 把 app_secret 折成 has_secret，密文一次都不会进 Python 对象。

_BOT_CHANNEL_SELECT = (
    "SELECT id, provider, display_name, persona_id, app_id, "
    "CASE WHEN app_secret = '' THEN 0 ELSE 1 END AS has_secret, "
    "CASE WHEN extra_secret = '' THEN 0 ELSE 1 END AS has_extra_secret, "
    "enabled, created_at, "
    "(SELECT COUNT(*) FROM bot_conversations c WHERE c.channel_id = bot_channels.id) "
    "AS conversation_count FROM bot_channels"
)


def _bot_channel_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["has_secret"] = bool(data["has_secret"])
    data["has_extra_secret"] = bool(data["has_extra_secret"])
    data["enabled"] = data["enabled"] == "1"
    return data


def create_bot_channel(
    *,
    provider: str,
    display_name: str,
    persona_id: str,
    app_id: str,
    app_secret: str,
    extra_secret: str = "",
) -> dict:
    channel_id = _new_id()
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_channels (id, provider, display_name, persona_id, app_id, "
            "app_secret, extra_secret, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '0', ?)",
            (
                channel_id,
                provider,
                display_name,
                persona_id,
                app_id,
                app_secret or "",
                extra_secret or "",
                _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_bot_channel(channel_id)


def list_bot_channels() -> list[dict]:
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(_BOT_CHANNEL_SELECT + " ORDER BY created_at ASC, id ASC").fetchall()
        return [_bot_channel_dict(row) for row in rows]
    finally:
        conn.close()


def list_enabled_bot_channels() -> list[dict]:
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            _BOT_CHANNEL_SELECT + " WHERE enabled = '1' ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [_bot_channel_dict(row) for row in rows]
    finally:
        conn.close()


def get_bot_channel(channel_id: str) -> Optional[dict]:
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(_BOT_CHANNEL_SELECT + " WHERE id = ?", (channel_id,)).fetchone()
        return _bot_channel_dict(row) if row else None
    finally:
        conn.close()


def get_bot_channel_persona_id(channel_id: str) -> str:
    """入站热路径只问「这个机器人现在是谁」：渠道被改人格后不必重连就能读到新值。

    不走 _BOT_CHANNEL_SELECT —— 那条 SELECT 每条渠道都带一个会话计数子查询，
    而这里每条消息都要问一次。渠道不存在返回空串，由调用侧决定回落。
    """
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT persona_id FROM bot_channels WHERE id = ?", (channel_id,)
        ).fetchone()
        return row["persona_id"] if row else ""
    finally:
        conn.close()


def get_bot_channel_secret(channel_id: str) -> str:
    """读凭据明文，只给运行时拨号用；接口层请一律走 get_bot_channel 的 has_secret。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT app_secret FROM bot_channels WHERE id = ?", (channel_id,)
        ).fetchone()
        return unprotect_secret(row["app_secret"]) if row else ""
    finally:
        conn.close()


def set_bot_channel_secret(channel_id: str, app_secret: str) -> None:
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE bot_channels SET app_secret = ? WHERE id = ?",
            (app_secret or "", channel_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_bot_channel_extra_secret(channel_id: str) -> str:
    """第三个凭据的明文（微信：EncodingAESKey）。只给运行时用，接口层看 has_extra_secret。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT extra_secret FROM bot_channels WHERE id = ?", (channel_id,)
        ).fetchone()
        return unprotect_secret(row["extra_secret"]) if row else ""
    finally:
        conn.close()


def set_bot_channel_extra_secret(channel_id: str, extra_secret: str) -> None:
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE bot_channels SET extra_secret = ? WHERE id = ?",
            (extra_secret or "", channel_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_bot_channel(channel_id: str, **fields) -> Optional[dict]:
    allowed = {"display_name", "persona_id", "app_id", "provider"}
    data = {key: value for key, value in fields.items() if key in allowed}
    if not data:
        return get_bot_channel(channel_id)
    assignments = ", ".join(f"{key} = ?" for key in data)
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            f"UPDATE bot_channels SET {assignments} WHERE id = ?",
            (*data.values(), channel_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_bot_channel(channel_id)


def set_bot_channel_enabled(channel_id: str, enabled: bool) -> None:
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE bot_channels SET enabled = ? WHERE id = ?",
            ("1" if enabled else "0", channel_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_bot_channel(channel_id: str) -> bool:
    """删机器人；它的会话与消息由 FK CASCADE 一并清掉（本地陪聊会话不受影响）。"""
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute("DELETE FROM bot_channels WHERE id = ?", (channel_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_bot_channels(persona_id: Optional[str] = None) -> int:
    conn = _conn_mod._get_conn()
    try:
        if persona_id is None:
            row = conn.execute("SELECT COUNT(*) AS n FROM bot_channels").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM bot_channels WHERE persona_id = ?", (persona_id,)
            ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def _persona_snapshot_payload(persona: dict) -> str:
    """会话快照：人格全量但剔除头像。

    头像 data URL 单条约 10-20KB，逐会话复制会让会话列表涨到 MB 级；
    所以快照只留生成需要的字段，展示用头像在读时按 persona_id 关联实时人格。
    人格被删除时头像为空，前端回退到首字母占位 —— 与气泡渲染一致。
    """
    snapshot = dict(persona)
    snapshot["avatar"] = ""
    return json.dumps(snapshot, ensure_ascii=False)


def _bot_conversation_dict(row: sqlite3.Row) -> dict:
    """内部形态：persona 是生成用的人格全量（快照 + 实时头像补位）。

    路由输出前会裁成 {id, name, avatar}：性格/背景文本只用于拼提示词，
    没必要随每个会话重复下发给前端。
    """
    result = dict(row)
    try:
        snapshot = json.loads(result.pop("persona_snapshot") or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    keys = row.keys()
    live_avatar = result.pop("persona_live_avatar", None) if "persona_live_avatar" in keys else None
    snapshot["avatar"] = live_avatar or snapshot.get("avatar") or ""
    snapshot["id"] = snapshot.get("id") or result.get("persona_id") or ""
    snapshot["name"] = snapshot.get("name") or result.get("title") or ""
    result["persona"] = snapshot
    return result


def create_bot_conversation(
    persona_id: str,
    channel: str = "local",
    external_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> dict:
    persona = get_bot_persona(persona_id)
    if not persona:
        raise ValueError("Persona not found")
    conversation_id = _new_id()
    now = _now()
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_conversations "
            "(id, title, persona_id, persona_snapshot, channel, channel_id, external_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                persona["name"],
                persona_id,
                _persona_snapshot_payload(persona),
                channel,
                channel_id,
                external_id,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    # 本地新会话：开场白作为首条 assistant 消息直接落库（不走 LLM）；
    # QQ 会话（channel='qq'）不在此插入，其 greeting 由回复时首回合前置（见 qq_channel）。
    if channel == "local" and persona.get("greeting"):
        # 跨子域调用（消息域在 db/bot_social.py）：局部导入避免模块循环依赖。
        from db.bot_social import add_bot_message
        add_bot_message(conversation_id, "assistant", persona["greeting"])
    return get_bot_conversation(conversation_id)


def _find_bot_conversation_by_external(
    conn: sqlite3.Connection, channel_id: Optional[str], persona_id: str, external_id: str
):
    """会话身份 = (哪个机器人, 哪个人格, 平台内会话)，与唯一索引同形。

    channel_id/persona_id 用 IS 比较：它们可为 NULL（本地会话、人格被删），
    而 `= NULL` 在 SQL 里永远不成立。
    """
    return conn.execute(
        "SELECT c.*, p.avatar AS persona_live_avatar FROM bot_conversations c "
        "LEFT JOIN bot_personas p ON p.id = c.persona_id "
        "WHERE c.channel_id IS ? AND c.persona_id IS ? AND c.external_id = ?",
        (channel_id, persona_id, external_id),
    ).fetchone()


def get_or_create_bot_conversation(
    persona_id: str,
    channel: str,
    external_id: str,
    channel_id: Optional[str] = None,
) -> dict:
    conn = _conn_mod._get_conn()
    try:
        row = _find_bot_conversation_by_external(conn, channel_id, persona_id, external_id)
    finally:
        conn.close()
    if row:
        return _bot_conversation_dict(row)
    try:
        return create_bot_conversation(persona_id, channel, external_id, channel_id)
    except sqlite3.IntegrityError:
        conn = _conn_mod._get_conn()
        try:
            row = _find_bot_conversation_by_external(conn, channel_id, persona_id, external_id)
            return _bot_conversation_dict(row)
        finally:
            conn.close()


def list_bot_conversations(
    channel: Optional[str] = None,
    persona_id: Optional[str] = None,
    limit: int = 100,
    channel_id: Optional[str] = None,
) -> list[dict]:
    conditions = []
    params: list = []
    if channel is not None:
        conditions.append("c.channel = ?")
        params.append(channel)
    if persona_id is not None:
        conditions.append("c.persona_id = ?")
        params.append(persona_id)
    if channel_id is not None:
        conditions.append("c.channel_id = ?")
        params.append(channel_id)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)
    conn = _conn_mod._get_conn()
    try:
        rows = conn.execute(
            "SELECT c.*, p.avatar AS persona_live_avatar, "
            "(SELECT m.content FROM bot_messages m "
            "WHERE m.conversation_id = c.id ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) "
            "AS preview FROM bot_conversations c "
            "LEFT JOIN bot_personas p ON p.id = c.persona_id "
            f"{where} ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_bot_conversation_dict(row) for row in rows]
    finally:
        conn.close()


def get_bot_conversation(conversation_id: str) -> Optional[dict]:
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT c.*, p.avatar AS persona_live_avatar FROM bot_conversations c "
            "LEFT JOIN bot_personas p ON p.id = c.persona_id WHERE c.id = ?",
            (conversation_id,),
        ).fetchone()
        return _bot_conversation_dict(row) if row else None
    finally:
        conn.close()


def delete_bot_conversation(conversation_id: str) -> bool:
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute("DELETE FROM bot_conversations WHERE id = ?", (conversation_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_bot_memory_injection(conversation_id: str) -> dict:
    """读该会话最近一次记忆注入记录（P2 防复读账本）。无记录返回空结构。"""
    conn = _conn_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT last_memory_ids FROM bot_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["last_memory_ids"]:
        return {"ids": [], "at": None}
    try:
        data = json.loads(row["last_memory_ids"])
        if isinstance(data, dict):
            return {"ids": list(data.get("ids") or []), "at": data.get("at")}
    except (ValueError, TypeError):
        pass
    return {"ids": [], "at": None}


def set_bot_memory_injection(conversation_id: str, memory_ids: list, at: str) -> None:
    """写该会话最近一次注入的 {ids, at}。只保留最近一次：7 天窗口按 at 判断。"""
    payload = json.dumps({"ids": list(memory_ids)[:20], "at": at}, ensure_ascii=False)
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE bot_conversations SET last_memory_ids = ? WHERE id = ?",
            (payload, conversation_id),
        )
        conn.commit()
    finally:
        conn.close()
