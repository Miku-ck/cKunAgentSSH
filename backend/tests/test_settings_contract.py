"""设置写入契约统一（round 22）—— 全仓只留一种 PUT /settings/* 语义。

背景：同一个文件里并存过三套契约 ——
  · /settings/http、/settings/permission-policy：字段全 Optional（不传 = 不改）+ 单事务落库
  · /settings/memory、/settings/conv-retention：字段全带默认值（少传 = 静默重置）+ 逐条提交
  · /settings/post-process：同上，且该行为被 round 20 显式钉住

前端永远整包提交，所以"少传 = 重置"在应用内不可见，但它是给 API 调用方留的枪：
脚本或第三方只发一项就把其余设置抹回默认。本文件把三套收敛成 Optional + bulk 一套，
并钉住 round 20 那条登记的反面 —— 增量提交不得静默打开未传的开关。
"""
import os

import pytest


# ── 夹具 ────────────────────────────────────────────────────────────────────

@pytest.fixture
def base_db(tmp_workspace):
    import database as db
    db_path = os.path.join(tmp_workspace, "contract22.db")
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
    app = fastapi.FastAPI()
    app.include_router(settings_router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def clean_pp_config():
    """PUT /settings/post-process 会就地替换全局 cfg，用例跑完必须还原"""
    import post_processor as pp
    saved = pp.get_post_process_config()
    yield pp
    pp.set_post_process_config(saved)


# ── 记忆设置：不传 = 不改 ────────────────────────────────────────────────────

def test_memory_partial_put_keeps_other_fields(client):
    r = client.put("/api/settings/memory", json={
        "enabled": True, "max_items": 200, "inject_budget_tokens": 3000,
        "blocks_budget_tokens": 900, "search_timeout_s": 12.0,
        "reconcile_llm_enabled": False,
    })
    assert r.status_code == 200, r.text
    # 只发一项：其余五项必须保持上一次的显式值，而不是落回 Field 默认
    r2 = client.put("/api/settings/memory", json={"max_items": 150})
    assert r2.status_code == 200, r2.text
    assert r2.json() == {
        "enabled": True, "max_items": 150, "inject_budget_tokens": 3000,
        "blocks_budget_tokens": 900, "search_timeout_s": 12.0,
        "reconcile_llm_enabled": False,
    }
    assert client.get("/api/settings/memory").json() == r2.json()


def test_memory_put_writes_single_transaction(client, monkeypatch):
    """六个键必须一次 bulk 落库：逐条提交时中途失败会留下半新半旧的库。"""
    import database as db
    calls: list[dict] = []
    real_bulk = db.update_app_settings_bulk

    def spy_bulk(settings: dict[str, str]) -> None:
        calls.append(dict(settings))
        real_bulk(settings)

    monkeypatch.setattr(db, "update_app_settings_bulk", spy_bulk)
    client.put("/api/settings/memory", json={"max_items": 300})
    assert len(calls) == 1, f"期望一次批量写，实际 {len(calls)} 次"
    assert calls[0] == {"memory.max_items": "300"}


def test_memory_empty_put_changes_nothing(client, monkeypatch):
    """空请求体 = 一次都不写：不能让 {} 变成"全部回默认"的快捷键。"""
    client.put("/api/settings/memory", json={"max_items": 300, "enabled": False})
    import database as db
    calls: list[dict] = []

    def spy(settings: dict[str, str]) -> None:
        calls.append(dict(settings))

    monkeypatch.setattr(db, "update_app_settings_bulk", spy)
    r = client.put("/api/settings/memory", json={})
    assert r.status_code == 200, r.text
    assert calls == [], "空请求体不应产生任何写入"
    assert r.json()["max_items"] == 300
    assert r.json()["enabled"] is False


# ── 后处理四开关：同一套契约 ─────────────────────────────────────────────────

def test_post_process_partial_put_keeps_other_switches(client, clean_pp_config):
    client.put("/api/settings/post-process", json={
        "enabled": True, "generate_title": False,
        "extract_memory": False, "generate_skill": False})
    r = client.put("/api/settings/post-process", json={"enabled": False})
    assert r.json() == {"enabled": False, "generate_title": False,
                        "extract_memory": False, "generate_skill": False}
    # PUT 返回内存值、GET 从库读：两边必须一致，否则"改完没生效"
    assert client.get("/api/settings/post-process").json() == r.json()
    live = clean_pp_config.get_post_process_config()
    assert (live.enabled, live.generate_title, live.extract_memory, live.generate_skill) == \
        (False, False, False, False)


def test_post_process_restart_reads_same_after_partial(
        client, clean_pp_config):
    import post_processor as pp
    client.put("/api/settings/post-process", json={
        "enabled": True, "generate_title": False,
        "extract_memory": False, "generate_skill": False})
    client.put("/api/settings/post-process", json={"generate_skill": True})
    fresh = pp.load_post_process_config_from_db()
    assert (fresh.enabled, fresh.generate_title, fresh.extract_memory,
            fresh.generate_skill) == (True, False, False, True)


# ── 会话保留：同一套契约 ─────────────────────────────────────────────────────

def test_conv_retention_partial_put_keeps_other_field(client):
    client.put("/api/settings/conv-retention", json={"enabled": True, "keep_count": 42})
    r = client.put("/api/settings/conv-retention", json={"enabled": False})
    assert r.json() == {"enabled": False, "keep_count": 42}
    assert client.get("/api/settings/conv-retention").json() == r.json()


def test_conv_retention_rejects_out_of_range(client):
    for bad in ({"keep_count": -1}, {"keep_count": 100001}, {"enabled": "notabool"}):
        assert client.put("/api/settings/conv-retention", json=bad).status_code == 422


# ── 诊断快照的脱敏边界 ──────────────────────────────────────────────────────

def test_mask_value_keeps_numeric_budgets_readable():
    """"token" 这个 hint 命中了 memory.inject_budget_tokens —— 纯数值预算被掩成
    "已设置"，诊断面板就读不到真正需要读的东西（预算调没调对）。

    放行条件是「键在数值白名单里」+「值解析为数字」，不是"值是数字就放行"：
    后者会让一个 16 位纯数字的 API key 原样进诊断响应。
    """
    from routes.settings import _mask_value
    assert _mask_value("memory.inject_budget_tokens", "3000") == "3000"
    assert _mask_value("memory.blocks_budget_tokens", "900") == "900"
    assert _mask_value("memory.max_items", "200") == "200"
    assert _mask_value("memory.search_timeout_s", "8.0") == "8.0"
    assert _mask_value("http.timeout_seconds", "33") == "33"
    assert _mask_value("permission.allowlist_ttl_hours", "0") == "0"
    # 布尔开关本来就不含 hint，保持原样
    assert _mask_value("memory.reconcile_llm_enabled", "true") == "true"
    # 字符串密钥必须继续被打码（hint 表里的 token/api_key/proxy/password/secret）
    assert _mask_value("web.tavily_api_key", "tvly-abcdefghijklmnop") == "tvly…mnop"


def test_mask_value_numeric_rule_cannot_leak_passwords():
    """数值例外只给数值设置键：口令恰好是纯数字时仍必须走代理脱敏。"""
    from routes.settings import _mask_value
    assert _mask_value("http.proxy_url", "socks5://admin:123456@10.0.0.1:1080") == \
        "socks5://10.0.0.1:1080"
    assert "123456" not in _mask_value(
        "http.proxy_url", "http://u:987654321@host:8080")
    # 纯数字形态的密钥字面量（tavily 就有这种 key）照旧打码：键不在数值白名单里
    assert _mask_value("web.tavily_api_key", "1234567890123456") == "1234…3456"
    # 数值键里的脏值（手工改库写成一句话）不放行，仍按 hint 打码
    assert _mask_value("memory.inject_budget_tokens", "abc") == "已设置"


def test_mask_value_covers_every_numeric_app_setting_key():
    """数值白名单必须与实际读取器同集合：漏一个键 = 那个键在面板上读不到数。

    app_settings 只有 16 个键（database.get_* 那几处），其中 9 个是数值、其余是
    布尔/字符串（含密钥）。这里把两侧对齐钉住，新增数值设置时若忘了登记，
    诊断面板会静默显示"已设置"。
    """
    from routes.settings import _DIAG_NUMERIC_KEYS
    numeric = {
        "conv_retention.keep_count", "http.retry_count", "http.timeout_seconds",
        "memory.blocks_budget_tokens", "memory.inject_budget_tokens",
        "memory.max_items", "memory.search_timeout_s",
        "permission.allowlist_ttl_hours", "permission.approval_fallback_s",
    }
    assert set(_DIAG_NUMERIC_KEYS) == numeric


def test_diagnostics_masks_only_string_secrets_not_numeric_budgets(client, monkeypatch):
    """端点级对照：数值预算原样回显、密钥仍打码，且明文不出现在响应体里。"""
    import netconfig
    import database as db
    monkeypatch.setattr(netconfig, "_policy", {
        "proxy_url": "", "timeout_seconds": 0, "retry_count": 1})
    monkeypatch.setattr(netconfig, "_loaded", True)
    db.set_app_setting("memory.inject_budget_tokens", "3000")
    db.set_app_setting("memory.blocks_budget_tokens", "900")
    db.set_app_setting("web.tavily_api_key", "tvly-abcdefghijklmnop")

    body = client.get("/api/settings/diagnostics").json()
    raw = client.get("/api/settings/diagnostics").text
    assert "abcdefghijklmnop" not in raw, "密钥必须继续被打码"
    assert body["app_settings"]["web.tavily_api_key"] == "tvly…mnop"
    assert body["app_settings"]["memory.inject_budget_tokens"] == "3000"
    assert body["app_settings"]["memory.blocks_budget_tokens"] == "900"


# ── bulk helper 自身的事务性 ────────────────────────────────────────────────

def test_bulk_write_rolls_back_all_keys_when_one_fails(base_db):
    """单事务的价值所在：第二条失败时第一条不得留在库里。"""
    import database as db

    real_get_conn = db._get_conn
    state = {"n": 0}

    class FlakyConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args, **kwargs):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("模拟磁盘写失败")
            return self._conn.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def get_flaky_conn():
        return FlakyConn(real_get_conn())

    db._get_conn = get_flaky_conn
    try:
        with pytest.raises(RuntimeError):
            db.update_app_settings_bulk({
                "memory.max_items": "333",
                "memory.inject_budget_tokens": "4444",
            })
    finally:
        db._get_conn = real_get_conn

    assert db.get_app_setting("memory.max_items") is None
    assert db.get_app_setting("memory.inject_budget_tokens") is None
