"""mark_schedule_run 的写入形状与「不传就不动」契约。

调度引擎每次触发都要回写 schedules 的 last_status / last_run_at / last_run_id，
而调用方的用法是分裂的（schedule_engine.py）：
- `:305` 起跑时三个一起传（running + started + run_id）；
- `:324/:340/:349` 收尾时只传状态 —— 此时 last_run_at/last_run_id 必须保持原值，
  面板上「上次执行」那一列才不会在每次跑完被抹成空。

原实现把这三列拆成三条 UPDATE（同事务、同一行、三个写操作 + 三次索引维护），
其中 updated_at 只在第一条里写。这里既钉「一条语句」这个形状，也钉住那个
「不传/传空串 = 不动」的哨兵语义 —— 后者是调用方唯一在用的形态，静默改掉会让
面板读数变空。
"""
from pathlib import Path

import pytest

import database as db


@pytest.fixture
def sch_db():
    """固定同一个库文件、逐用例清表：不借 tmp_workspace 的 rmtree
    （线程本地 keep-alive 连接还挂在文件上时删目录 = Windows 挂起，见
    test_ordering_tiebreak_family.py 的同一注脚）。
    """
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    original = db._db_path
    db._close_all_connections()
    db.init_db(str(base / "mark-schedule-run-test.db"))
    try:
        conn = db._get_conn()
        try:
            conn.execute("DELETE FROM schedules")
            conn.commit()
        finally:
            conn.close()
        yield
    finally:
        db._close_all_connections()
        if original:
            db.init_db(original)
        db._close_all_connections()


def _new_schedule(name: str = "每小时体检") -> str:
    return db.create_schedule({
        "name": name, "schedule_type": "interval", "interval_minutes": 60,
        "task_prompt": "systemctl status nginx",
    })["id"]


def _row(sid: str) -> dict:
    conn = db._get_conn()
    try:
        return dict(conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone())
    finally:
        conn.close()


class _Counting:
    """代理连接：数 UPDATE 语句的条数，其余透传。"""

    def __init__(self, conn):
        self._conn = conn
        self.updates = 0

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("UPDATE"):
            self.updates += 1
        return self._conn.execute(sql, *args)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        # 透传：池化连接的 close() 语义是「归还」，拦下来会把后面的用例饿死
        return self._conn.close()


def test_一次调用只发一条_UPDATE(sch_db, monkeypatch):
    sid = _new_schedule()
    counter = _Counting(db._get_conn())
    monkeypatch.setattr(db, "_get_conn", lambda: counter)

    db.mark_schedule_run(sid, "running", last_run_at="2026-10-08 09:00:00",
                         last_run_id="run-1")

    assert counter.updates == 1, (
        f"同一行同一事务发了 {counter.updates} 条 UPDATE：三列本可以一条写完")


def test_三个参数一起传时三列都落(sch_db):
    sid = _new_schedule()
    db.mark_schedule_run(sid, "running", last_run_at="2026-10-08 09:00:00",
                         last_run_id="run-1")
    row = _row(sid)
    assert row["last_status"] == "running"
    assert row["last_run_at"] == "2026-10-08 09:00:00"
    assert row["last_run_id"] == "run-1"
    assert row["updated_at"], "合并成一条 UPDATE 后 updated_at 仍要写"


def test_只传状态时上次执行时间与run_id保持原值(sch_db):
    """收尾那一支（schedule_engine.py:324/340/349）就在依赖这条。"""
    sid = _new_schedule()
    db.mark_schedule_run(sid, "running", last_run_at="2026-10-08 09:00:00",
                         last_run_id="run-1")

    db.mark_schedule_run(sid, "completed")

    row = _row(sid)
    assert row["last_status"] == "completed"
    assert row["last_run_at"] == "2026-10-08 09:00:00", "被缺省值抹平了，面板读数变空"
    assert row["last_run_id"] == "run-1"


def test_空串哨兵同样不动_这是调用方唯一的用法(sch_db):
    sid = _new_schedule()
    db.mark_schedule_run(sid, "running", last_run_at="2026-10-08 09:00:00",
                         last_run_id="run-1")

    db.mark_schedule_run(sid, "failed", last_run_at="", last_run_id="")

    row = _row(sid)
    assert row["last_status"] == "failed"
    assert row["last_run_at"] == "2026-10-08 09:00:00"
    assert row["last_run_id"] == "run-1"
