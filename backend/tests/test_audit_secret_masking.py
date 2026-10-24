"""审计落库脱敏回归：内联口令不进审计库，且哈希链保持自洽。

守三件事：
1. command_raw / result 里的高置信凭据形态（mysql -p 内联、password= 等）落库前
   被 *** 掩码，result 带 secret_masked=1 标记 —— 掩码是诚实的，看得见被抹掉；
2. command_hash 与链式哈希都基于**脱敏后**文本：导出侧按存储的 command_raw
   重算 hash 必须能对账，verify() 全链校验必须 intact（脱敏不得破坏防篡改链）；
3. 无凭据语义的命令/结果原样落库，result 不带标记 —— 既有审计行为零变化。
"""
import sqlite3

import pytest

import database as db
from agent import audit


def _conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


@pytest.fixture
def wired(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    return conn


def test_inline_password_masked_and_chain_intact(wired):
    rec = audit.append_record(
        action="shell_exec", conclusion="executed",
        command_raw="mysql -u root -p123456 -e 'select 1'",
        result="exit=0 durationMs=120")
    assert "-p***" in rec["command_raw"]
    assert "123456" not in rec["command_raw"]
    assert "secret_masked=1" in rec["result"]
    assert "exit=0" in rec["result"]
    # 哈希基于脱敏后文本：按存储文本重算可对账
    assert rec["command_hash"] == audit.command_hash(rec["command_raw"])
    # 防篡改链不受脱敏影响
    report = audit.verify()
    assert report["intact"] is True
    assert report["total"] == 1


def test_result_secret_masked_with_marker(wired):
    rec = audit.append_record(
        action="shell_exec", conclusion="executed",
        command_raw="python manage.py dump --out x",
        result="connect password=hunter2 ok")
    assert "hunter2" not in rec["result"]
    assert "password=***" in rec["result"]
    assert "secret_masked=1" in rec["result"]
    assert audit.verify()["intact"] is True


def test_plain_record_unchanged_no_marker(wired):
    rec = audit.append_record(
        action="shell_exec", conclusion="executed",
        command_raw="systemctl restart nginx",
        result="exit=0 durationMs=123")
    assert rec["command_raw"] == "systemctl restart nginx"
    assert rec["result"] == "exit=0 durationMs=123"
    assert "secret_masked" not in rec["result"]
    assert audit.verify()["intact"] is True


def test_multiple_masked_records_chain_still_intact(wired):
    audit.append_record(action="shell_exec", conclusion="executed",
                        command_raw="mysql -pabc123 x", result="exit=0")
    audit.append_record(action="shell_exec", conclusion="executed",
                        command_raw="systemctl status nginx", result="exit=3")
    audit.append_record(action="shell_exec", conclusion="blocked",
                        command_raw="sshpass -pS3cret! rsync -a /s h:/d",
                        result="")
    report = audit.verify()
    assert report["intact"] is True
    assert report["total"] == 3
