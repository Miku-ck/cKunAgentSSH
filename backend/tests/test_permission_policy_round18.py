"""E3：永久授权保留时长（allowlist TTL）

覆盖 gate 侧的到期判定与 sidecar 兜底、TTL 进程内生效，以及
GET/PUT /settings/permission-policy 的落库 + 推 gate。
"""
import json
import os
import time

import fastapi
import pytest
from fastapi.testclient import TestClient

import agent.harness.reins.gate as gate_mod
import database as db
from agent.harness.reins.gate import PermissionGate


@pytest.fixture
def isolated_allowlist(tmp_workspace, monkeypatch):
    """allowlist.txt / allowlist.meta.json 都落进 tmp 目录，并重置进程级单例。"""
    path = os.path.join(tmp_workspace, "allowlist.txt")
    monkeypatch.setattr(gate_mod, "ALLOWLIST_PATH", path)
    monkeypatch.setattr(gate_mod, "_global_permission_gate", None)
    monkeypatch.setattr(gate_mod, "_ALLOWLIST_TTL_HOURS", 0)
    return path


def _meta_path(allowlist_path: str) -> str:
    return os.path.join(os.path.dirname(allowlist_path), "allowlist.meta.json")


def _grant(tool: str, command: str | None = None) -> tuple[PermissionGate, str]:
    g = PermissionGate()
    key = g.grant(tool, scope="always", command=command)
    return g, key


def test_default_ttl_keeps_allowlist_forever(isolated_allowlist):
    """TTL=0（默认）：写入即长期有效，与引入该设置之前的行为一致。"""
    g, key = _grant("shell_exec", "systemctl restart nginx")
    g._permanent_granted_at[key] = int(time.time()) - 10 * 365 * 86400  # 十年前
    assert g.is_permanently_allowed("shell_exec", {"command": "systemctl restart nginx"}) is True
    assert g.check_permission("shell_exec", {"command": "systemctl restart nginx"},
                             risk="dangerous", run_id="r1") is True


def test_expired_entry_stops_granting(isolated_allowlist):
    """TTL=24h 且授予时间在 25h 前 → 不再算预授权，且就地从生效集合里移除。"""
    g, key = _grant("shell_exec", "systemctl restart nginx")
    g.allowlist_ttl_hours = 24
    g._permanent_granted_at[key] = int(time.time()) - 25 * 3600
    assert g.is_permanently_allowed("shell_exec", {"command": "systemctl restart nginx"}) is False
    assert key not in g._confirmed_permanent


def test_prune_persists_removal(isolated_allowlist):
    """清理掉的过期键要落盘：否则重启后同一行又被读回来继续免审。"""
    g, key = _grant("service_manage", "systemctl stop nginx")
    with open(isolated_allowlist, encoding="utf-8") as f:
        assert key in f.read()
    g.allowlist_ttl_hours = 24
    g._permanent_granted_at[key] = int(time.time()) - 48 * 3600
    assert g.prune_expired_allowlist() == 1
    with open(isolated_allowlist, encoding="utf-8") as f:
        assert key not in f.read()
    # sidecar 同样不再留这条
    with open(_meta_path(isolated_allowlist), encoding="utf-8") as f:
        assert key not in json.load(f)


