"""模型主键解析 + 全局默认模型可设（round 22）。

登记在册的前置（round 14）：读取路径认两种键（行主键 id 或 LLM 模型名），写路径
只认行主键。于是 PATCH /settings/models/default?model_id=<模型名>：存在性检查过 →
第一条 UPDATE 把真正的默认清掉 → 第二条匹配 0 行 → 0 个默认模型且响应 200。
本轮补「全局默认」UI 之前必须先收敛这个口径，否则新控件多一条传名字的路径。
"""
import os

import pytest


@pytest.fixture
def base_db(tmp_workspace):
    import database as db
    db_path = os.path.join(tmp_workspace, "modelpk22.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


@pytest.fixture
def client(base_db):
    import fastapi
    from fastapi.testclient import TestClient
    from routes.settings import router as settings_router
    app = fastapi.FastAPI()
    app.include_router(settings_router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def two_models(base_db):
    """一真一假两个模型：行主键与模型名列不同名，才能区分"按 id 命中"与"按名字命中"。"""
    import database as db
    a = db.create_model_config(name="A", base_url="https://a.example/v1",
                               model_id="qwen-a", api_key="sk-aaa")
    b = db.create_model_config(name="B", base_url="https://b.example/v1",
                               model_id="qwen-b", api_key="sk-bbb")
    return db, a["id"], b["id"]


def _defaults(db):
    rows = db.list_models_config()
    return {r["id"]: (r["is_default"], r["is_default_ssh"]) for r in rows}


# ── 写路径与读路径同口径 ────────────────────────────────────────────────────

def test_set_default_by_model_name_does_not_wipe_defaults(two_models):
    """旧行为：按模型名设 SSH 默认 → 真默认被清、目标没置上 → 0 个默认还返回成功。"""
    db, a_id, b_id = two_models
    db.set_default_model(a_id, mode="ssh")
    db.set_default_model("qwen-b", mode="ssh")
    state = _defaults(db)
    assert state[b_id] == (0, 1), "按模型名应当命中 B 这一行"
    assert state[a_id] == (0, 0), "A 的 SSH 默认应被 B 顶替"
    assert db.get_default_model(mode="ssh")["id"] == b_id


def test_set_default_by_row_id_still_works(two_models):
    db, a_id, b_id = two_models
    db.set_default_model(a_id, mode="global")
    assert db.get_default_model(mode=None)["id"] == a_id
    assert _defaults(db)[a_id] == (1, 0)


def test_set_default_unknown_key_raises(two_models):
    """静默成功是这条缺陷最难查的一半：查不到就抛，由路由层转 404。"""
    db, _, _ = two_models
    with pytest.raises(ValueError):
        db.set_default_model("no-such-model", mode="ssh")


def test_update_model_config_accepts_model_name(two_models):
    """PATCH /settings/models/{模型名} 此前同形：存在性检查过、UPDATE 匹配 0 行，
    返回 200 但什么都没改。"""
    db, a_id, _ = two_models
    updated = db.update_model_config("qwen-a", temperature=0.15)
    assert updated is not None and updated["id"] == a_id
    assert db.get_model_config(a_id)["temperature"] == 0.15


def test_delete_model_by_name_still_protects_default(two_models):
    """默认不可删的保护此前只认行主键：按名字传进来会绕过检查直接删掉默认模型。"""
    db, a_id, _ = two_models
    db.set_default_model(a_id, mode="ssh")
    with pytest.raises(ValueError):
        db.delete_model_config("qwen-a")
    assert db.get_model_config(a_id) is not None


# ── 全局默认可设（C9）───────────────────────────────────────────────────────

def test_patch_default_mode_global_sets_is_default(client, two_models):
    _, a_id, _ = two_models
    r = client.patch("/api/settings/models/default",
                     params={"model_id": a_id, "mode": "global"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "global" and body["default"] == a_id
    # 全局档与 SSH 档互不影响：只设全局不该顺带动 SSH 默认
    state = _defaults(two_models[0])
    assert state[a_id] == (1, 0)


def test_patch_default_mode_ssh_is_unchanged_shape(client, two_models):
    """既有调用（不传 mode）必须继续是 SSH 档：前端"设为默认"与 round18 的 A1 依赖它。"""
    _, a_id, _ = two_models
    r = client.patch("/api/settings/models/default", params={"model_id": a_id})
    assert r.status_code == 200, r.text
    assert r.json() == {"default": a_id, "mode": "ssh"}
    assert _defaults(two_models[0])[a_id] == (0, 1)


def test_patch_default_rejects_unknown_mode(client, two_models):
    _, a_id, _ = two_models
    r = client.patch("/api/settings/models/default",
                     params={"model_id": a_id, "mode": "nonsense"})
    assert r.status_code == 422


def test_patch_default_unknown_model_returns_404(client, two_models):
    r = client.patch("/api/settings/models/default",
                     params={"model_id": "ghost", "mode": "global"})
    assert r.status_code == 404


def test_models_list_exposes_both_flags_for_panel(client, two_models):
    """面板的两枚徽标只靠 GET /settings/models 的每行两个旗标派生，不需要额外端点；
    前提是该列表把两列都带回来且 api_key 保持脱敏。"""
    _, a_id, b_id = two_models
    client.patch("/api/settings/models/default",
                 params={"model_id": a_id, "mode": "global"})
    client.patch("/api/settings/models/default",
                 params={"model_id": b_id, "mode": "ssh"})
    rows = client.get("/api/settings/models").json()
    by_id = {r["id"]: r for r in rows}
    assert by_id[a_id]["is_default"] == 1 and by_id[a_id]["is_default_ssh"] == 0
    assert by_id[b_id]["is_default"] == 0 and by_id[b_id]["is_default_ssh"] == 1
    text = client.get("/api/settings/models").text
    # 密钥永不回明文。口径是「保留前 4 位 + ****」，所以断完整密钥字面量不出现，
    # 而不是断前缀（sk-a**** 是合法形态）。
    for full_key in ("sk-aaa", "sk-bbb"):
        assert full_key not in text


# ── 读路径的档位顺序 ────────────────────────────────────────────────────────

def _get_default(client):
    r = client.get("/api/settings/models/default")
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_get_default_without_ssh_flag_uses_global_row(client, two_models):
    """只标了全局档（⊕）时，运行时（routes/agent.py、terminal、schedule_engine）
    读的就是那一行；端点先前缺这一级，直接落到 models[0] —— 前端按它预选模型，
    于是「面板上 ★ 亮在 A」与「实际跑的是列表第一行 B」同时成立。"""
    db, a_id, b_id = two_models
    db.set_default_model(b_id, mode="global")
    assert _get_default(client) == b_id
    assert _get_default(client) != a_id


def test_get_default_prefers_ssh_over_global(client, two_models):
    _, a_id, b_id = two_models
    client.patch("/api/settings/models/default", params={"model_id": a_id, "mode": "global"})
    client.patch("/api/settings/models/default", params={"model_id": b_id, "mode": "ssh"})
    assert _get_default(client) == b_id


def test_get_default_falls_back_to_first_row_when_nothing_marked(client, two_models):
    """一个默认都没标时的兜底留给前端选出一个能跑的模型，别把它改成 404。"""
    first = client.get("/api/settings/models").json()[0]["id"]
    assert _get_default(client) == first
