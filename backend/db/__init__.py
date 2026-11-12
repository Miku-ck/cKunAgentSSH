"""数据访问层 — 按领域拆分的 repository 包。

公共入口仍是 backend/database.py（再导出），本包供新代码按需引用。
连接层与工具函数从 database.py 原样复制（见 docs/structure-refactor-plan.md 阶段 1）。
"""
from db._conn import (  # noqa: F401
    _assert_sql_ident,
    _assert_sql_ident_list,
    _close_all_connections,
    _get_conn,
    init_db,
)
from db._util import _new_id, _now  # noqa: F401
from db.conversations import *  # noqa: F401,F403
from db.experts import *  # noqa: F401,F403
from db.plugins import *  # noqa: F401,F403
from db.skills import *  # noqa: F401,F403
