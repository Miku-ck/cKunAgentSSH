"""G4/G5/G9/G10 回归测试 — 出站 HTTP 策略、日志级别、诊断快照。

覆盖三条设计不变量（破坏任一条都会让"设置页显示的值"与"实际生效的值"分叉）：
1. 默认值 = 引入设置之前的行为（超时不覆盖调用点硬编码、重试 1 次、直连、日志 warning）。
2. PUT 之后生效值即时改变，且缓存客户端按新策略重建（否则代理要重启才生效）。
3. 诊断面板/设置 GET 不回显任何明文密钥，代理 URL 的 userinfo 也必须脱敏。
"""
import logging

import httpx
import pytest

import logconfig
import netconfig


# ── 夹具 ────────────────────────────────────────────────────────────────────

@pytest.fixture
def base_db(tmp_workspace):
    import os
    import database as db
    db_path = os.path.join(tmp_workspace, "http19.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


@pytest.fixture
def clean_policy():
    saved = dict(netconfig._policy)
    saved_loaded = netconfig._loaded
    netconfig._policy = {"proxy_url": "", "timeout_seconds": 0, "retry_count": 1}
    netconfig._loaded = True
    yield netconfig
    netconfig._policy = saved
    netconfig._loaded = saved_loaded


@pytest.fixture
def clean_loglevel():
    root = logging.getLogger()
    saved_level = root.level
    saved_handler = logconfig._handler
    saved_current = logconfig._current
    yield logconfig
    if logconfig._handler is not None and logconfig._handler is not saved_handler:
        root.removeHandler(logconfig._handler)
    logconfig._handler = saved_handler
    root.setLevel(saved_level)
    logconfig._current = saved_current


@pytest.fixture
def client(base_db):
    import fastapi
    from fastapi.testclient import TestClient
    from routes.settings import router as settings_router
    app = fastapi.FastAPI()
    app.include_router(settings_router, prefix="/api")
    return TestClient(app)


# ── G4 代理地址校验与脱敏 ───────────────────────────────────────────────────

def test_proxy_url_accepts_supported_schemes(clean_policy):
    for url in ("http://proxy:8080", "https://user:pw@proxy:443", "socks5://127.0.0.1:1080"):
        assert clean_policy.normalize_proxy_url(f"  {url} ") == url


def test_proxy_url_rejects_garbage(clean_policy):
    for bad in ("", "  ", "proxy:8080", "ftp://proxy:21", "http://", "http:///x"):
        if bad.strip():
            with pytest.raises(ValueError):
                clean_policy.normalize_proxy_url(bad)
    assert clean_policy.normalize_proxy_url("   ") == ""


def test_redact_proxy_drops_credentials(clean_policy):
    assert clean_policy.redact_proxy("http://user:pw@proxy.example:8080") == \
        "http://proxy.example:8080"
    # 主机名里不含 @，最后一个 @ 之后才是 host
    assert clean_policy.redact_proxy("http://u@p@proxy:8080") == "http://proxy:8080"
    assert clean_policy.redact_proxy("") == ""
    assert clean_policy.redact_proxy("不是URL") == ""


def test_set_policy_clamps_and_applies_immediately(clean_policy):
    p = clean_policy.set_policy("http://p:1", 30, 99)
    assert p["retry_count"] == netconfig.RETRY_MAX
    assert p["timeout_seconds"] == 30
    assert clean_policy.get_policy()["proxy_url"] == "http://p:1"
    # 脏值（非数字）不抛异常，回 0 = 不覆盖
    assert clean_policy.set_policy("", "abc", None)["timeout_seconds"] == 0


# ── G5 超时：默认必须等于旧硬编码 ────────────────────────────────────────────

def test_timeout_default_preserves_call_site_values(clean_policy):
    assert clean_policy.timeout_seconds(10.0) == 10.0
    assert clean_policy.http_timeout(15.0) == 15.0
    # LLM 路径没设时沿用旧的 connect10/read300 —— 设成标量会把长回答掐断
    assert clean_policy.http_timeout(clean_policy.LLM_TIMEOUT_DEFAULT) is \
        clean_policy.LLM_TIMEOUT_DEFAULT


def test_timeout_user_value_floors_read_at_60s(clean_policy):
    clean_policy.set_policy("", 5, 1)
    to = clean_policy.http_timeout(None)
    assert to.connect == 5.0
    # 用户填 5 秒是为了快速失败，但 read 低于 60s 会让正常长度的回答中断
    assert to.read == 60.0
    assert clean_policy.timeout_seconds(10.0) == 5.0


def test_new_client_passes_proxy_and_timeout(clean_policy, monkeypatch):
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(httpx, "AsyncClient", _capture)
    clean_policy.set_policy("http://user:pw@proxy:8080", 42, 2)
    with pytest.raises(RuntimeError):
        clean_policy.new_client(default_timeout=10.0)
    assert captured["proxy"] == "http://user:pw@proxy:8080"
    # connect 有 10s 上限：连不上的端口不该让用户等到 42s 才知道
    assert captured["timeout"].read == 60.0 and captured["timeout"].connect == 10.0
    # 不能默认开重定向：web_fetch 的 SSRF 守卫依赖不跟随
    assert "follow_redirects" not in captured

    captured.clear()
    with pytest.raises(RuntimeError):
        clean_policy.proxy_http_client()
    assert captured["proxy"] == "http://user:pw@proxy:8080"
    assert "timeout" not in captured  # 超时由 openai SDK 按每请求传


def test_new_client_without_proxy_stays_direct(clean_policy, monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: captured.update(kw))
    clean_policy.set_policy("", 0, 1)
    clean_policy.new_client(default_timeout=15.0)
    # 未配置超时时原样透传调用点默认值，不擅自加一层 Timeout 对象
    assert "proxy" not in captured and captured["timeout"] == 15.0


# ── 路由：读写 + 脱敏 + 缓存重建 ─────────────────────────────────────────────

def test_http_put_then_get_masks_proxy(client, clean_policy):
    assert client.get("/api/settings/http").json()["proxy_url_set"] is False
    r = client.put("/api/settings/http", json={
        "proxy_url": "http://user:pw@corp-proxy:3128",
        "timeout_seconds": 45,
        "retry_count": 2,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proxy_url_masked"] == "http://corp-proxy:3128"
    assert body["proxy_url_set"] is True
    assert "pw" not in r.text and "user:" not in r.text
    assert body["timeout_seconds"] == 45 and body["retry_count"] == 2
    # 生效值同步更新（不是只写库等重启）
    assert netconfig.get_policy()["timeout_seconds"] == 45


def test_http_put_rejects_bad_proxy_and_range(client, clean_policy):
    assert client.put("/api/settings/http", json={"proxy_url": "ftp://x:21"}).status_code == 422
    assert client.put("/api/settings/http", json={"timeout_seconds": 9999}).status_code == 422
    assert client.put("/api/settings/http", json={"retry_count": -1}).status_code == 422
    # 失败的写入不能改到生效策略
    assert netconfig.get_policy()["proxy_url"] == ""


def test_http_put_without_proxy_field_keeps_proxy(client, clean_policy):
    """只改超时不能顺手把代理抹掉 —— GET 从不回明文，前端带空串就是清除。"""
    client.put("/api/settings/http", json={
        "proxy_url": "socks5h://corp:1080", "timeout_seconds": 30, "retry_count": 1,
    })
    r = client.put("/api/settings/http", json={"timeout_seconds": 60})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proxy_url_masked"] == "socks5h://corp:1080"
    assert body["timeout_seconds"] == 60 and body["retry_count"] == 1
    assert netconfig.get_policy()["proxy_url"] == "socks5h://corp:1080"
    # 显式空串才是清除
    assert client.put("/api/settings/http", json={"proxy_url": ""}).json()["proxy_url_set"] is False


def test_http_put_rebuilds_cached_llm_clients(client, clean_policy, base_db):
    import asyncio
    import llm_adapter

    class _FakeHttp:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    class _FakeClient:
        def __init__(self):
            self._client = _FakeHttp()

    stale = _FakeClient()
    busy = _FakeClient()
    key_stale = ("http://a/v1", "EMPTY")
    key_busy = ("http://b/v1", "EMPTY")
    llm_adapter._client_cache[key_stale] = stale
    llm_adapter._client_cache[key_busy] = busy
    llm_adapter._client_in_flight[key_busy] = 1
    try:
        r = client.put("/api/settings/http", json={"proxy_url": "http://p:1",
                                                  "timeout_seconds": 0,
                                                  "retry_count": 1})
        assert r.status_code == 200
        assert r.json()["clients_rebuilt"] == 2
        assert stale._client.closed is True          # 无在途流 → 关连接池
        assert busy._client.closed is False          # 有在途流 → 只摘除不 aclose
        assert llm_adapter._client_cache == {}
    finally:
        llm_adapter._client_in_flight.pop(key_busy, None)
        asyncio.run(stale._client.aclose())


def test_llm_client_uses_policy_for_timeout_and_retries(clean_policy, monkeypatch):
    import asyncio
    import llm_adapter

    llm_adapter._client_cache.clear()
    clean_policy.set_policy("socks5h://proxy:1080", 120, 3)
    c = asyncio.run(llm_adapter._get_client("http://model.test/v1", "k"))
    try:
        assert c.max_retries == 3
        assert c.timeout.read == 120.0
        assert "model.test" in str(c.base_url)
    finally:
        llm_adapter._client_cache.clear()
        asyncio.run(c._client.aclose())


# ── 模型连接测试：报的是生效超时，不是写死的 10s ─────────────────────────────

def test_connection_test_reports_effective_timeout(client, clean_policy, base_db):
    import database as db
    cfg = db.create_model_config(
        name="t", base_url="http://upstream.test/v1", api_key="k",
        model_id="m1", provider="custom",
    )
    seen = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "m1"}]}

    class _FakeClient:
        async def get(self, url, **kwargs):
            seen["url"] = url
            import httpx as _hx
            raise _hx.ReadTimeout("boom")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _capture(default_timeout=None, **kwargs):
        seen["default_timeout"] = default_timeout
        return _FakeClient()

    clean_policy.set_policy("", 7, 0)
    orig = netconfig.new_client
    netconfig.new_client = _capture
    try:
        body = client.post(f"/api/settings/models/{cfg['id']}/test").json()
    finally:
        netconfig.new_client = orig
    assert body["status"] == "error"
    assert "7s" in body["detail"]
    assert body["timeout_seconds"] == 7
    assert seen["default_timeout"] == 10.0  # 调用点默认仍是原来的 10s


