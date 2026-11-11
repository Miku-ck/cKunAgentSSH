"""插件（MCP）CRUD — 从 database.py 原样复制的领域模块（阶段 2 试点）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
"""
import json
from typing import Optional

from db._conn import _assert_sql_ident, _get_conn
from db._util import _new_id
from db.experts import _validate_expert_slug

_PLUGINS_PUBLIC_COLS = (
    "id, slug, name, description, command, args_json, cwd, config_rev, "
    "enabled, consented_at, timeout_s, tools_json, readonly_tools_json, "
    "created_at, updated_at"
)
_PLUGIN_CONFIG_FIELDS = frozenset({
    "name", "description", "command", "args_json", "cwd",
    "env_ciphertext", "timeout_s",
})


def list_plugins(include_env: bool = False) -> list[dict]:
    """插件列表。默认不返回 env_ciphertext —— 它不该出现在任何 API 响应里。"""
    cols = _PLUGINS_PUBLIC_COLS + (", env_ciphertext" if include_env else "")
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {cols} FROM plugins ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_plugin(plugin_id: str, include_env: bool = False) -> Optional[dict]:
    cols = _PLUGINS_PUBLIC_COLS + (", env_ciphertext" if include_env else "")
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {cols} FROM plugins WHERE id = ?", (plugin_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_plugin_by_slug(slug: str, include_env: bool = False) -> Optional[dict]:
    cols = _PLUGINS_PUBLIC_COLS + (", env_ciphertext" if include_env else "")
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {cols} FROM plugins WHERE slug = ?", (slug,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_plugin(slug: str, name: str, command: str, **fields) -> dict:
    """安装插件。保存即停用、未确认 —— 未走同意流绝不启动子进程。"""
    unknown = set(fields) - _PLUGIN_CONFIG_FIELDS
    if unknown:
        raise ValueError(f"不支持的插件字段: {sorted(unknown)}")
    slug = _validate_expert_slug(slug)
    if not (command or "").strip():
        raise ValueError("启动程序不能为空")
    values = {
        "id": _new_id(), "slug": slug, "name": name.strip(),
        "command": command.strip(), "args_json": "[]", "timeout_s": 30,
        "config_rev": 1, "enabled": 0,
    }
    values.update(fields)
    cols, vals = list(values), [values[k] for k in values]
    conn = _get_conn()
    try:
        conn.execute(
            f"INSERT INTO plugins ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})", vals,
        )
        conn.commit()
        return get_plugin(values["id"])
    finally:
        conn.close()


def update_plugin_config(plugin_id: str, **fields) -> Optional[dict]:
    """配置变更 = 撤销同意：递增 revision、停用、清空工具快照与只读确认。"""
    unknown = set(fields) - _PLUGIN_CONFIG_FIELDS
    if unknown:
        raise ValueError(f"不支持的插件字段: {sorted(unknown)}")
    sets = []
    vals = []
    for key, value in fields.items():
        _assert_sql_ident(key)
        sets.append(f"{key} = ?")
        vals.append(value)
    sets += [
        "config_rev = config_rev + 1", "enabled = 0", "consented_at = NULL",
        "tools_json = '[]'", "readonly_tools_json = '[]'",
        "updated_at = datetime('now')",
    ]
    vals.append(plugin_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"UPDATE plugins SET {', '.join(sets)} WHERE id = ?", vals,
        )
        conn.commit()
        return get_plugin(plugin_id) if cur.rowcount else None
    finally:
        conn.close()


def consent_plugin(plugin_id: str) -> Optional[dict]:
    """用户确认后启用。确认时刻由服务端写入，客户端无法伪造。"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE plugins SET enabled = 1, consented_at = datetime('now'), "
            "updated_at = datetime('now') WHERE id = ?", (plugin_id,),
        )
        conn.commit()
        return get_plugin(plugin_id) if cur.rowcount else None
    finally:
        conn.close()


def set_plugin_enabled(plugin_id: str, enabled: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE plugins SET enabled = ?, updated_at = datetime('now') "
            "WHERE id = ?", (1 if enabled else 0, plugin_id),
        )
        conn.commit()
        return get_plugin(plugin_id) if cur.rowcount else None
    finally:
        conn.close()


def set_plugin_tools(plugin_id: str, tools: list) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE plugins SET tools_json = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(tools, ensure_ascii=False), plugin_id),
        )
        conn.commit()
        return get_plugin(plugin_id) if cur.rowcount else None
    finally:
        conn.close()


def set_plugin_readonly(plugin_id: str, names: list) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE plugins SET readonly_tools_json = ?, updated_at = datetime('now') "
            "WHERE id = ?", (json.dumps(list(names), ensure_ascii=False), plugin_id),
        )
        conn.commit()
        return get_plugin(plugin_id) if cur.rowcount else None
    finally:
        conn.close()


def delete_plugin(plugin_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
