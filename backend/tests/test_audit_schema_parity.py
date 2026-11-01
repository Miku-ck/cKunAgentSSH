"""audit_log 两份建表 DDL 的结构性对账。

同一张审计表在两个地方各写了一遍：
  · 生产：database.init_db 内联的那段（真库里的形态）
  · 测试：agent/audit.py 的 _AUDIT_SCHEMA（全库 10+ 个审计测试靠它建表）

合不成一份 —— audit.py 顶部 `import database`，让 database 反向引用 audit 会成环，
而 init_db 站在启动关键路径上。既然是两份手抄，就必须有人保证它们真的同构：
以前只靠 audit.py 那句「同源副本」的注释自觉，索引名因此已经漂成了
`ix_audit_*`（生产）对 `idx_audit_*`（测试）。列漂了更糟 —— 审计测试对着一个
和生产长得不一样的表跑绿，等于零证明。

所以这里不用文本比对（改个空格就假红），而是各建一个库比结构。
"""

import sqlite3

import pytest

from agent import audit


def _table_info(conn, table):
    """(列名, 类型, notnull, 默认值, 主键序号) 全量，按列序。"""
    return [tuple(row) for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


def _index_shape(conn, table):
    """该表上手工建立的索引：{索引名: (唯一性, 索引列清单)}。"""
    out = {}
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    ).fetchall():
        info = conn.execute("PRAGMA index_info(%s)" % name).fetchall()
        out[name] = (bool(sql.upper().startswith("CREATE UNIQUE")), [row[2] for row in info])
    return out


@pytest.fixture
def prod_conn(base_db):
    """生产形态：走 init_db，和桌面端启动时建的是同一个库。"""
    conn = sqlite3.connect(base_db)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def test_schema_conn(tmp_workspace):
    """测试形态：只跑 audit._AUDIT_SCHEMA，即各审计测试 fixture 里的建表方式。"""
    import os

    conn = sqlite3.connect(os.path.join(tmp_workspace, "audit_only.db"))
    conn.executescript(audit._AUDIT_SCHEMA)
    try:
        yield conn
    finally:
        conn.close()


def test_audit_table_columns_match_production(prod_conn, test_schema_conn):
    """列名、类型、NOT NULL、默认值、主键序号逐位对齐。"""
    table = audit.AUDIT_TABLE
    assert _table_info(test_schema_conn, table) == _table_info(prod_conn, table), (
        "audit._AUDIT_SCHEMA 与 database.init_db 里的 audit_log 定义已漂移："
        "审计测试建的表不再等于生产表，它们跑绿不再构成生产的证明。"
    )


def test_audit_indexes_match_production(prod_conn, test_schema_conn):
    table = audit.AUDIT_TABLE
    assert _index_shape(test_schema_conn, table) == _index_shape(prod_conn, table), (
        "audit_log 的索引在两份 DDL 里不一致（名字或列不同）。查询按 run_id/target_id "
        "过滤是审计页的常态，测试库缺了这条索引就测不到生产形态的读路径。"
    )


def test_chain_columns_are_indexed_for_queries(prod_conn):
    """钉住「有索引」这件事本身，而不是钉住它出现在哪份 DDL 里。

    防的是另一种退化：有人把两份 DDL 里的索引一起删了，上两条对账照样全绿。
    ts / run_id / target_id / environment 是 audit_log 的四个查询维度。
    """
    covered = {col for _unique, cols in _index_shape(prod_conn, audit.AUDIT_TABLE).values()
               for col in cols}
    assert {"ts", "run_id", "target_id", "environment"} <= covered


def test_hash_column_is_unique(prod_conn):
    """链式签名的防重放闸：hash 必须带唯一约束，否则同一条审计能被塞两遍。

    列上写 UNIQUE 时 SQLite 建的是隐式索引（sqlite_autoindex_*，在 sqlite_master
    里没有 sql 文本），所以这条只能读 index_list，不能读上面那个按 sql 过滤的清单。
    """
    unique_cols = set()
    for _seq, name, is_unique, _origin, _partial in prod_conn.execute(
        "PRAGMA index_list(%s)" % audit.AUDIT_TABLE
    ).fetchall():
        if not is_unique:
            continue
        for info in prod_conn.execute("PRAGMA index_info(%s)" % name).fetchall():
            unique_cols.add(info[2])
    assert "hash" in unique_cols, "audit_log.hash 上的 UNIQUE 约束没了"
