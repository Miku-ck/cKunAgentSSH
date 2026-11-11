"""DB 迁移守卫：四内置齐全 / general_ops 升级 / 用户行不动 / schedule 回填 / 二次幂等。

证伪：去掉 UPDATE 的 is_builtin=1 条件 → 用户行被改 → 红。
证伪：去掉回填的 not _ddl_applied 门 → 每次重启把 general 任务重置成 general_ops → 红。
"""
import json

import database as db


def _experts():
    return {r["slug"]: r for r in db.list_experts()}


def test_four_builtins_seeded(base_db):
    rows = _experts()
    for slug in ("general", "general_ops", "research", "copywriting"):
        assert slug in rows, slug
        assert rows[slug]["is_builtin"] == 1
        assert rows[slug]["enabled"] == 1


def test_general_ops_builtin_upgraded_to_ops_pack(base_db):
    row = _experts()["general_ops"]
    assert row["name"] == "运维专家"
    assert "智能运维助手" in row["identity_prompt"]
    assert "远程运维方法" in row["identity_prompt"]


def test_domain_packs_persist_explicit_empty_plugins(base_db):
    for slug in ("research", "copywriting"):
        row = _experts()[slug]
        assert json.loads(row["plugin_ids_json"]) == []
        assert row["memory_scope"] == "global"


def test_user_custom_row_untouched_by_migration(base_db):
    """用户行（is_builtin=0）逐字节不动——即使它占着 general_ops 的 slug。

    experts.slug 有 UNIQUE 约束：内置行与用户行不可能同 slug 并存（真实
    「复制专家」走 duplicate 路由，副本必须换新 slug）。为钉死升级 UPDATE 的
    is_builtin=1 守卫，这里构造最坏情形：general_ops 槽位上只有一行、且它是
    用户行——先移除种子内置行（delete_expert 按设计拒删内置行，故走裸 SQL），
    再经真实 CRUD 建用户行。去掉守卫的 UPDATE 会把它改写成运维包 → 红。
    这也覆盖真实旧库场景：种子落库前用户已占用该 slug（INSERT WHERE NOT
    EXISTS 同样按 slug 跳过，不会顶掉用户行）。
    """
    conn = db._get_conn()
    conn.execute("DELETE FROM experts WHERE slug = 'general_ops'")
    conn.commit()
    created = db.create_expert("general_ops", "我的运维", identity_prompt="自定义身份")
    # create_expert 默认 is_builtin=0
    before = db.get_expert(created["id"])
    assert before["is_builtin"] == 0
    db.init_db(base_db)  # 二次 init 触发升级 UPDATE
    after = db.get_expert(created["id"])
    assert after["identity_prompt"] == "自定义身份"
    assert after["name"] == "我的运维"
    assert after["revision"] == before["revision"]


def test_second_init_is_idempotent(base_db):
    db.init_db(base_db)
    snapshot = {s: dict(r) for s, r in _experts().items()}
    db.init_db(base_db)
    again = {s: dict(r) for s, r in _experts().items()}
    for slug in snapshot:
        for key in ("name", "identity_prompt", "revision", "tool_names_json", "plugin_ids_json"):
            assert snapshot[slug][key] == again[slug][key], (slug, key)


def test_schedule_expert_slug_backfill_and_default(base_db):
    # 存量行（迁移前创建）回填 general_ops
    old = db.create_schedule({"name": "旧巡检", "task_prompt": "看下磁盘"})
    db.init_db(base_db)  # 触发回填（仅 _ddl_applied=False 时）
    # base_db 已 init 过一次（version=10），此处再 init 是 _ddl_applied=True，
    # 回填不重跑——旧行必须保持新建时的 general（断言钉死一次性门：若回填
    # 每次 init 都跑，这里会被重置成 general_ops → 红）。存量回填的真实路径
    # 由 test_legacy_schedule_backfill 覆盖。
    assert db.get_schedule(old["id"])["expert_slug"] == "general"
    new = db.create_schedule({"name": "新任务", "task_prompt": "写周报"})
    assert db.get_schedule(new["id"])["expert_slug"] == "general"


def test_legacy_schedule_backfill(tmp_workspace):
    """模拟旧库：先建无 expert_slug 列的 schedules 行，再升级 → 回填 general_ops。

    按 brief 注记简化：只降版不删列（SQLite 删列需重建表）。回填判据是
    _ddl_applied=False（版本不命中），与列是否已存在无关；增量补列本身
    由 PRAGMA table_info 守卫，列在则跳过。
    """
    import os
    import sqlite3
    db_path = os.path.join(tmp_workspace, "legacy.db")
    db._close_all_connections()
    db.init_db(db_path)
    sid = db.create_schedule({"name": "存量", "task_prompt": "巡检"})["id"]
    # 人为降版，模拟旧库结构
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    db._close_all_connections()
    db.init_db(db_path)  # version 9 → 10，_ddl_applied=False → 回填
    assert db.get_schedule(sid)["expert_slug"] == "general_ops"
    db._close_all_connections()