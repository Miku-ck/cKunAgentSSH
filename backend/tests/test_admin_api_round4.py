"""
管理端点测试 — 手动授权 / Tavily 密钥设置 / 包内技能同步

覆盖 2026-09「完善功能」轮新增：
- POST /agent/permission/allow（allowlist 手动登记，命令级绑定键）
- GET/PUT /settings/web-tools（Tavily 密钥：env 优先、DB 回退、掩码回显）
- POST /skills/sync-packaged + builtin_skills frontmatter 解析
- routes/skills sync-builtin 孤儿清理对包内 slug 的豁免
"""
import os

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db


@pytest.fixture
def base_db(tmp_workspace):
    db_path = os.path.join(tmp_workspace, "admin_api.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


def _make_client(*routers) -> TestClient:
    app = fastapi.FastAPI()
    for r in routers:
        app.include_router(r, prefix="/api")
    return TestClient(app)


# ── 手动授权 ────────────────────────────────────────────────────────────────


@pytest.fixture
def perm_client(base_db, tmp_workspace, monkeypatch):
    import agent.harness.reins.gate as reins
    monkeypatch.setattr(reins, "ALLOWLIST_PATH", os.path.join(tmp_workspace, "allowlist.txt"))
    monkeypatch.setattr(reins, "_global_permission_gate", None)
    import routes.agent as agent_mod
    return _make_client(agent_mod.router)


def test_allow_tool_level_grant(perm_client):
    resp = perm_client.post("/api/agent/permission/allow", json={"tool": "view_file"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["granted"] is True
    assert body["grant_key"] == "view_file"
    listed = perm_client.get("/api/agent/permission/list").json()["tools"]
    assert "view_file" in listed
    # 落盘
    import agent.harness.reins.gate as reins
    with open(reins.ALLOWLIST_PATH, encoding="utf-8") as f:
        assert "view_file" in f.read()


def test_allow_command_scoped_grant_and_revoke(perm_client):
    resp = perm_client.post(
        "/api/agent/permission/allow",
        json={"tool": "shell_exec", "command": "systemctl status nginx"},
    )
    assert resp.status_code == 200
    key = resp.json()["grant_key"]
    assert key.startswith("shell_exec:") and len(key.split(":")[1]) == 16

    # 单条命令级撤销：只撤这一条，工具级键不受影响
    perm_client.post("/api/agent/permission/allow", json={"tool": "shell_exec"})
    resp = perm_client.post(
        "/api/agent/permission/revoke",
        params={"tool": "shell_exec", "command": "systemctl status nginx"},
    )
    assert resp.status_code == 200
    listed = perm_client.get("/api/agent/permission/list").json()["tools"]
    assert key not in listed
    assert "shell_exec" in listed  # 工具级键保留


def test_allow_rejects_empty_tool(perm_client):
    resp = perm_client.post("/api/agent/permission/allow", json={"tool": ""})
    assert resp.status_code == 422


# ── Tavily 密钥 ─────────────────────────────────────────────────────────────


@pytest.fixture
def settings_client(base_db, monkeypatch):
    # 隔离 env：避免本机真实 TAVILY_API_KEY 干扰
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    import routes.settings as settings_mod
    return _make_client(settings_mod.router)


def test_web_tools_key_save_mask_and_clear(settings_client, monkeypatch):
    import routes.settings as settings_mod
    store: dict[str, str] = {}
    monkeypatch.setattr(settings_mod.db, "get_app_setting", lambda k: store.get(k))
    monkeypatch.setattr(settings_mod.db, "set_app_setting", lambda k, v: store.__setitem__(k, v))

    body = settings_client.get("/api/settings/web-tools").json()
    assert body["tavily_api_key_set"] is False

    resp = settings_client.put("/api/settings/web-tools", json={"tavily_api_key": "  tvly-abcdef0123456789  "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tavily_api_key_set"] is True
    assert body["tavily_api_key_source"] == "db"
    assert body["tavily_api_key_masked"].startswith("tvly")
    assert "abcdef" not in body["tavily_api_key_masked"]  # 中段不回显（只露首尾各 4 位）
    assert store["web.tavily_api_key"] == "tvly-abcdef0123456789"  # 已 strip

    # 清除
    body = settings_client.put("/api/settings/web-tools", json={"tavily_api_key": ""}).json()
    assert body["tavily_api_key_set"] is False


def test_web_tools_env_priority(settings_client, monkeypatch):
    import routes.settings as settings_mod
    monkeypatch.setattr(settings_mod.db, "get_app_setting", lambda k: "db-key-123456")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-envkey987654")
    body = settings_client.get("/api/settings/web-tools").json()
    assert body["tavily_api_key_source"] == "env"
    assert body["tavily_api_key_masked"].startswith("tvly")


def test_web_tools_key_length_capped(settings_client, monkeypatch):
    import routes.settings as settings_mod
    monkeypatch.setattr(settings_mod.db, "set_app_setting", lambda k, v: None)
    resp = settings_client.put("/api/settings/web-tools", json={"tavily_api_key": "x" * 257})
    assert resp.status_code == 422


def test_memory_settings_budget_bounds_and_dead_strategy(settings_client, monkeypatch):
    """v2：strategy 是死配置（沉淀策略由写入管线决定），预算/超时按 Field 区间挡脏值"""
    import routes.settings as settings_mod
    store: dict[str, str] = {}
    monkeypatch.setattr(settings_mod.db, "get_app_setting",
                        lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_mod.db, "set_app_setting",
                        lambda k, v: store.__setitem__(k, v))

    resp = settings_client.put("/api/settings/memory", json={"inject_budget_tokens": 99999})
    assert resp.status_code == 422
    resp = settings_client.put("/api/settings/memory", json={"search_timeout_s": 0})
    assert resp.status_code == 422

    resp = settings_client.put("/api/settings/memory", json={
        "enabled": True, "max_items": 50, "inject_budget_tokens": 900,
        "blocks_budget_tokens": 800, "search_timeout_s": 3.5,
        "reconcile_llm_enabled": False,
    })
    assert resp.status_code == 200
    assert store["memory.max_items"] == "50"
    assert store["memory.inject_budget_tokens"] == "900"
    assert store["memory.blocks_budget_tokens"] == "800"
    assert store["memory.search_timeout_s"] == "3.5"
    assert store["memory.reconcile_llm_enabled"] == "false"
    assert "memory.strategy" not in store
    assert "strategy" not in resp.json()


def test_web_tools_search_fallback(web_tools_key_resolution_env):
    """web_search._get_tavily_key：env 优先，env 缺失回退 DB"""
    tool_cls, patcher = web_tools_key_resolution_env
    t = tool_cls()
    assert t._get_tavily_key() == "env-key"
    patcher.delenv("TAVILY_API_KEY")
    assert t._get_tavily_key() == "db-key"


@pytest.fixture
def web_tools_key_resolution_env(monkeypatch):
    import database as dbmod
    from agent.tools.builtin.web_tools import WebSearchTool
    monkeypatch.setattr(dbmod, "get_app_setting", lambda k: "db-key" if k == "web.tavily_api_key" else None)

    class EnvPatcher:
        def __init__(self, mp):
            self.mp = mp
        def delenv(self, name):
            self.mp.delenv(name, raising=False)

    monkeypatch.setenv("TAVILY_API_KEY", "env-key")
    return WebSearchTool, EnvPatcher(monkeypatch)


# ── 包内技能 ────────────────────────────────────────────────────────────────


def test_scan_packaged_skills_content():
    """skills_builtin/ 目录内容与 frontmatter 解析器契约"""
    import builtin_skills

    skills = builtin_skills.scan_builtin_skills()
    assert len(skills) >= 3, "包内运维技能至少 3 条"
    by_slug = {s["slug"]: s for s in skills}
    assert {"linux-ops-triage", "ops-change-safety", "nginx-service-diagnose"} <= set(by_slug)
    for slug, s in by_slug.items():
        assert s["name"], slug
        assert len(s["system_prompt"]) > 200, f"{slug} 正文过短"
        assert s["trigger_words"], f"{slug} 缺触发词"
        # 正文不含 frontmatter 残留
        assert not s["system_prompt"].startswith("---")


def test_parse_frontmatter_variants():
    import builtin_skills

    text = (
        "---\n"
        'name: "quoted-name"\n'
        "slug: q\n"
        "description: 带冒号: 描述\n"
        "trigger_words: [alpha, 'beta gamma', \"delta\"]\n"
        "---\n"
        "正文内容\n"
    )
    meta, body = builtin_skills._parse_frontmatter(text)
    assert meta["name"] == "quoted-name"
    assert meta["trigger_words"] == ["alpha", "beta gamma", "delta"]
    assert meta["description"] == "带冒号: 描述"
    assert body == "正文内容"

    meta2, body2 = builtin_skills._parse_frontmatter("没有 frontmatter 的文本")
    assert meta2 == {}
    assert body2 == "没有 frontmatter 的文本"


def test_sync_packaged_skills_to_db(base_db, tmp_workspace, monkeypatch):
    import builtin_skills
    monkeypatch.setattr(
        builtin_skills, "_STATE_FILE",
        __import__("pathlib").Path(tmp_workspace) / "state.json",
    )
    result = builtin_skills.sync_packaged_skills()
    assert result["upserted"] >= 3
    slugs = set(result["slugs"])

    rows = {s["slug"]: s for s in db.list_skills(limit=500)}
    for slug in slugs:
        assert slug in rows, f"{slug} 未入库"
        assert rows[slug]["is_builtin"] == 1
        assert rows[slug]["enabled"] == 1, "包内技能首次插入应默认启用"
        assert len(rows[slug]["system_prompt"]) > 200

    # 幂等：再同步一次不产生重复行、保留用户停用的 enabled 状态
    one = next(s for s in db.list_skills(limit=500) if s["slug"] in slugs)
    db.update_skill_enabled(one["id"], 0)
    builtin_skills.sync_packaged_skills()
    again = db.get_skill(one["id"])
    assert again["enabled"] == 0, "同步不应覆盖用户的启用选择"
    assert len([s for s in db.list_skills(limit=500) if s["slug"] in slugs]) == len(slugs)


def test_sync_builtin_orphan_cleanup_exempts_packaged(base_db, tmp_workspace, monkeypatch):
    """前端 ~/.agents/skills 同步不得误删包内技能（孤儿清理豁免）"""
    import builtin_skills
    monkeypatch.setattr(
        builtin_skills, "_STATE_FILE",
        __import__("pathlib").Path(tmp_workspace) / "state.json",
    )
    builtin_skills.sync_packaged_skills()

    import routes.skills as skills_mod
    client = _make_client(skills_mod.router)
    resp = client.post("/api/skills/sync-builtin", json={"skills": [
        {"name": "user-dir-skill", "slug": "user-dir-skill", "system_prompt": "x" * 10, "description": "", "category": "", "trigger_words": []},
    ]})
    assert resp.status_code == 200
    assert resp.json()["orphans_removed"] == 0, "包内技能不应被前端同步当孤儿删除"
    slugs = {s["slug"] for s in db.list_skills(limit=500)}
    assert "linux-ops-triage" in slugs
    assert "user-dir-skill" in slugs
