"""技能 / 技能按模式隔离配置 CRUD — 从 database.py 原样复制的领域模块（P0 阶段 3）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
"""
import json
from typing import Optional

from db._conn import _get_conn
from db._util import _new_id, _now

# skills 全列（四处查询 + 一处 INSERT 共用）。与 _MC_COLS/_SCHEDULE_COLS 同族：
# 各处各抄一份字面量时，加一列只会改到其中几处，其余读法永久拿不到那一列。
_SKILLS_COLS = (
    "id, name, slug, description, category, system_prompt, source, "
    "is_builtin, enabled, priority, trigger_words, created_at, updated_at"
)


def list_skills(limit: int = 200, offset: int = 0) -> list[dict]:
    """获取所有技能列表

    updated_at 只到秒，同批导入/批量启停的技能会整片并列；带 id 兜底键才让
    LIMIT/OFFSET 翻页可重复（否则同一条技能印在两页上、另一条翻不到）。
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_SKILLS_COLS} "
            "FROM skills ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_all_skills() -> list[dict]:
    """获取所有技能（含 disabled），用于注册表缓存"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_SKILLS_COLS} "
            "FROM skills ORDER BY priority DESC, updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_skills_by_source(source: str) -> int:
    """统计指定来源的技能数（审查：替代 list_skills(limit=9999) 全量加载只计数）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM skills WHERE source = ?",
            (source,),
        ).fetchone()
        return int(row["cnt"]) if row else 0
    finally:
        conn.close()


def get_skill(skill_id: str) -> Optional[dict]:
    """按 ID 获取单个技能"""
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_SKILLS_COLS} "
            "FROM skills WHERE id = ?",
            (skill_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_skill_by_slug(slug: str) -> Optional[dict]:
    """按 slug 获取单个技能"""
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_SKILLS_COLS} "
            "FROM skills WHERE slug = ?",
            (slug,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_skill(
    name: str,
    slug: str,
    system_prompt: str,
    description: str = "",
    category: str = "",
    source: str = "manual",
    is_builtin: int = 0,
    enabled: int = 0,
    priority: int = 0,
    trigger_words: Optional[list] = None,
) -> dict:
    """创建技能，slug 冲突时自动追加后缀"""
    conn = _get_conn()
    try:
        original_slug = slug
        suffix = 1
        while conn.execute(
            "SELECT 1 FROM skills WHERE slug = ?", (slug,)
        ).fetchone() is not None:
            slug = f"{original_slug}-{suffix}"
            suffix += 1

        sid = _new_id()
        now = _now()
        # 触发词列表序列化为 JSON 字符串存储，与读取路径（TEXT 列）保持一致
        trigger_words_json = (
            json.dumps(trigger_words, ensure_ascii=False) if trigger_words else "[]"
        )
        conn.execute(
            f"INSERT INTO skills ({_SKILLS_COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, name, slug, description, category, system_prompt, source, is_builtin, enabled, priority, trigger_words_json, now, now),
        )
        conn.commit()
        return {
            "id": sid,
            "name": name,
            "slug": slug,
            "description": description,
            "category": category,
            "system_prompt": system_prompt,
            "source": source,
            "is_builtin": is_builtin,
            "enabled": enabled,
            "priority": priority,
            "trigger_words": trigger_words_json,
            "created_at": now,
            "updated_at": now,
        }
    finally:
        conn.close()


def update_skill_enabled(skill_id: str, enabled: int) -> Optional[dict]:
    """更新技能启用状态"""
    now = _now()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE skills SET enabled = ?, updated_at = ? WHERE id = ?",
            (enabled, now, skill_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return {"id": skill_id, "enabled": enabled, "updated_at": now}
    finally:
        conn.close()


def upsert_builtin_skill(
    name: str,
    slug: str,
    system_prompt: str,
    description: str = "",
    category: str = "",
    trigger_words: Optional[list] = None,
    enabled_on_insert: int = 0,
) -> dict:
    """同步内置技能到数据库（按 slug + is_builtin=1 upsert）

    与 create_skill 的区别：slug 冲突时更新而非追加后缀，
    避免每次同步都新增重复记录。保留用户已设置的 enabled 状态；
    enabled_on_insert 仅控制首次插入的默认启用值（包内运维技能 = 1 开箱可用）。
    """
    conn = _get_conn()
    try:
        now = _now()
        trigger_words_json = (
            json.dumps(trigger_words, ensure_ascii=False) if trigger_words else "[]"
        )
        existing = conn.execute(
            "SELECT id, enabled FROM skills WHERE slug = ? AND is_builtin = 1",
            (slug,),
        ).fetchone()
        if existing:
            # 更新内容字段，保留 enabled（用户可自定义启用状态）
            conn.execute(
                "UPDATE skills SET name = ?, system_prompt = ?, description = ?, "
                "category = ?, trigger_words = ?, updated_at = ? WHERE id = ?",
                (name, system_prompt, description, category, trigger_words_json, now, existing["id"]),
            )
            conn.commit()
            return {"id": existing["id"], "slug": slug, "updated": True}
        else:
            sid = _new_id()
            conn.execute(
                "INSERT INTO skills (id, name, slug, description, category, system_prompt, "
                "source, is_builtin, enabled, priority, trigger_words, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'builtin', 1, ?, 0, ?, ?, ?)",
                (sid, name, slug, description, category, system_prompt, enabled_on_insert, trigger_words_json, now, now),
            )
            conn.commit()
            return {"id": sid, "slug": slug, "updated": False}
    finally:
        conn.close()


def list_builtin_skill_slugs() -> list[str]:
    """返回数据库中所有内置技能的 slug 列表（用于孤儿清理）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT slug FROM skills WHERE is_builtin = 1"
        ).fetchall()
        return [r["slug"] for r in rows]
    finally:
        conn.close()