# ── G9 日志级别 ─────────────────────────────────────────────────────────────

def test_log_level_default_matches_previous_effective_output(clean_loglevel):
    # 装 handler 之前 root 无 handler → lastResort 只放 WARNING+，默认必须仍是 warning
    assert logconfig.DEFAULT_LEVEL == "warning"
    assert logconfig.normalize_level("DEBUG") == "debug"
    assert logconfig.normalize_level("verbose") == "warning"
    assert logconfig.normalize_level(None) == "warning"


def test_log_level_apply_installs_single_handler_and_sets_root(clean_loglevel):
    root = logging.getLogger()
    before = len(root.handlers)
    assert logconfig.apply_level("debug") == "debug"
    assert root.level == logging.DEBUG
    assert len(root.handlers) == before + 1
    logconfig.apply_level("error")
    assert root.level == logging.ERROR
    # 二次 apply 只改级别，不再加 handler（否则日志翻倍）
    assert len(root.handlers) == before + 1


def test_log_level_route_persists_and_applies(client, clean_loglevel, base_db):
    import database as db
    assert client.get("/api/settings/log-level").json()["level"] in logconfig.LEVELS
    r = client.put("/api/settings/log-level", json={"level": "debug"})
    assert r.status_code == 200 and r.json()["level"] == "debug"
    assert logging.getLogger().level == logging.DEBUG
    assert db.get_app_setting("log.level") == "debug"
    assert client.put("/api/settings/log-level", json={"level": "nope"}).status_code == 422


