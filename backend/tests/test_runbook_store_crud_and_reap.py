"""runbook_store 守卫 — 两表 CRUD / FK CASCADE / 重启收尸（滞留 running → stale_reaped）/
终态幂等 / 全参数绑定（铁律：SQL 不得拼接外部输入）。"""
import os

import pytest

import database as db
import runbook_store as rstore


@pytest.fixture
def base_db(tmp_workspace):
    db_path = os.path.join(tmp_workspace, "runbook_store.db")
    db._close_all_connections()
    db.init_db(db_path)
    rstore._reset_memo_for_tests()
    yield db_path
    db._close_all_connections()


def _mk_runbook(**over):
    data = {
        "name": "nginx 恢复",
        "description": "502 时重启上游并验证",
        "target_host": "conn-1",
        "steps": [
            {"command": "systemctl restart nginx",
             "assertCommand": "systemctl is-active nginx", "note": ""},
            {"command": "curl -sf localhost/health", "assertCommand": "", "note": "健康检查"},
        ],
        "enabled": True,
    }
    data.update(over)
    return rstore.create_runbook(data)


def test_create_get_list_roundtrip(base_db):
    row = _mk_runbook()
    assert row["id"]
    got = rstore.get_runbook(row["id"])
    assert got["name"] == "nginx 恢复"
    assert [s["command"] for s in got["steps"]] == [
        "systemctl restart nginx", "curl -sf localhost/health"]
    assert got["steps"][0]["assertCommand"] == "systemctl is-active nginx"
    assert got["enabled"] == 1

    listed = rstore.list_runbooks()
    assert [r["id"] for r in listed] == [row["id"]]
    assert listed[0]["steps"][1]["note"] == "健康检查"
    # 无历史时 last_run 摘要为空
    assert listed[0]["last_status"] is None


def test_update_partial_keeps_other_fields(base_db):
    row = _mk_runbook()
    updated = rstore.update_runbook(row["id"], {"name": "改名"})
    assert updated["name"] == "改名"
    # 未提供的字段保持原值（COALESCE 语义）
    assert updated["description"] == "502 时重启上游并验证"
    assert updated["target_host"] == "conn-1"
    assert [s["command"] for s in updated["steps"]] == [
        "systemctl restart nginx", "curl -sf localhost/health"]
    assert updated["enabled"] == 1

    # steps 键存在 = 整体替换；enabled=False 落 0；description 显式空串是合法值
    updated2 = rstore.update_runbook(row["id"], {
        "steps": [{"command": "echo hi", "assertCommand": "", "note": ""}],
        "enabled": False,
        "description": "",
    })
    assert [s["command"] for s in updated2["steps"]] == ["echo hi"]
    assert updated2["enabled"] == 0
    assert updated2["description"] == ""
    assert updated2["name"] == "改名"


def test_update_missing_returns_none(base_db):
    assert rstore.update_runbook("no-such-id", {"name": "x"}) is None


def test_delete_cascades_runs(base_db):
    """FK ON DELETE CASCADE：删剧本必须连带清执行历史（PRAGMA foreign_keys=ON）。"""
    row = _mk_runbook()
    rstore.create_runbook_run(row["id"])
    rstore.create_runbook_run(row["id"])
    assert len(rstore.list_runbook_runs(row["id"])) == 2
    assert rstore.delete_runbook(row["id"]) is True
    assert rstore.get_runbook(row["id"]) is None
    assert rstore.list_runbook_runs(row["id"]) == []


