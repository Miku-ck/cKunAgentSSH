"""备用模型链 DB 层 — fallback_model_id 列与写侧校验的回归测试。

守护点：
- 列存在（建表 + 旧库幂等补列），get_default_model / get_model_config 返回携带该键；
- 备用引用归一：按模型名传入存成行主键；空串=清除；
- 写侧校验 fail-fast：引用不存在 / 自指 / 环（A→B→A）/ 超深链 → ValueError；
- 删除被引用模型 → 引用方置空（无悬空引用残留）；
- resolve_fallback_model_cfg：一跳解析，密钥/base_url 缺失与自指不启用。

运行: python -m pytest backend/tests/test_model_fallback_chain.py -v
"""
import pytest

import database as db


def _fresh_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(tmp_workspace + "/fallback-chain.sqlite")
    return db


def _mk(name, model_id=None, **kw):
    return db.create_model_config(
        name=name, base_url=kw.pop("base_url", f"http://{name}.invalid/v1"),
        model_id=model_id or name, **kw)


def test_column_present_and_carried_by_reads(tmp_workspace):
    _fresh_db(tmp_workspace)
    cfg = _mk("a", fallback_model_id="")
    assert "fallback_model_id" in cfg
    assert cfg["fallback_model_id"] == ""
    row = db._get_conn().execute(
        "SELECT fallback_model_id FROM models_config WHERE id = ?", (cfg["id"],)).fetchone()
    assert row is not None


def test_fallback_ref_normalized_to_pk_and_clearable(tmp_workspace):
    _fresh_db(tmp_workspace)
    a = _mk("a")
    b = _mk("b", model_id="b-model")
    # 按模型名引用，落库为行主键
    db.update_model_config(a["id"], fallback_model_id="b-model")
    row = db._get_conn().execute(
        "SELECT fallback_model_id FROM models_config WHERE id = ?", (a["id"],)).fetchone()
    assert row["fallback_model_id"] == b["id"]
    # 空串 = 清除
    db.update_model_config(a["id"], fallback_model_id="")
    row = db._get_conn().execute(
        "SELECT fallback_model_id FROM models_config WHERE id = ?", (a["id"],)).fetchone()
    assert row["fallback_model_id"] == ""


def test_missing_ref_rejected(tmp_workspace):
    _fresh_db(tmp_workspace)
    _mk("a")
    with pytest.raises(ValueError, match="备用模型不存在"):
        db.update_model_config("a", fallback_model_id="ghost-model")


def test_self_reference_rejected(tmp_workspace):
    _fresh_db(tmp_workspace)
    _mk("a", model_id="a-model")
    with pytest.raises(ValueError, match="不能指向自身"):
        db.update_model_config("a-model", fallback_model_id="a-model")


def test_cycle_rejected(tmp_workspace):
    _fresh_db(tmp_workspace)
    a = _mk("a")
    b = _mk("b")
    db.update_model_config(b["id"], fallback_model_id=a["id"])
    # A→B→A 构成环
    with pytest.raises(ValueError, match="环"):
        db.update_model_config(a["id"], fallback_model_id=b["id"])
    # 链上间接回指同样被拒：A→B→C→A
    c = _mk("c")
    db.update_model_config(b["id"], fallback_model_id=c["id"])
    db.update_model_config(c["id"], fallback_model_id=a["id"])
    with pytest.raises(ValueError, match="环"):
        db.update_model_config(a["id"], fallback_model_id=b["id"])


def test_delete_clears_inbound_references(tmp_workspace):
    _fresh_db(tmp_workspace)
    a = _mk("a")
    b = _mk("b")
    db.update_model_config(a["id"], fallback_model_id=b["id"])
    assert db.delete_model_config(b["id"]) is True
    row = db._get_conn().execute(
        "SELECT fallback_model_id FROM models_config WHERE id = ?", (a["id"],)).fetchone()
    assert row["fallback_model_id"] == ""


def test_resolve_fallback_one_hop(tmp_workspace):
    _fresh_db(tmp_workspace)
    a = _mk("a")
    b = _mk("b", model_id="b-model", api_key="sk-b")
    db.update_model_config(a["id"], fallback_model_id="b-model")
    fb = db.resolve_fallback_model_cfg(db.get_model_config(a["id"], mask_key=False))
    assert fb is not None and fb["id"] == b["id"]
    assert fb["api_key"] == "sk-b"  # mask_key=False，供 StreamLLMClient 直用
    # 悬空引用 → None
    db.delete_model_config(b["id"])
    assert db.resolve_fallback_model_cfg(db.get_model_config(a["id"], mask_key=False)) is None


def test_resolve_skips_when_key_or_base_url_missing(tmp_workspace):
    _fresh_db(tmp_workspace)
    a = _mk("a")
    _mk("nk", model_id="nk-model", api_key="")
    db.update_model_config(a["id"], fallback_model_id="nk-model")
    assert db.resolve_fallback_model_cfg(db.get_model_config(a["id"], mask_key=False)) is None
    _mk("nu", model_id="nu-model", base_url="")
    db.update_model_config(a["id"], fallback_model_id="nu-model")
    assert db.resolve_fallback_model_cfg(db.get_model_config(a["id"], mask_key=False)) is None


def test_resolve_ignores_unconfigured(tmp_workspace):
    _fresh_db(tmp_workspace)
    a = _mk("a")
    assert db.resolve_fallback_model_cfg(db.get_model_config(a["id"], mask_key=False)) is None
