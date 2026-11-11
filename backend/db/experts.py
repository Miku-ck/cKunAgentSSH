"""专家（Expert）CRUD — 从 database.py 原样复制的领域模块（阶段 2 试点）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
"""
from typing import Optional

from db._conn import _assert_sql_ident, _get_conn
from db._util import _new_id

_EXPERTS_COLS = (
    "id, slug, name, description, identity_prompt, model_id, tool_names_json, "
    "plugin_ids_json, skill_ids_json, permission_ceiling, max_iterations, "
    "memory_scope, enabled, is_builtin, revision, created_at, updated_at"
)

# 只允许这些字段被写入。slug 不可改（归因与插件命名都引用它）；
# enabled 走 set_expert_enabled（内置专家禁止禁用）；revision 只能由更新自增。
_EXPERT_WRITABLE_FIELDS = frozenset({
    "name", "description", "identity_prompt", "model_id", "tool_names_json",
    "plugin_ids_json", "skill_ids_json", "permission_ceiling", "max_iterations",
    "memory_scope",
})


def _validate_expert_slug(slug: str) -> str:
    """slug 会进入 URL 与插件工具命名空间，只允许 ASCII 小写字母/数字/-/_。"""
    slug = (slug or "").strip().lower()
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if not slug or len(slug) > 64 or not set(slug) <= allowed or not slug[0].isalnum():
        raise ValueError("slug 须为 1-64 位小写字母/数字/-/_，且以字母或数字开头")
    return slug


def list_experts(include_disabled: bool = True) -> list[dict]:
    """专家列表：内置在前，其余按最近更新排序（id 兜底保证翻页可重复）。"""
    conn = _get_conn()
    try:
        sql = f"SELECT {_EXPERTS_COLS} FROM experts"
        params: tuple = ()
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY is_builtin DESC, updated_at DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_expert(expert_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_EXPERTS_COLS} FROM experts WHERE id = ?", (expert_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_expert_by_slug(slug: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_EXPERTS_COLS} FROM experts WHERE slug = ?", (slug,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_expert(slug: str, name: str, **fields) -> dict:
    """新建专家。默认全继承 + 无插件，与 general_ops 同一安全起点。"""
    unknown = set(fields) - _EXPERT_WRITABLE_FIELDS
    if unknown:
        raise ValueError(f"不支持的专家字段: {sorted(unknown)}")
    slug = _validate_expert_slug(slug)
    if not (name or "").strip():
        raise ValueError("专家名称不能为空")
    expert_id = _new_id()
    # 默认值与显式字段合并成一份 dict：复制专家会把源行的 plugin_ids_json
    # （可能是 NULL）传进来，若仍用「默认列 + 追加列」两串拼接会出现重复列名。
    values = {
        "id": expert_id, "slug": slug, "name": name.strip(),
        "identity_prompt": "", "plugin_ids_json": "[]",
        "permission_ceiling": "inherit", "memory_scope": "inherit",
        "enabled": 1, "is_builtin": 0, "revision": 1,
    }
    values.update(fields)
    cols, vals = list(values), [values[k] for k in values]
    conn = _get_conn()
    try:
        conn.execute(
            f"INSERT INTO experts ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            vals,
        )
        conn.commit()
    finally:
        conn.close()
    return get_expert(expert_id)


def update_expert(expert_id: str, **fields) -> Optional[dict]:
    """更新专家配置。任何变更都 revision+1 —— 快照靠它区分同名专家的不同代际。"""
    unknown = set(fields) - _EXPERT_WRITABLE_FIELDS
    if unknown:
        raise ValueError(f"不支持的专家字段: {sorted(unknown)}")
    if not fields:
        return get_expert(expert_id)
    sets, vals = [], []
    for key, value in fields.items():
        _assert_sql_ident(key)
        sets.append(f"{key} = ?")
        vals.append(value)
    sets.append("revision = revision + 1")
    sets.append("updated_at = datetime('now')")
    vals.append(expert_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"UPDATE experts SET {', '.join(sets)} WHERE id = ?", vals,
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_expert(expert_id)


def set_expert_enabled(expert_id: str, enabled: int) -> Optional[dict]:
    """启停专家。内置默认专家不可禁用 —— 它是未选专家时的系统兜底。"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE experts SET enabled = ?, updated_at = datetime('now') "
            "WHERE id = ? AND is_builtin = 0",
            (1 if enabled else 0, expert_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_expert(expert_id)


def delete_expert(expert_id: str) -> bool:
    """删除专家。内置保护同时落在 SQL 层，路由漏判也删不掉默认专家。"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM experts WHERE id = ? AND is_builtin = 0", (expert_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
