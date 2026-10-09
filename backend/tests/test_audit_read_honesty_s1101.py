# -*- coding: utf-8 -*-
"""S11-01 / S11-05 回归：审计读侧的诚实性 + 这四个端点第一次有路由级测试。

S11-01 的原缺陷：`query`/`count` 把任何 DB 异常都吞成空列表 / 0，于是审计页在锁冲突、
坏盘、schema 损坏时渲染出一个「一条记录都没有」的界面 —— 与轮转清空后的观感一模一样。
审计证据「没读到」被冒充成「确认没有」，正是合规场景里最不能含糊的一处。

分界线只有一条：**表还没建**才是真的没有；其余照抛，由路由回 500，页面自有
`加载审计记录失败` 的通道。
"""
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database as db
from agent import audit
from routes.audit import router as audit_router


def _conn(with_table=True):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if with_table:
        conn.executescript(audit._AUDIT_SCHEMA)
    return conn


@pytest.fixture
def wired(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    app = FastAPI()
    app.include_router(audit_router, prefix="/api")
    return TestClient(app), conn


def _append(n=2):
    for i in range(n):
        audit.append_record(action="shell_exec", conclusion="executed",
                            target_type="ssh", target_id="sess-1",
                            command_raw=f"echo {i}")


def test_records_read_back_real_rows(wired):
    """S11-05：此前 /audit/records 没有任何 HTTP 级测试。"""
    client, _ = wired
    _append(3)
    resp = client.get("/api/audit/records")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    # 倒序：最新那条在前
    assert body["items"][0]["id"] > body["items"][-1]["id"]


def test_filter_set_is_consistent_between_items_and_total(wired):
    client, _ = wired
    _append(2)
    audit.append_record(action="file_write", conclusion="blocked",
                        target_type="ssh", target_id="sess-1", command_raw="rm -rf /")
    body = client.get("/api/audit/records", params={"action": "file_write"}).json()
    assert body["total"] == 1, body
    assert len(body["items"]) == 1


def test_missing_table_reads_as_honest_empty(monkeypatch):
    """首次运行前表还不存在 —— 这一种「空」是真的没有，不该报错。"""
    conn = _conn(with_table=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    app = FastAPI()
    app.include_router(audit_router, prefix="/api")
    client = TestClient(app)
    resp = client.get("/api/audit/records")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "total": 0}


@pytest.mark.parametrize("fail", [
    sqlite3.OperationalError("database is locked"),
    sqlite3.OperationalError("disk I/O error"),
])
def test_read_failure_is_500_not_an_empty_list(wired, monkeypatch, fail):
    client, _ = wired
    _append(1)

    def _boom():
        raise fail

    monkeypatch.setattr(db, "_get_conn", _boom)
    resp = client.get("/api/audit/records")
    assert resp.status_code == 500, resp.text
    assert "读取失败" in resp.json()["detail"]
    # 不把 sqlite 原始串端给用户（S0-24 同口径）
    assert "locked" not in resp.text and "I/O" not in resp.text


def test_count_raises_instead_of_reporting_zero(wired, monkeypatch):
    """count 单独也钉一次：0 与「读不到」必须是两件事。"""
    client, conn = wired
    _append(2)

    class _Broken:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_get_conn", lambda: _Broken())
    with pytest.raises(sqlite3.OperationalError):
        audit.count()
    assert client.get("/api/audit/records").status_code == 500


# ── 同族第二处：/audit/export ─────────────────────────────────────────────────
# 导出比列表更重：产物是要交到评估方手里的文件。原先 export_rows 把任何异常折成
# 空列表 ⇒ 交付一份看起来「审计没有记录」的 CSV，且与轮转清空后的导出无法区分；
# 而空表的表头还退化成 ["id"] 一列，那份文件结构上根本不是审计日志。

def test_export_rows_raises_instead_of_returning_empty(monkeypatch):
    def _locked():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "_get_conn", _locked)
    with pytest.raises(sqlite3.OperationalError):
        audit.export_rows()


def test_export_rows_treats_absent_table_as_genuinely_empty(monkeypatch):
    conn = _conn(with_table=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    assert audit.export_rows() == []


def test_export_fails_loudly_instead_of_shipping_an_empty_csv(wired, monkeypatch):
    import routes.audit as ra

    def _locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ra, "export_rows", _locked)
    client, _c = wired
    resp = client.get("/api/audit/export")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "导出失败" in detail
    assert "locked" not in detail, "不把驱动原始串端给用户（S0-24 同口径）"


def test_export_of_empty_table_still_carries_the_full_header(wired):
    client, _c = wired
    resp = client.get("/api/audit/export")
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    assert text.splitlines()[0].split(",") == list(audit.AUDIT_COLUMNS)


def test_export_keeps_chain_columns_with_rows(wired):
    client, _c = wired
    _append(3)
    lines = client.get("/api/audit/export").content.decode("utf-8-sig").strip().splitlines()
    assert len(lines) == 4  # 表头 + 3 条
    header = lines[0].split(",")
    assert {"hash", "prev_hash", "run_id"} <= set(header)


def test_column_constants_equal_the_real_table(wired):
    """INSERT 用 _INSERT_COLUMNS、导出表头用 AUDIT_COLUMNS —— 两份清单必须真的等于表的列。

    这两处原先各抄一遍 14 个列名（append_record 与封缄各一份，导出再抄第三份），
    收敛成常量后由这条守卫对账：schema 加了列而常量没改，导出表头就会和 SELECT *
    错位，测试当场变红。
    """
    client, conn = wired
    _append(1)
    rec = conn.execute(f"SELECT * FROM {audit.AUDIT_TABLE}").fetchone()
    assert tuple(rec.keys()) == audit.AUDIT_COLUMNS

    written = audit.append_record(
        action="shell_exec", conclusion="executed", target_id="sess-1", command_raw="ls",
    )
    assert set(written) - {"id", "created_at"} == set(audit._INSERT_COLUMNS)
