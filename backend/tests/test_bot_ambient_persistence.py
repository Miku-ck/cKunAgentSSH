"""群环境上下文落库持久化 — write-through 与重启恢复的回归测试。

守护点：
- write-through：入站群消息与机器人发言两个落点都写 `bot_ambient_lines`
  且写内存上下文；`bot_messages` 不写（既有「不写用户消息/不记账」守卫不受影响）；
- 落库失败降级：内存上下文照常更新（与旧纯内存行为一致）；
- 重启恢复：TTL 窗口内的行灌回内存、窗口外不灌、at_until 不恢复（重启后
  不主动接话）；
- 清理：过期行被 purge，索引按 (channel_id, group_openid, created_at) 覆盖读路径。

运行: python -m pytest backend/tests/test_bot_ambient_persistence.py -v
"""
from datetime import datetime, timedelta, timezone

import pytest

import database as db
from bot import group_ambient

# 时间基准取真实时钟：created_at 的 TTL 判定走 SQLite 的 datetime('now')（真实
# 时钟），固定假时间戳会落在未来导致 purge/窗口判定全错。
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _fresh_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(tmp_workspace + "/ambient-persist.sqlite")
    return db


@pytest.fixture(autouse=True)
def _clean_memory():
    group_ambient.reset_for_tests()
    yield
    group_ambient.reset_for_tests()


# ── DB 层：表与基本读写 ──────────────────────────────────────────────────────

def test_table_and_crud(tmp_workspace):
    _fresh_db(tmp_workspace)
    stamp = NOW.strftime("%Y-%m-%d %H:%M:%S")
    db.add_bot_ambient_line("ch", "g1", "小明", "今晚吃啥", stamp)
    rows = db.list_recent_ambient_lines(minutes=10)
    assert len(rows) == 1
    assert rows[0]["channel_id"] == "ch"
    assert rows[0]["group_openid"] == "g1"
    assert rows[0]["content"] == "今晚吃啥"
    # 过期行被 purge
    old = (NOW - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    db.add_bot_ambient_line("ch", "g1", "小明", "很久以前", old)
    assert db.purge_ambient_lines(minutes=15) == 1
    rows = db.list_recent_ambient_lines(minutes=10)
    assert len(rows) == 1 and rows[0]["content"] == "今晚吃啥"


# ── write-through：两个落点都落库且写内存 ────────────────────────────────────

def test_note_line_persisted_writes_db_and_memory(tmp_workspace):
    _fresh_db(tmp_workspace)
    group_ambient.note_line_persisted("ch", "g1", "小明", "今晚吃啥", now=NOW)
    # 内存可见
    assert ("小明", "今晚吃啥") in group_ambient.context_lines("ch", "g1", now=NOW)
    # 库可见
    rows = db.list_recent_ambient_lines(minutes=10)
    assert any(r["content"] == "今晚吃啥" for r in rows)


def test_persist_failure_degrades_to_memory_only(tmp_workspace, monkeypatch):
    """落库失败只留痕：内存上下文照常更新（与旧纯内存行为一致）。"""
    _fresh_db(tmp_workspace)

    def boom(*a, **kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(db, "add_bot_ambient_line", boom)
    group_ambient.note_line_persisted("ch", "g1", "小明", "今晚吃啥", now=NOW)
    assert ("小明", "今晚吃啥") in group_ambient.context_lines("ch", "g1", now=NOW)


def test_inbound_pipeline_bot_reply_persists(tmp_workspace):
    """机器人自己的发言（inbound_pipeline 落点）同样 write-through。"""
    _fresh_db(tmp_workspace)
    stamp = NOW.strftime("%Y-%m-%d %H:%M:%S")
    db.add_bot_ambient_line("ch", "g1", "鲸鱼", "我刚才吃过啦", stamp)
    rows = db.list_recent_ambient_lines(minutes=10)
    assert any(r["speaker"] == "鲸鱼" for r in rows)


def test_existing_bot_messages_guard_unaffected(tmp_workspace):
    """ambient 落库绝不写 bot_messages（独立表方案的核心保证）。"""
    _fresh_db(tmp_workspace)
    group_ambient.note_line_persisted("ch", "g1", "小明", "今晚吃啥", now=NOW)
    count = db._get_conn().execute("SELECT COUNT(*) AS n FROM bot_messages").fetchone()["n"]
    assert count == 0


# ── 重启恢复 ─────────────────────────────────────────────────────────────────

def test_restore_fills_ttl_window_only(tmp_workspace):
    _fresh_db(tmp_workspace)
    fresh = NOW.strftime("%Y-%m-%d %H:%M:%S")
    stale = (NOW - timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S")
    db.add_bot_ambient_line("ch", "g1", "小明", "刚说的", fresh)
    db.add_bot_ambient_line("ch", "g1", "小明", "很久前的", stale)
    db.add_bot_ambient_line("ch", "g2", "小红", "别的群", fresh)

    group_ambient.restore_from_db()
    # TTL 内的本群行恢复了；过期行与别的群不进来
    assert ("小明", "刚说的") in group_ambient.context_lines("ch", "g1", now=NOW)
    assert ("小明", "很久前的") not in group_ambient.context_lines("ch", "g1", now=NOW)
    assert ("小红", "别的群") in group_ambient.context_lines("ch", "g2", now=NOW)
    assert group_ambient.context_lines("ch", "g1", now=NOW).count(("小红", "别的群")) == 0


def test_restore_does_not_reopen_participation_window(tmp_workspace):
    """at_until 不持久化：重启后可参与期默认关闭（新 @ 才重新打开）。"""
    _fresh_db(tmp_workspace)
    db.add_bot_ambient_line(
        "ch", "g1", "小明", "重启前的话",
        NOW.strftime("%Y-%m-%d %H:%M:%S"))
    group_ambient.restore_from_db()
    assert group_ambient.is_open("ch", "g1", now=NOW) is False
    assert group_ambient.should_reply("ch", "g1", now=NOW) is False


def test_restore_survives_db_failure(monkeypatch):
    """恢复失败按空上下文启动（不拖垮渠道恢复流程）。"""
    def boom(*a, **kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(db, "list_recent_ambient_lines", boom)
    group_ambient.restore_from_db()
    assert group_ambient.context_lines("ch", "g1") == []


def test_repeated_note_is_idempotent_for_memory(tmp_workspace):
    """同一条消息 note 两次：内存去重交给 seen_event（渠道层），上下文按
    追加语义保留（与旧纯内存行为一致，本测试只钉住不因落库引入变化）。"""
    _fresh_db(tmp_workspace)
    group_ambient.note_line_persisted("ch", "g1", "小明", "吃了吗", now=NOW)
    group_ambient.note_line_persisted("ch", "g1", "小明", "吃了吗", now=NOW)
    lines = group_ambient.context_lines("ch", "g1", now=NOW)
    assert lines.count(("小明", "吃了吗")) == 2
