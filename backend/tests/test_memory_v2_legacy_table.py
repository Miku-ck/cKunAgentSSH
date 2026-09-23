"""老库升级路径回归：v1 三层记忆的 `memories` 表会顶掉 v2 建表。

v1（初始提交 f6168bd）的三层记忆表就叫 `memories`，列是 id/mode/content/…，没有
kind；v2 的 `CREATE TABLE IF NOT EXISTS memories` 遇到同名旧表静默跳过，于是缺的是
整张表的列。原实现在这里只降级到"少建几个索引"并打一行 warning，真相是此后每一条
v2 读写都抛 "no such column: kind" —— v2 记忆在所有升级库上整体失效。

测试全部在自管的临时 conn 上跑（`_init_memory_v2` 收 conn 参数），不碰本模块其它
测试共用的 memv2-test.db。
"""
import sqlite3

import pytest

import database as db

_V1_DDL = """
    CREATE TABLE memories (
        id              TEXT PRIMARY KEY,
        mode            TEXT NOT NULL,
        content         TEXT NOT NULL,
        source_conv_id  TEXT,
        last_used_at    TEXT NOT NULL DEFAULT (datetime('now')),
        use_count       INTEGER DEFAULT 1,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_V1_AGENT_DDL = """
    CREATE TABLE agent_memories (
        id TEXT PRIMARY KEY, type TEXT, mode TEXT, content TEXT,
        source_conv_id TEXT, agent_run_id TEXT, embedding TEXT,
        embedding_src TEXT, importance REAL, last_used_at TEXT,
        use_count INTEGER, created_at TEXT
    )
"""


def _legacy_conn(path):
    """造一个"升级前"的库：v1 memories 空表 + 有存量的 agent_memories + 被 poison 的标记。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_V1_DDL)
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(_V1_AGENT_DDL)
    for i in range(12):
        conn.execute(
            "INSERT INTO agent_memories(id,type,mode,content,importance,use_count,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"am-{i}", "semantic", "global", f"主机 A 的 nginx 配置在 /etc/nginx/{i}.conf",
             0.6, i, "2026-09-01 00:00:00"),
        )
    conn.execute("INSERT INTO app_settings(key,value) VALUES('memory_v2.migrated','1')")
    conn.commit()
    return conn


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_legacy_table_is_parked_not_silently_reused(tmp_path):
    """旧表改名保留，memories 换成真正的 v2 形状（缺 kind 才是问题根因）"""
    conn = _legacy_conn(str(tmp_path / "legacy.db"))
    db._init_memory_v2(conn)

    assert "kind" in _cols(conn, "memories"), "v2 表没建起来，读写仍会抛 no such column"
    assert "memories_v1_layers" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }, "旧表被删了而不是保留"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 12


def test_poisoned_migrated_flag_is_retried_once(tmp_path):
    """标记=1 但迁移从没成功过的库：清标记重跑，存量搬进 v2 并投 backfill job"""
    conn = _legacy_conn(str(tmp_path / "retry.db"))
    db._init_memory_v2(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE source='migration'"
    ).fetchone()[0] == 12
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_jobs WHERE kind='backfill_reconcile'"
    ).fetchone()[0] == 2  # 每 10 条一批


def test_migrated_memories_are_searchable(tmp_path):
    """搬进来就得搜得到：检索只走 memory_fts，直连 INSERT 不同步侧表=列得出搜不到"""
    conn = _legacy_conn(str(tmp_path / "fts.db"))
    db._init_memory_v2(conn)

    assert conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 12
    toks = conn.execute("SELECT tok FROM memory_fts LIMIT 1").fetchone()[0]
    assert "nginx" in toks.split()


def test_second_run_is_idempotent(tmp_path):
    """再跑一遍不得重复搬运、不得改名、不得报错"""
    conn = _legacy_conn(str(tmp_path / "twice.db"))
    db._init_memory_v2(conn)
    names_before = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    db._init_memory_v2(conn)

    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 12
    assert conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 12
    assert "memories_v1_layers" in names_before
    assert "kind" in _cols(conn, "memories")


def test_fresh_db_takes_the_legacy_path_nowhere(tmp_path):
    """新库（无 memories 表）不受改名分支影响，v2 表照常建"""
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(_V1_AGENT_DDL)

    db._init_memory_v2(conn)

    assert "kind" in _cols(conn, "memories")
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_v1_layers'"
    ).fetchone()


def test_nonempty_legacy_rows_survive_the_rename(tmp_path):
    """旧表里有行也必须原样保留 —— 这是用户的既有数据，改名不是删除的借口"""
    conn = _legacy_conn(str(tmp_path / "data.db"))
    for i in range(3):
        conn.execute(
            "INSERT INTO memories(id,mode,content,created_at) VALUES (?,?,?,?)",
            (f"v1-{i}", "global", f"三层记忆遗留内容 {i}", "2026-08-01 00:00:00"),
        )
    conn.commit()

    db._init_memory_v2(conn)

    rows = conn.execute(
        "SELECT id, mode, content FROM memories_v1_layers ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == ["v1-0", "v1-1", "v1-2"]
    assert rows[0]["content"] == "三层记忆遗留内容 0"


@pytest.mark.parametrize("missing", ["status", "pinned"])
def test_index_failure_on_bizarre_shape_does_not_abort(tmp_path, missing):
    """有 kind 但缺别的列的第三种形状：不能让 init_db 崩，索引建不上要能吞。

    缺 kind 不在此列 —— 那就是 v1 旧表，走改名重建（上面的用例已覆盖）。
    """
    conn = _legacy_conn(str(tmp_path / f"odd-{missing}.db"))
    conn.execute("DROP TABLE memories")
    keep = {"kind", "status", "pinned"} - {missing}
    cols = ", ".join(f"{c} TEXT" for c in sorted(keep))
    conn.execute(f"CREATE TABLE memories (id TEXT PRIMARY KEY, {cols})")
    conn.execute("INSERT INTO memories(id,kind) VALUES('x','semantic')"
                 if "kind" in keep else "INSERT INTO memories(id) VALUES('x')")
    conn.commit()

    db._init_memory_v2(conn)  # 不抛即通过

    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_v1_layers'"
    ).fetchone(), "有 kind 就不是 v1 旧表，不该被改名"