def test_log_level_load_from_db_falls_back_on_dirty_row(client, clean_loglevel, base_db):
    import database as db
    db.set_app_setting("log.level", "trace")
    assert logconfig.load_from_db() == "warning"
    db.set_app_setting("log.level", "INFO")
    assert logconfig.load_from_db() == "info"


# ── G10 诊断快照 ────────────────────────────────────────────────────────────

def test_diagnostics_masks_secrets_and_reports_state(client, clean_policy, base_db):
    import database as db
    db.set_app_setting("web.tavily_api_key", "tvly-abcdefghijklmnop")
    client.put("/api/settings/http", json={
        "proxy_url": "http://u:pw@diag-proxy:3128", "timeout_seconds": 33, "retry_count": 1})
    logconfig.apply_level("info")
    body = client.get("/api/settings/diagnostics").json()
    raw = client.get("/api/settings/diagnostics").text
    assert "pw@" not in raw and "abcdefghijklmnop" not in raw
    assert body["http"]["proxy"] == "http://diag-proxy:3128" and body["http"]["proxy_set"]
    assert body["http"]["timeout_seconds"] == 33
    assert body["logging"]["level"] == "info"
    assert body["app_settings"]["web.tavily_api_key"] == "tvly…mnop"
    assert body["database"]["path"].endswith("http19.db")
    assert body["database"]["size_bytes"] and body["database"]["counts"]["conversations"] == 0
    assert body["backend"]["version"]
    assert body["tick"]["running"] is False
    assert body["bridge"]["connected"] is False
    assert body["permissions"]["ttl_hours"] == 0


def test_diagnostics_counts_only_whitelisted_tables(base_db):
    import database as db
    assert db.count_rows("messages") is not None
    # 表名不来自外部入参：非白名单一律拒绝（防拼接注入）
    assert db.count_rows("app_settings") is None
    assert db.count_rows("messages; DROP TABLE messages") is None
    assert set(db.diag_counts()) == set(db._DIAG_COUNT_TABLES)
