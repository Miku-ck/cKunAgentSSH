"""插件同意流守卫：安装即停用 / 未验证不能启用 / 配置变更撤销同意 /
只读确认不能凭空授权 / 变更即关闭进程。"""
import json

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
import routes.plugins as pl_mod


class _FakeManager:
    def __init__(self):
        self.closed: list[str] = []
        self.tools = [
            {"name": "list_pods", "description": "列出 Pod",
             "inputSchema": {"type": "object", "properties": {}}, "readOnlyHint": True},
            {"name": "restart", "description": "重启部署",
             "inputSchema": {"type": "object", "properties": {}}, "readOnlyHint": False},
        ]

    async def list_tools(self, row, timeout_s=10.0):
        return self.tools

    async def close(self, plugin_id):
        self.closed.append(plugin_id)


@pytest.fixture
def env(base_db, monkeypatch):
    manager = _FakeManager()
    monkeypatch.setattr(pl_mod, "get_plugin_manager", lambda: manager)
    app = fastapi.FastAPI()
    app.include_router(pl_mod.router, prefix="/api")
    return TestClient(app), manager


def _install(client, slug="k8s", **over):
    body = {"slug": slug, "name": "K8s 巡检", "command": "python", "args": ["-m", "srv"]}
    body.update(over)
    return client.post("/api/plugins", json=body)


def test_install_is_disabled_and_unconfirmed(env):
    client, _ = env
    resp = _install(client)
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["enabled"] == 0
    assert row["consented_at"] is None
    assert row["config_rev"] == 1
    assert "env_ciphertext" not in row


def test_invalid_slug_and_duplicate(env):
    client, _ = env
    assert _install(client, slug="Bad Slug").status_code in (400, 422)
    assert _install(client).status_code == 201
    assert _install(client).status_code == 409


def test_command_must_be_program_name_only(env):
    client, _ = env
    assert _install(client, slug="bad", command="python -m srv").status_code == 422


def test_consent_requires_validated_tool_list(env):
    client, _ = env
    pid = _install(client).json()["id"]
    assert client.post(f"/api/plugins/{pid}/consent").status_code == 400

    validated = client.post(f"/api/plugins/{pid}/validate")
    assert validated.status_code == 200, validated.text
    assert validated.json()["count"] == 2
    assert len(json.loads(db.get_plugin(pid)["tools_json"])) == 2

    consented = client.post(f"/api/plugins/{pid}/consent").json()
    assert consented["enabled"] == 1
    assert consented["consented_at"]


def test_readonly_confirmation_rejects_unknown_tools(env):
    client, _ = env
    pid = _install(client).json()["id"]
    client.post(f"/api/plugins/{pid}/validate")
    assert client.post(f"/api/plugins/{pid}/readonly", json={"names": ["ghost"]}).status_code == 400
    ok = client.post(f"/api/plugins/{pid}/readonly", json={"names": ["list_pods"]})
    assert ok.status_code == 200
    assert json.loads(ok.json()["readonly_tools_json"]) == ["list_pods"]


def test_config_change_revokes_consent_and_closes_session(env):
    client, manager = env
    pid = _install(client).json()["id"]
    client.post(f"/api/plugins/{pid}/validate")
    client.post(f"/api/plugins/{pid}/consent")

    updated = client.put(f"/api/plugins/{pid}", json={"timeout_s": 60}).json()
    assert updated["config_rev"] == 2
    assert updated["enabled"] == 0
    assert updated["consented_at"] is None
    assert json.loads(updated["tools_json"]) == []
    assert pid in manager.closed

    # 重新确认前不能启用
    assert client.patch(f"/api/plugins/{pid}/enabled", json={"enabled": True}).status_code == 400


def test_disable_closes_session_and_delete_removes_row(env):
    client, manager = env
    pid = _install(client).json()["id"]
    client.post(f"/api/plugins/{pid}/validate")
    client.post(f"/api/plugins/{pid}/consent")
    assert client.patch(f"/api/plugins/{pid}/enabled", json={"enabled": False}).status_code == 200
    assert pid in manager.closed
    assert client.delete(f"/api/plugins/{pid}").json() == {"deleted": True}
    assert client.get("/api/plugins").json() == []
