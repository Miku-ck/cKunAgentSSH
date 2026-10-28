"""默认模型读取契约回归 — get_default_model 的列集合必须与 get_model_config 一致。

背景：models_config 的 SELECT 列清单原本在三处各写一份字面量，
get_default_model 那份漏了 reasoning_tokens / sort_order —— 经
schedule_engine._resolve_model 回退、consciousness_tick 等直接消费默认模型
的调用方拿到 None，思考预算退化为「不限制」，且 UI 无从察觉。
本测试锁定「同一行经三个访问器读出同一组字段」，防止再次漏列。
"""


import database as db


def _create(name: str, **kw):
    return db.create_model_config(
        name=name,
        base_url="http://localhost:8000/v1",
        api_key="sk-secret-value-1234",
        model_id=f"{name}-mid",
        reasoning_tokens=kw.pop("reasoning_tokens", 32000),
        max_output_tokens=kw.pop("max_output_tokens", 8192),
        **kw,
    )


def test_default_model_columns_match_row(base_db):
    """同一行的默认模型读取不得比按 id 读取少字段。"""
    created = _create("m-col")
    db.set_default_model(created["id"], mode="ssh")
    by_id = db.get_model_config(created["id"])
    default = db.get_default_model(mode="ssh")

    assert default is not None
    assert set(default) == set(by_id), (
        f"get_default_model 列集合漂移: 缺 {set(by_id) - set(default)} / "
        f"多 {set(default) - set(by_id)}"
    )
    assert default["reasoning_tokens"] == by_id["reasoning_tokens"] == 32000
    assert default["sort_order"] == by_id["sort_order"]
    assert default["api_key"] == by_id["api_key"] == "sk-s****"


def test_global_default_columns_match_row(base_db):
    """mode=None（全局 is_default）分支同样不得漏列——两条 SQL 曾分别手写。"""
    created = _create("m-global")
    db.set_default_model(created["id"], mode=None)
    default = db.get_default_model(mode=None)
    assert default["id"] == created["id"]
    assert set(default) == set(db.get_model_config(created["id"]))
    assert default["reasoning_tokens"] == 32000


def test_set_default_keeps_single_flag_per_mode(base_db):
    """默认标记按列独立：切换 is_default 不得连带改动 is_default_ssh，反之亦然。"""
    a = _create("m-a")
    b = _create("m-b")
    db.set_default_model(a["id"], mode="ssh")
    db.set_default_model(b["id"], mode=None)

    assert db.get_default_model(mode="ssh")["id"] == a["id"]
    assert db.get_default_model(mode=None)["id"] == b["id"]
    conn = db._get_conn()
    try:
        for col, expect in (("is_default", b["id"]), ("is_default_ssh", a["id"])):
            rows = conn.execute(
                f"SELECT id FROM models_config WHERE {col} = 1"
            ).fetchall()
            assert [r["id"] for r in rows] == [expect], f"{col} 应唯一且互不串扰"
    finally:
        conn.close()


def test_list_models_exposes_reasoning_tokens(base_db):
    """列表接口（设置面板数据源）与按 id 读取同源，字段不得缺失。"""
    created = _create("m-list")
    row = next(m for m in db.list_models_config() if m["id"] == created["id"])
    assert row["reasoning_tokens"] == 32000
    assert set(row) == set(db.get_model_config(created["id"]))
