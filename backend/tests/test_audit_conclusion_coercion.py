"""非法审计结论不得静默冒充「已执行」。

`conclusion` 是审计链里「这件事到底成没成」的唯一判词，而 `append_record` 过去对非法值
的处理是**静默改写成 `executed`** —— 也就是往一条链式签名、声称不可篡改的记录里写下
「这条命令执行过了」。这不是假想：`answered`/`skipped`（澄清向导的两个终态）加进调用点
时没加进枚举，用户的决策语义在审计链里就此消失，且没有任何报警。

改后的契约（三件事同时成立）：
1. 枚举列仍然有界 —— 脏值不直接进列，筛选与导出不会多出没人认领的桶；
2. 原始结论留在 `result` 里 —— 它在 `_canonical()` 覆盖的哈希范围内，记录自己说明自己
   是被改写过的，事后能追责到「谁写了个不在枚举里的结论」；
3. 记一条 warning —— 让「加新值不加枚举」这件事在开发期就看得见，而不是等审计页对不上。

「审计失败不阻塞 Agent」是本模块的既有契约（写库异常只记 warning 返回 None），所以这里
不抛错：抛出去会让一次分类失误变成运行中断。三份清单（枚举 / 审计页下拉 / 安全文档 §6）
的同源检查在 `test_ai_loop_round13c.py::test_audit_conclusion_四处清单同源`，不在此重复。
"""
import logging
import sqlite3

import pytest

import database as db
from agent import audit


def _audit_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


@pytest.fixture
def audit_conn(monkeypatch):
    conn = _audit_conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    return conn


def _last(conn):
    return conn.execute(
        "SELECT conclusion, result FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()


def test_非法结论落枚举内有界值但保留原值(audit_conn, caplog):
    with caplog.at_level(logging.WARNING):
        audit.append_record(
            action="shell_exec", conclusion="answered_next_turn",
            target_id="run1", result="澄清向导返回",
        )
    row = _last(audit_conn)
    assert row["conclusion"] == "executed", "脏值不该原样进枚举列"
    assert "answered_next_turn" in row["result"], (
        f"改写不留原始值，审计里就只剩一句「已执行」：{row['result']!r}")
    assert any("answered_next_turn" in m for m in caplog.messages), (
        "静默改写正是这次要消掉的行为")


def test_合法结论不被加注(audit_conn):
    """反向锁：留痕只发生在改写时，正常记录的内容不能被这条逻辑改动。"""
    for value in ("denied", "answered", "skipped", "released", "revoked"):
        audit.append_record(action="shell_exec", conclusion=value,
                            target_id="run1", result="ok")
        row = _last(audit_conn)
        assert row["conclusion"] == value
        assert row["result"] == "ok", f"合法值被加了改写标记：{row['result']!r}"


def test_空结论也被当作非法值留痕(audit_conn):
    """`""` 不是合法结论，也不该被 falsy 判定悄悄当成 executed 的正牌值。"""
    audit.append_record(action="shell_exec", conclusion="", target_id="run1", result="x")
    row = _last(audit_conn)
    assert row["conclusion"] == "executed"
    assert "conclusion_raw=" in row["result"]


def test_原始值里的换行被压平且长度受限(audit_conn):
    """result 会进审计页与导出，原始结论来自调用点字面量 —— 不能带多行/超长内容。"""
    noisy = "blocked\n\r  by_newline_rule   " + "x" * 200
    audit.append_record(action="shell_exec", conclusion=noisy, target_id="run1", result="x")
    row = _last(audit_conn)
    tail = row["result"].split("conclusion_raw=", 1)[1]
    assert "\n" not in tail and "\r" not in tail
    assert len(tail) <= 60
