# -*- coding: utf-8 -*-
"""审计归档轮转（archive_and_clear）回归测试。

守护点：
- 清空不是裸删除：全量记录先写 CSV 归档，live 表只剩一条封缄记录（新链 genesis）；
- 轮转后 verify() 对 live 链自洽（封缄记录 prev_hash=""），继续追加仍完好；
- 空表幂等：archived=0，不动库不写文件；
- 归档写盘失败发生在删除之前 → 事务回滚，链原样保留（无丢数据窗口）；
- 封缄记录的环境归属跟着被清空的证据走（S11-04）：碰过 prod 的轮转必须在
  prod 视图可见，不能凭空消失在 test 里。
"""
import builtins
import csv
import sqlite3

import pytest

import database as db
from agent import audit


def _audit_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


def _append_n(conn, n, environment=None):
    for i in range(n):
        audit.append_record(
            action="shell_exec", conclusion="executed",
            target_type="ssh", target_id="sess-1",
            command_raw=f"echo {i}",
            environment=environment,
        )


@pytest.fixture
def audit_env(monkeypatch, tmp_workspace):
    conn = _audit_conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    return conn, tmp_workspace


def test_rotate_archives_seals_and_chain_intact(audit_env):
    conn, ws = audit_env
    _append_n(conn, 3)
    assert audit.verify()["intact"] and audit.verify()["total"] == 3

    r = audit.archive_and_clear(archive_dir=ws)
    assert r["archived"] == 3
    import os
    assert os.path.isfile(r["file"])

    # 归档 CSV：3 行数据 + 链字段完整（末行 hash 与返回的旧链尾一致）
    with open(r["file"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3
    assert rows[-1]["hash"] == r["last_hash"]
    # 归档可离线重放验链：prev_hash 逐条衔接
    assert rows[0]["prev_hash"] == ""
    assert all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, 3))

    # live 表只剩封缄记录，链自洽
    v = audit.verify()
    assert v["intact"] is True and v["total"] == 1
    seal = conn.execute("SELECT * FROM audit_log").fetchone()
    assert seal["action"] == "audit_cleared"
    assert seal["prev_hash"] == ""
    assert f"archived=3" in seal["result"] and r["last_hash"] in seal["result"]

    # 封缄后继续追加，链依旧完好（新记录挂到封缄之后）
    _append_n(conn, 2)
    v2 = audit.verify()
    assert v2["intact"] is True and v2["total"] == 3


def test_rotate_empty_table_noop(audit_env):
    conn, ws = audit_env
    r = audit.archive_and_clear(archive_dir=ws)
    assert r == {"archived": 0, "file": "", "last_hash": "", "seal_hash": ""}
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_rotate_archive_write_failure_keeps_chain(audit_env, monkeypatch):
    """归档写盘失败必须发生在 DELETE 之前并整体回滚 — 记录一条不少、链完好。"""
    conn, ws = audit_env
    _append_n(conn, 2)

    real_open = builtins.open

    def boom(path, *a, **k):
        if "audit_archive_" in str(path):
            raise OSError("disk full (simulated)")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", boom)
    with pytest.raises(OSError):
        audit.archive_and_clear(archive_dir=ws)

    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2
    v = audit.verify()
    assert v["intact"] is True and v["total"] == 2


def test_seal_environment_follows_cleared_evidence(audit_env):
    """S11-04：对 prod 主机做过轮转，prod 过滤视图必须看得到「谁清的库」。"""
    conn, ws = audit_env
    _append_n(conn, 2, environment="prod")
    audit.archive_and_clear(archive_dir=ws)

    seal = conn.execute("SELECT * FROM audit_log").fetchone()
    assert seal["action"] == "audit_cleared"
    assert seal["environment"] == "prod"
    # 封缄仍在 live 链上自洽（环境列参与 canonical，别把哈希算错）
    v = audit.verify()
    assert v["intact"] is True and v["total"] == 1
    # prod 视图查得到，test 视图不该被这条冒充
    assert audit.query(environment="prod")[0]["action"] == "audit_cleared"
    assert audit.query(environment="test") == []


def test_seal_environment_defaults_when_only_test_rows(audit_env):
    conn, ws = audit_env
    _append_n(conn, 1, environment="test")
    audit.archive_and_clear(archive_dir=ws)
    assert conn.execute("SELECT environment FROM audit_log").fetchone()[0] == "test"


def test_seal_environment_is_monotone_on_mixed_rows():
    """判据是「任一行为 prod」而非众数/尾行：混合轮转宁可多一条也不能少一条。"""
    assert audit._seal_environment([{"environment": "test"}]) == "test"
    assert audit._seal_environment([{"environment": "test"}] * 9 + [{"environment": "prod"}]) == "prod"
    # 缺键/空值不该被当成 prod（append_record 落库前已兜默认，这里是防御性形状）
    assert audit._seal_environment([{}, {"environment": None}]) == "test"
