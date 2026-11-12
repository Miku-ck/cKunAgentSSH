"""会话消息读取的守卫：get_messages 的分页窗口与同秒排序。

背景：get_messages 外层 `ORDER BY rowid` 引用派生表不存在的列 —— 派生表没有
隐式 rowid，SQLite 3.51 实测整条查询报 "no such column: rowid"（此前无任何
测试覆盖该函数）。修复后 rowid 由内层显式投影为 msg_rowid。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    database.init_db(str(tmp_path / "t.db"))
    yield


def test_get_messages_返回最新N条且按时间正序(db):
    c = database.create_conversation("t")
    for i in range(5):
        database.add_message(c["id"], "user", f"m{i}")

    got = database.get_messages(c["id"], limit=3)
    assert [m["content"] for m in got] == ["m2", "m3", "m4"]


def test_get_messages_同秒消息按插入序回放(db):
    c = database.create_conversation("t")
    # _now() 只到秒：同批写入 created_at 必然并列，乱序即暴露 rowid 兜底失效
    first = database.add_message(c["id"], "user", "先发")
    second = database.add_message(c["id"], "assistant", "后发")

    got = database.get_messages(c["id"])
    assert [m["id"] for m in got] == [first["id"], second["id"]]