def test_run_progress_and_terminal_idempotent(base_db):
    """终态单一出口：finish 只允许 running → 终态，重复调用 no-op。"""
    row = _mk_runbook()
    rrb = rstore.create_runbook_run(row["id"], run_id="run-a")

    rstore.update_runbook_run(
        rrb["id"], run_id="run-b", current_step=1,
        steps_state=[{"seq": 0, "status": "success"}],
    )
    runs = rstore.list_runbook_runs(row["id"])
    assert runs[0]["run_id"] == "run-b"
    assert runs[0]["current_step"] == 1
    assert runs[0]["steps_state"] == [{"seq": 0, "status": "success"}]
    assert runs[0]["status"] == "running"
    assert runs[0]["finished_at"] is None

    assert rstore.finish_runbook_run(
        rrb["id"], "failed", error="第 2 步失败", diagnosis="检查 nginx 配置") is True
    first = rstore.list_runbook_runs(row["id"])[0]
    assert first["status"] == "failed"
    assert first["error"] == "第 2 步失败"
    assert first["diagnosis"] == "检查 nginx 配置"
    assert first["finished_at"]

    # 重复收尾：no-op，先到者写的 diagnosis/error 不被覆盖
    assert rstore.finish_runbook_run(rrb["id"], "success") is False
    again = rstore.list_runbook_runs(row["id"])[0]
    assert again["status"] == "failed"
    assert again["diagnosis"] == "检查 nginx 配置"
    assert again["finished_at"] == first["finished_at"]

    # 非法终态在写入前拒绝
    with pytest.raises(ValueError):
        rstore.finish_runbook_run(rrb["id"], "running")


def test_list_runbooks_last_run_summary(base_db):
    a = _mk_runbook(name="A")
    _mk_runbook(name="B")
    rrb = rstore.create_runbook_run(a["id"])
    rstore.finish_runbook_run(rrb["id"], "success")
    listed = {r["name"]: r for r in rstore.list_runbooks()}
    assert listed["A"]["last_status"] == "success"
    assert listed["A"]["last_run_id"] == rrb["id"]
    assert listed["A"]["last_run_at"]
    assert listed["B"]["last_status"] is None


def test_reap_stale_marks_only_running(base_db):
    """重启收尸契约：滞留 running → stale_reaped；终态行不被动。"""
    a = _mk_runbook(name="A")
    b = _mk_runbook(name="B")
    stale = rstore.create_runbook_run(a["id"])
    done = rstore.create_runbook_run(a["id"])
    rstore.finish_runbook_run(done["id"], "success")

    n = rstore.reap_stale_runs()
    assert n == 1
    runs = {r["id"]: r for r in rstore.list_runbook_runs(a["id"])}
    assert runs[stale["id"]]["status"] == "stale_reaped"
    assert "后端重启" in runs[stale["id"]]["error"]
    assert runs[stale["id"]]["finished_at"]
    assert runs[done["id"]]["status"] == "success"
    # 收尸是终态写：再跑一遍没有新的 running 可收
    assert rstore.reap_stale_runs() == 0
    assert rstore.get_runbook(b["id"])["name"] == "B"


def test_reap_stale_respects_exclude_set(base_db):
    """排除集保护当前进程在途剧本：其 running 行不被收尸。"""
    a = _mk_runbook(name="A")
    b = _mk_runbook(name="B")
    ra = rstore.create_runbook_run(a["id"])
    rb = rstore.create_runbook_run(b["id"])
    rstore.reap_stale_runs(exclude_runbook_ids={a["id"]})
    statuses = {r["id"]: r["status"] for r in rstore.list_runbook_runs(a["id"])}
    statuses.update({r["id"]: r["status"] for r in rstore.list_runbook_runs(b["id"])})
    assert statuses[ra["id"]] == "running"
    assert statuses[rb["id"]] == "stale_reaped"


def test_external_input_stays_parameter_bound(base_db):
    """铁律守卫：含引号/分号/%/_ 的外部输入原样存取，表结构不受影响。"""
    hostile = "x'; DROP TABLE runbooks;-- %_% ' OR '1'='1"
    row = _mk_runbook(name=hostile, description="100% 纯文本_下划线")
    got = rstore.get_runbook(row["id"])
    assert got["name"] == hostile
    assert got["description"] == "100% 纯文本_下划线"
    # 表仍然活着，LIKE 通配符没有当成 SQL 执行
    assert rstore.get_runbook(row["id"]) is not None
    listed = rstore.list_runbooks()
    assert any(r["name"] == hostile for r in listed)
