"""设置字段补齐测试（C2）—— 之前只有前端消费、后端无专项用例的六组设置。

覆盖 conv_retention / post_process.* / web.tavily_api_key / 工具启用 / 策略启用 /
技能 per-mode 开关。每组的关注点相同：写入 → 读取回显 → 实际生效值，三段必须一致。
读侧的默认值口径要与前端 settings.ts 的默认对齐，否则"没改过"会被显示成"改过"。
"""
import os

import pytest


# ── 夹具 ────────────────────────────────────────────────────────────────────

@pytest.fixture
def base_db(tmp_workspace):
    import database as db
    db_path = os.path.join(tmp_workspace, "fields20.db")
    # 开发机上的真实密钥会改变 source 判定，用例之间就靠它互相污染了
    saved_env = os.environ.pop("TAVILY_API_KEY", None)
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()
    os.environ.pop("TAVILY_API_KEY", None)
    if saved_env is not None:
        os.environ["TAVILY_API_KEY"] = saved_env


@pytest.fixture
def client(base_db):
    import fastapi
    from fastapi.testclient import TestClient
    from routes.settings import router as settings_router
    from routes.agent import router as agent_router
    from routes.skills import router as skills_router
    app = fastapi.FastAPI()
    app.include_router(settings_router, prefix="/api")
    app.include_router(agent_router, prefix="/api")
    app.include_router(skills_router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def clean_pp_config():
    """PUT /settings/post-process 会就地替换全局 cfg，用例跑完必须还原"""
    import post_processor as pp
    saved = pp.get_post_process_config()
    yield pp
    pp.set_post_process_config(saved)


# ── 会话保留清理 ─────────────────────────────────────────────────────────────

def test_conv_retention_default_is_off(client, base_db):
    # 默认关闭：新装就自动删会话是不可接受的
    body = client.get("/api/settings/conv-retention").json()
    assert body == {"enabled": False, "keep_count": 1000}


def test_conv_retention_put_roundtrip_and_keys(client):
    r = client.put("/api/settings/conv-retention", json={"enabled": True, "keep_count": 50})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": True, "keep_count": 50}
    # 落库的 key 与读取点是同一套字符串（漂移过一次：读侧写死默认）
    import database as db
    assert db.get_app_setting("conv_retention.enabled") == "true"
    assert db.get_app_setting("conv_retention.keep_count") == "50"
    assert client.get("/api/settings/conv-retention").json()["keep_count"] == 50


def test_conv_retention_rejects_out_of_range(client):
    for bad in ({"enabled": True, "keep_count": -1},
                {"enabled": True, "keep_count": 100001}):
        assert client.put("/api/settings/conv-retention", json=bad).status_code == 422


def test_conv_retention_tolerates_dirty_stored_value(client, base_db):
    import database as db
    db.set_app_setting("conv_retention.enabled", "1")     # 不是 "true"
    db.set_app_setting("conv_retention.keep_count", "abc")
    body = client.get("/api/settings/conv-retention").json()
    # 脏值不能变成 500，也不能把 keep_count 变成 None 让前端渲染崩掉
    assert body["enabled"] is False
    assert body["keep_count"] == 1000


# ── 后处理四开关 ─────────────────────────────────────────────────────────────

def test_post_process_defaults_all_on(client):
    assert client.get("/api/settings/post-process").json() == {
        "enabled": True, "generate_title": True,
        "extract_memory": True, "generate_skill": True,
    }


def test_post_process_put_syncs_memory_and_get(client, clean_pp_config):
    payload = {"enabled": True, "generate_title": False,
               "extract_memory": True, "generate_skill": False}
    put = client.put("/api/settings/post-process", json=payload).json()
    assert put == payload
    # GET 从库读、PUT 返回内存值：两者不一致就说明"改完没生效"
    assert client.get("/api/settings/post-process").json() == payload
    live = clean_pp_config.get_post_process_config()
    assert (live.enabled, live.generate_title, live.extract_memory, live.generate_skill) == \
        (True, False, True, False)


def test_post_process_restart_reads_same(client, clean_pp_config):
    """重启后由 load_*_from_db 回填，读回的值必须与关掉应用前一致"""
    import post_processor as pp
    client.put("/api/settings/post-process", json={
        "enabled": False, "generate_title": False,
        "extract_memory": True, "generate_skill": False})
    fresh = pp.load_post_process_config_from_db()
    assert (fresh.enabled, fresh.generate_title, fresh.extract_memory, fresh.generate_skill) == \
        (False, False, True, False)
    # 库只存这 4 个开关，其余字段（超时/频率）始终取 dataclass 默认
    assert fresh.skill_interval == pp.PostProcessConfig().skill_interval


def test_post_process_partial_payload_keeps_other_switches(client, clean_pp_config):
    """契约变更（round 22）：四字段全 Optional，只发一项 = 只改那一项。

    这里原本钉的是"四字段都有默认值 True，只发一项会把其余三项静默打开并落库"，
    并以此警告别改成增量提交。改成 Optional 才是安全方向：前端本来就整包提交，
    增量语义只会影响 API 调用方，而"少传即打开"对调用方是不可见的枪。
    现契约由 tests/test_settings_contract.py 整体钉住。
    """
    client.put("/api/settings/post-process", json={
        "enabled": True, "generate_title": False, "extract_memory": False, "generate_skill": False})
    r = client.put("/api/settings/post-process", json={"enabled": False})
    assert r.json() == {"enabled": False, "generate_title": False,
                        "extract_memory": False, "generate_skill": False}
    assert client.get("/api/settings/post-process").json() == r.json()


# ── Tavily 密钥 ──────────────────────────────────────────────────────────────

def test_tavily_key_never_echoed_in_plaintext(client):
    key = "tvly-abcdefghijklmnopqrst"
    body = client.put("/api/settings/web-tools", json={"tavily_api_key": key}).json()
    assert body["tavily_api_key_set"] is True
    assert body["tavily_api_key_source"] == "db"
    masked = body["tavily_api_key_masked"]
    assert key not in masked and "…" in masked
    # 再 GET 一次也不能出现明文
    assert key not in client.get("/api/settings/web-tools").text


def test_tavily_env_wins_over_db_and_clear_is_explicit(client, base_db):
    import database as db
    db.set_app_setting("web.tavily_api_key", "tvly-dbkey0000000001")
    os.environ["TAVILY_API_KEY"] = "tvly-envkey0000000001"
    body = client.get("/api/settings/web-tools").json()
    assert body["tavily_api_key_source"] == "env"
    assert body["tavily_api_key_masked"].startswith("tvly")
    # 清库里的行不影响 env 提供的密钥 —— 面板此时仍显示"已设置/来自环境变量"
    assert client.put("/api/settings/web-tools", json={"tavily_api_key": ""}).json()[
        "tavily_api_key_source"] == "env"
    assert db.get_app_setting("web.tavily_api_key") in (None, "")
    os.environ.pop("TAVILY_API_KEY", None)
    after = client.get("/api/settings/web-tools").json()
    assert after["tavily_api_key_set"] is False and after["tavily_api_key_source"] == ""


def test_tavily_rejects_overlong_key(client):
    assert client.put("/api/settings/web-tools", json={"tavily_api_key": "x" * 257}).status_code == 422


# ── 工具启用（默认全局档）────────────────────────────────────────────────────

def test_tool_toggle_defaults_to_global_mode(client, base_db):
    import database as db
    name = "shell_exec"
    # 前端 setToolEnabled 从不带 mode，落到后端的默认值必须是全局档 all，
    # 否则"关掉的工具"只关了一个模式，换个模式又冒出来。
    assert client.patch(f"/api/agent/tools/{name}/enabled", json={"enabled": False}).status_code == 200
    assert name in db.list_disabled_tools("ssh")
    assert name in db.list_disabled_tools("chat")
    assert client.patch(f"/api/agent/tools/{name}/enabled", json={"enabled": True}).status_code == 200
    assert name not in db.list_disabled_tools("ssh")
    # 重新启用要把那行真的置回 1，只删行或只写 mode 行都不算
    assert db.list_disabled_tools("all") == []


def test_tools_list_reports_enabled_flag(client, base_db):
    client.patch("/api/agent/tools/web_fetch/enabled", json={"enabled": False})
    # 这条 GET 没有 mode 查询参数可传（routes/agent.py 的 list_tools 是零参数，
    # 返回体里的 mode 是服务端固定的 SSH_MODE 常量）。原先挂一个
    # params={"mode": "ssh"} 看着像在验模式隔离，实际整个被路由丢弃 ——
    # 装饰性参数会教后来的人误以为这个接口按模式分答案。
    tools = client.get("/api/agent/tools").json()
    assert tools["mode"] == "ssh"
    flags = {t["name"]: t["enabled"] for t in tools["tools"]}
    assert flags.get("web_fetch") is False
    assert any(v is True for v in flags.values())


# ── 策略启用 ─────────────────────────────────────────────────────────────────

def test_strategy_toggle_and_404(client, base_db):
    import database as db
    sid = db.add_agent_strategy(mode="ssh", strategy_type="reflection",
                                name="测试策略", content="x", enabled=1)
    r = client.patch(f"/api/agent/strategies/{sid}/enabled", params={"enabled": 0})
    assert r.status_code == 200, r.text
    rows = client.get("/api/agent/strategies", params={"mode": "ssh"}).json()["strategies"]
    assert next(s for s in rows if s["id"] == sid)["enabled"] == 0
    assert client.patch("/api/agent/strategies/nope/enabled", params={"enabled": 1}).status_code == 404


# ── 技能 per-mode 开关 ───────────────────────────────────────────────────────

def _new_skill(db, **kw):
    args = dict(name="巡检技能", slug="ops-check", system_prompt="p")
    args.update(kw)
    return db.create_skill(**args)


def test_skill_mode_toggle_only_affects_that_mode(client, base_db):
    import database as db
    sid = _new_skill(db, enabled=1)["id"]
    assert client.patch(f"/api/skills/{sid}/enabled",
                        json={"enabled": False}, params={"mode": "chat"}).status_code == 200
    assert sid in db.list_disabled_skills_for_mode("chat")
    assert sid not in db.list_disabled_skills_for_mode("ssh")

    listed = client.get("/api/skills", params={"mode": "chat"}).json()
    assert next(s for s in listed if s["id"] == sid)["mode_enabled"] is False
    listed_ssh = client.get("/api/skills", params={"mode": "ssh"}).json()
    assert next(s for s in listed_ssh if s["id"] == sid)["mode_enabled"] is True
    # 不带 mode 时不伪造 mode_enabled 字段（前端按存在与否分支）
    assert "mode_enabled" not in client.get("/api/skills").json()[0]


def test_skill_global_disable_wins_over_per_mode_enable(client, base_db):
    import database as db
    sid = _new_skill(db, enabled=1)["id"]
    client.patch(f"/api/skills/{sid}/enabled", json={"enabled": False})
    row = next(s for s in client.get("/api/skills", params={"mode": "chat"}).json()
               if s["id"] == sid)
    assert row["enabled"] == 0 and row["mode_enabled"] is False


def test_skill_toggle_rejects_unknown_id(client, base_db):
    assert client.patch("/api/skills/not-a-skill/enabled",
                        json={"enabled": True}, params={"mode": "chat"}).status_code == 404


def test_skill_mode_enable_turns_global_on(client, base_db):
    import database as db
    sid = _new_skill(db, enabled=0)["id"]
    # 全局关着的技能单独按模式启用没有意义，后端会顺手把全局打开
    assert client.patch(f"/api/skills/{sid}/enabled",
                        json={"enabled": True}, params={"mode": "ssh"}).status_code == 200
    assert db.get_skill(sid)["enabled"] == 1
    assert next(s for s in client.get("/api/skills", params={"mode": "ssh"}).json()
                if s["id"] == sid)["mode_enabled"] is True
