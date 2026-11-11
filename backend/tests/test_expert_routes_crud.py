"""专家路由契约守卫：内置保护 / slug 唯一 / 枚举与工具名校验 /
None(继承) 与 [](空集) 的持久化区分 / 复制语义 / revision 递增。"""
import json

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
import routes.experts as ex_mod
from agent.expert_policy import DEFAULT_EXPERT_SLUG


@pytest.fixture
def client(base_db, monkeypatch):
    # 工具清单是进程级缓存，测试里固定为少量真实名 + 一个 MCP 命名空间名，
    # 避免用例依赖 35 个工具类的实例化副作用。
    monkeypatch.setattr(ex_mod, "_TOOL_NAME_CACHE", {"shell_exec", "file_read"})
    app = fastapi.FastAPI()
    app.include_router(ex_mod.router, prefix="/api")
    return TestClient(app)


def _create(client, slug="db-ops", **overrides):
    body = {"slug": slug, "name": "数据库专家"}
    body.update(overrides)
    return client.post("/api/experts", json=body)


def _builtin(client):
    rows = client.get("/api/experts").json()
    return next(r for r in rows if r["slug"] == DEFAULT_EXPERT_SLUG)


def test_builtin_general_ops_is_seeded_on_init(client):
    rows = client.get("/api/experts").json()
    row = next(r for r in rows if r["slug"] == "general_ops")
    assert row["is_builtin"] == 1
    assert row["enabled"] == 1
    assert row["tool_names_json"] is None       # 继承，不是空集
    assert row["plugin_ids_json"] is None       # 内置继承：允许加载已启用插件
    assert row["permission_ceiling"] == "inherit"
    assert row["memory_scope"] == "inherit"
    assert "智能运维助手" in row["identity_prompt"]  # 升级为运维包


def test_all_four_builtins_seeded(client):
    rows = client.get("/api/experts").json()
    slugs = {r["slug"] for r in rows if r["is_builtin"] == 1}
    assert {"general", "general_ops", "research", "copywriting"} <= slugs


def test_create_defaults_to_inherit_everything(client):
    resp = _create(client)
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["slug"] == "db-ops"
    assert row["tool_names_json"] is None
    assert row["skill_ids_json"] is None
    assert json.loads(row["plugin_ids_json"]) == []
    assert row["identity_prompt"] == ""
    assert row["revision"] == 1
    assert row["is_builtin"] == 0


def test_none_and_empty_list_persist_differently(client):
    inherited = _create(client, slug="inherit-all").json()
    empty = _create(client, slug="no-tools", tool_names=[], skill_ids=[]).json()
    assert inherited["tool_names_json"] is None
    assert empty["tool_names_json"] == "[]"
    assert empty["skill_ids_json"] == "[]"


@pytest.mark.parametrize("slug", ["", "Bad Slug", "a/b", "中文", "x" * 65, "-lead"])
def test_invalid_slug_rejected(client, slug):
    assert _create(client, slug=slug).status_code in (400, 422)


def test_duplicate_slug_returns_409(client):
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409


def test_invalid_enums_return_400(client):
    assert _create(client, slug="bad-ceiling", permission_ceiling="yolo").status_code == 400
    assert _create(client, slug="bad-scope", memory_scope="persona:1").status_code == 400
    assert _create(client, slug="bad-iter", max_iterations=0).status_code == 422


def test_unknown_tool_name_rejected_but_mcp_namespace_allowed(client):
    assert _create(client, slug="bad-tool", tool_names=["not_a_tool"]).status_code == 400
    ok = _create(client, slug="good-tools",
                 tool_names=["shell_exec", "mcp__demo__lookup"])
    assert ok.status_code == 201, ok.text
    assert json.loads(ok.json()["tool_names_json"]) == ["shell_exec", "mcp__demo__lookup"]


def test_update_increments_revision(client):
    created = _create(client).json()
    first = client.put(f"/api/experts/{created['id']}",
                       json={"identity_prompt": "你是 DBA"}).json()
    assert first["revision"] == created["revision"] + 1
    assert first["identity_prompt"] == "你是 DBA"
    second = client.put(f"/api/experts/{created['id']}",
                        json={"permission_ceiling": "plan"}).json()
    assert second["revision"] == created["revision"] + 2
    assert second["identity_prompt"] == "你是 DBA"      # 部分更新不丢字段


def test_builtin_cannot_be_edited_disabled_or_deleted(client):
    row = _builtin(client)
    eid = row["id"]
    assert client.put(f"/api/experts/{eid}", json={"name": "改名"}).status_code == 400
    assert client.patch(f"/api/experts/{eid}/enabled", json={"enabled": False}).status_code == 400
    assert client.delete(f"/api/experts/{eid}").status_code == 400
    assert db.get_expert(eid)["name"] == row["name"]


def test_duplicate_builtin_keeps_collection_semantics(client):
    source = _builtin(client)
    resp = client.post(f"/api/experts/{source['id']}/duplicate",
                       json={"slug": "ops-copy", "name": "运维副本"})
    assert resp.status_code == 201, resp.text
    copy = resp.json()
    assert copy["is_builtin"] == 0
    assert copy["slug"] == "ops-copy"
    assert copy["tool_names_json"] is None          # 继承语义原样复制
    assert copy["plugin_ids_json"] is None
    assert source["id"] != copy["id"]


def test_duplicate_empty_collection_stays_empty(client):
    source = _create(client, slug="locked", tool_names=[], plugin_ids=[]).json()
    copy = client.post(f"/api/experts/{source['id']}/duplicate",
                       json={"slug": "locked-copy"}).json()
    assert copy["tool_names_json"] == "[]"


def test_toggle_and_delete_custom_expert(client):
    row = _create(client).json()
    eid = row["id"]
    assert client.patch(f"/api/experts/{eid}/enabled", json={"enabled": False}).json()["enabled"] == 0
    assert client.patch(f"/api/experts/{eid}/enabled", json={"enabled": True}).json()["enabled"] == 1
    assert client.delete(f"/api/experts/{eid}").json() == {"deleted": True}
    assert client.get(f"/api/experts/{eid}").status_code == 404


def test_unknown_expert_returns_404(client):
    assert client.get("/api/experts/not-exist").status_code == 404
    assert client.put("/api/experts/not-exist", json={"name": "x"}).status_code == 404