def test_manual_entry_without_timestamp_uses_file_mtime(isolated_allowlist):
    """无 sidecar 记录（手工登记/升级前旧行）→ 按 allowlist.txt 的 mtime 兜底。

    方向必须是"别当场撤销"：prod 预登记条目在升级当天集体失效，运维会在故障
    现场发现命令突然要走审批。
    """
    key = "shell_exec:" + "a" * 16
    with open(isolated_allowlist, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    old = int(time.time()) - 5 * 3600
    os.utime(isolated_allowlist, (old, old))
    g = PermissionGate(allowlist_ttl_hours=24)
    assert g._permanent_granted_at[key] == old
    assert key in g._confirmed_permanent  # 5h 前 < 24h，仍有效

    g.allowlist_ttl_hours = 1
    assert g.prune_expired_allowlist() == 1
    assert key not in g._confirmed_permanent


def test_set_ttl_clamps_and_applies_to_existing_singleton(isolated_allowlist):
    """改 TTL 要即时作用于已存在的单例（进程多半早已启动），并夹紧到合法区间。"""
    g, key = _grant("shell_exec", "rm -rf /tmp/x")
    gate_mod._global_permission_gate = g
    assert gate_mod.set_allowlist_ttl_hours(-5) == 0
    assert gate_mod.set_allowlist_ttl_hours(gate_mod.ALLOWLIST_TTL_MAX_HOURS + 9999) \
        == gate_mod.ALLOWLIST_TTL_MAX_HOURS
    assert g.allowlist_ttl_hours == gate_mod.ALLOWLIST_TTL_MAX_HOURS

    assert gate_mod.set_allowlist_ttl_hours(24) == 24
    assert g.allowlist_ttl_hours == 24
    g._permanent_granted_at[key] = int(time.time()) - 25 * 3600
    assert gate_mod.set_allowlist_ttl_hours(12) == 12  # 收紧时顺带清理
    assert key not in g._confirmed_permanent


def test_permission_entries_report_expiry(isolated_allowlist):
    """管理接口要能诚实回答"这条预登记还有效多久"。"""
    g, key = _grant("shell_exec", "df -h")
    g.allowlist_ttl_hours = 168
    entries = g.permanent_entries()
    assert [e["key"] for e in entries] == [key]
    granted = entries[0]["granted_at"]
    assert entries[0]["expires_at"] == granted + 168 * 3600

    g.allowlist_ttl_hours = 0
    assert g.permanent_entries()[0]["expires_at"] is None


@pytest.fixture
def base_db(tmp_workspace):
    """真实 DB（app_settings 读写要落库）。base_db 只在 test_admin_api_round4 里定义，
    不是 conftest 公共 fixture，这里自带一份。"""
    db_path = os.path.join(tmp_workspace, "policy.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


def test_db_permission_policy_dirty_value_falls_back(base_db):
    assert db.get_permission_policy() == {
        "allowlist_ttl_hours": 0,
        "auto_approve_enabled": True,
        "approval_fallback_s": 600,
    }
    db.set_app_setting("permission.allowlist_ttl_hours", "abc")
    assert db.get_permission_policy() == {
        "allowlist_ttl_hours": 0,
        "auto_approve_enabled": True,
        "approval_fallback_s": 600,
    }
    db.set_app_setting("permission.allowlist_ttl_hours", "999999")
    assert db.get_permission_policy()["allowlist_ttl_hours"] == 0
    db.set_app_setting("permission.allowlist_ttl_hours", "168")
    assert db.get_permission_policy()["allowlist_ttl_hours"] == 168


def test_db_审批策略新字段_默认脏值与合法写回读(base_db):
    """兜底时长与自动审批开关的 DB 读回口径：脏值/越界回默认（同 TTL）。"""
    db.set_app_setting("permission.approval_fallback_s", "99999")
    assert db.get_permission_policy()["approval_fallback_s"] == 600
    db.set_app_setting("permission.approval_fallback_s", "-3")
    assert db.get_permission_policy()["approval_fallback_s"] == 600
    db.set_app_setting("permission.approval_fallback_s", "0")
    db.set_app_setting("permission.auto_approve_enabled", "false")
    pol = db.get_permission_policy()
    assert pol["approval_fallback_s"] == 0
    assert pol["auto_approve_enabled"] is False


# ── 路由 ──


@pytest.fixture
def policy_client(base_db, isolated_allowlist):
    from routes.settings import router
    app = fastapi.FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_put_permission_policy_persists_and_pushes_gate(policy_client, isolated_allowlist):
    _, key = _grant("shell_exec", "uptime")
    assert policy_client.put("/api/settings/permission-policy",
                             json={"allowlist_ttl_hours": 24}).status_code == 200
    assert gate_mod._ALLOWLIST_TTL_HOURS == 24
    assert gate_mod._get_global_permission_gate_sync().allowlist_ttl_hours == 24
    assert db.get_permission_policy()["allowlist_ttl_hours"] == 24

    got = policy_client.get("/api/settings/permission-policy").json()
    assert got["allowlist_ttl_hours"] == 24
    # 之后新建的 gate（模拟重启）从模块值继承 TTL，不会退回"永久"
    g2 = PermissionGate()
    assert g2.allowlist_ttl_hours == 24
    assert key in g2._confirmed_permanent  # 刚写入，未到期


def test_put_permission_policy_pushes_runtime_toggles(policy_client, base_db):
    """PUT 必须把开关推进运行时模块（同 E3 推 gate 的理由：只写库要重启才生效）。

    新字段是 Optional「不传 = 不改」（G4/G5 的 /settings/http 同款契约）：
    带默认值的请求模型会让只发 TTL 的旧调用把 auto_approve 静默翻开。
    """
    import agent.harness.reins.auto_approve as aa_mod
    from agent.harness.reins import core as core_mod
    ttl_before = gate_mod._ALLOWLIST_TTL_HOURS
    fallback_before = core_mod.approval_fallback_seconds()
    try:
        r = policy_client.put("/api/settings/permission-policy", json={
            "auto_approve_enabled": False, "approval_fallback_s": 900})
        assert r.status_code == 200
        assert aa_mod.AUTO_APPROVE_ENABLED is False
        assert core_mod.approval_fallback_seconds() == 900
        assert db.get_permission_policy()["approval_fallback_s"] == 900

        # 只发 TTL：另两个字段必须原地不动
        r2 = policy_client.put("/api/settings/permission-policy",
                               json={"allowlist_ttl_hours": 48})
        got = r2.json()
        assert got["auto_approve_enabled"] is False
        assert got["approval_fallback_s"] == 900
        assert aa_mod.AUTO_APPROVE_ENABLED is False

        # 越界仍 422（0 合法 = 永久等待，不进这里）
        assert policy_client.put("/api/settings/permission-policy",
                                 json={"approval_fallback_s": 86401}).status_code == 422
        assert policy_client.put("/api/settings/permission-policy",
                                 json={"approval_fallback_s": -1}).status_code == 422
    finally:
        # 模块态是本进程共享的，用例结束复原，别把后门的开关留在别人脚下
        aa_mod.AUTO_APPROVE_ENABLED = True
        core_mod.set_approval_fallback_seconds(fallback_before)
        gate_mod.set_allowlist_ttl_hours(ttl_before)
        db.set_app_setting("permission.allowlist_ttl_hours", str(ttl_before))
        db.set_app_setting("permission.auto_approve_enabled", "true")
        db.set_app_setting("permission.approval_fallback_s", str(int(fallback_before)))


def test_put_permission_policy_rejects_out_of_range(policy_client):
    assert policy_client.put("/api/settings/permission-policy",
                             json={"allowlist_ttl_hours": -1}).status_code == 422
    assert policy_client.put("/api/settings/permission-policy",
                             json={"allowlist_ttl_hours": 8761}).status_code == 422