def delete_builtin_skill_by_slug(slug: str) -> bool:
    """按 slug 删除内置技能（用于孤儿清理：文件系统已删除的技能）"""
    conn = _get_conn()
    try:
        # skill_mode_config 没有指向 skills 的外键，不显式清就会每次删除残留一行
        # （GET /skills 与每次按 mode 过滤都全表扫它）
        conn.execute(
            "DELETE FROM skill_mode_config WHERE skill_id IN "
            "(SELECT id FROM skills WHERE slug = ? AND is_builtin = 1)",
            (slug,),
        )
        cur = conn.execute(
            "DELETE FROM skills WHERE slug = ? AND is_builtin = 1", (slug,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_skill(skill_id: str) -> bool:
    """删除技能（内置技能不可删除，由 is_builtin 控制）"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM skills WHERE id = ? AND is_builtin = 0", (skill_id,))
        if cur.rowcount > 0:
            conn.execute("DELETE FROM skill_mode_config WHERE skill_id = ?", (skill_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_disabled_skills_for_mode(mode: str) -> list[str]:
    """返回该模式下被禁用（enabled=0）的技能 ID 列表"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT skill_id FROM skill_mode_config WHERE mode = ? AND enabled = 0",
            (mode,),
        ).fetchall()
        return [r["skill_id"] for r in rows]
    finally:
        conn.close()


def upsert_skill_mode_config(skill_id: str, mode: str, enabled: int) -> dict:
    """插入或更新某技能在某模式下的启用状态"""
    now = _now()
    conn = _get_conn()
    try:
        cfg_id = _new_id()
        conn.execute(
            "INSERT INTO skill_mode_config (id, skill_id, mode, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(skill_id, mode) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (cfg_id, skill_id, mode, 1 if enabled else 0, now, now),
        )
        conn.commit()
        # ON CONFLICT 命中时保留的是既有行 id，新生成的 cfg_id 并不存在于表中 —
        # 与 upsert_tool_config 同样重查，避免把幽灵 id 返回给调用方
        row = conn.execute(
            "SELECT id FROM skill_mode_config WHERE skill_id = ? AND mode = ?",
            (skill_id, mode),
        ).fetchone()
        return {
            "id": row["id"] if row else cfg_id,
            "skill_id": skill_id,
            "mode": mode,
            "enabled": 1 if enabled else 0,
            "updated_at": now,
        }
    finally:
        conn.close()
