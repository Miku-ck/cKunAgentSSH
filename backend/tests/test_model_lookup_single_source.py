"""S10-22 回归：两种键的解析规则只许存在一处。

`get_model_config` 原先自己抄了一份「先按行主键 id、落空再按 model_id 列回退」，
与 `_resolve_model_pk` 里那份是同一规则的第二个副本。副本的代价不是重复四行 SQL，
而是「读」与「写」此后可以各自演化 —— 重名取舍、匹配列、掩码时机都会分叉，而且分叉
是静默的（两侧都仍然"能查到"，只是查到的不是同一个对象）。
"""
import inspect
import os

import pytest


@pytest.fixture
def wired(tmp_workspace):
    """换库必须成对调 _close_all_connections，否则模块级连接还停在旧库上。"""
    import database as db
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "s1022.db"))
    yield db
    db._close_all_connections()


def test_both_keys_resolve_to_the_same_row(wired):
    db = wired
    created = db.create_model_config(name="A", base_url="https://a.example/v1",
                                     model_id="qwen-a", api_key="sk-a")
    pk = created["id"]
    assert db.get_model_config(pk)["id"] == pk
    assert db.get_model_config("qwen-a")["id"] == pk
    assert db.get_model_config("没有这个键") is None
    assert db.get_model_config("") is None


def test_duplicate_model_names_take_the_earliest_row(wired):
    """model_id 没有唯一约束（面板不查重、create 也不查），取舍口径必须钉住。"""
    db = wired
    first = db.create_model_config(name="First", base_url="https://a/v1",
                                   model_id="dup")["id"]
    db.create_model_config(name="Second", base_url="https://b/v1", model_id="dup")
    assert db.get_model_config("dup")["id"] == first
    assert db._resolve_model_pk("dup") == first


def test_masking_happens_at_the_read_edge(wired):
    db = wired
    pk = db.create_model_config(name="A", base_url="https://a/v1",
                                model_id="qwen-a", api_key="sk-secret")["id"]
    assert "****" in db.get_model_config(pk)["api_key"]
    # 按模型名查同一条行，掩码口径必须一致 —— 这正是两份查询容易走岔的地方
    assert "****" in db.get_model_config("qwen-a")["api_key"]
    assert db.get_model_config(pk, mask_key=False)["api_key"] == "sk-secret"


def test_fallback_rule_is_written_only_once():
    """这条是本次修复真正要防的东西：同一规则的第二份文本副本。"""
    import database as db
    src = inspect.getsource(db)
    assert src.count("FROM models_config WHERE model_id = ? ORDER BY rowid LIMIT 1") == 1
    # 读路径不许再自己拼回退查询 —— 它只能调 _resolve_model_pk
    body = inspect.getsource(db.get_model_config)
    assert "model_id = ?" not in body
    assert "_resolve_model_pk" in body
