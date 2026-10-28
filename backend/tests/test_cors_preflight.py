from fastapi.testclient import TestClient


def _preflight(client: TestClient, path: str, method: str):
    return client.options(
        path,
        headers={
            "Origin": "http://127.0.0.1:5555",
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type, x-api-token",
        },
    )


def test_cors_preflight_allows_put_on_qq_config():
    """回归：QQ「保存并连接」的 PUT /bot/qq/config 曾被 CORS allow_methods 漏配 PUT。

    预检被 Starlette 判为 Disallowed CORS method 直接 400，浏览器拦截实际请求，
    fetch 抛 Failed to fetch → 前端兜底成误导性的「网络连接失败，请检查服务是否运行」
    （后端其实一直在线，日志里 OPTIONS 400 刷屏即此因）。
    """
    from main import app

    client = TestClient(app)
    res = _preflight(client, "/api/bot/qq/config", "PUT")
    assert res.status_code == 200, res.text
    assert "PUT" in res.headers.get("access-control-allow-methods", "").upper()
    assert res.headers.get("access-control-allow-origin") == "http://127.0.0.1:5555"


def test_cors_preflight_allows_put_on_settings():
    """同一 PUT 缺失影响所有 /settings PUT（写设置会预检失败 → 静默回滚）。"""
    from main import app

    client = TestClient(app)
    res = _preflight(client, "/api/settings/http", "PUT")
    assert res.status_code == 200, res.text
    assert "PUT" in res.headers.get("access-control-allow-methods", "").upper()
