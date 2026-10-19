"""回归：host-profile 块不能按连接数无限增长（S14-06）。

块键是 `host-profile:{ssh_session_id}` —— 每次连接都是新 id，而 render_for_context
只读当前主机那一条，旧连接那条永远不会再被读到；库里原先没有任何清理钩子
（`host_profile.clear_cache` 在生产侧也没有调用方），同时 `list_blocks()` 每轮 run
全量扫一次这张表：用得越久，每轮的固定开销越大。

裁剪只按 host-profile 前缀动手，用户写的 user-preferences / agent-rules 一条都不该碰。
"""
import pytest

import database as db
from agent.memory import blocks
from agent.memory import mstore


@pytest.fixture
def tmp_db(tmp_path):
    # 换库必须成对关连接：线程本地池里留着旧文件的连接，读到的还是上一个库
    db._close_all_connections()
    db.init_db(str(tmp_path / "blocks-test.db"))
    yield
    db._close_all_connections()


def _make_profiles(n: int) -> None:
    for i in range(n):
        mstore.put_block(blocks.host_key(f"sess-{i:03d}"), f"env {i}", "probe")


def test_prune_keeps_newest_and_spares_user_blocks(tmp_db):
    _make_profiles(7)
    mstore.put_block(blocks.USER_PREFERENCES, "用户的偏好", "user")
    mstore.put_block(blocks.AGENT_RULES, "操作规则", "agent")

    removed = mstore.prune_blocks_by_prefix(blocks.HOST_PREFIX, 3)

    assert removed == 4
    keys = {b["key"] for b in mstore.list_blocks()}
    assert keys == (
        {blocks.host_key(k) for k in ("sess-004", "sess-005", "sess-006")}
        | {blocks.USER_PREFERENCES, blocks.AGENT_RULES}
    )
    assert mstore.get_block(blocks.USER_PREFERENCES)["value"] == "用户的偏好"

    # 被删的键不能把历史留成孤儿
    conn = mstore._conn()
    orphans = conn.execute(
        "SELECT h.key FROM memory_block_history h "
        "WHERE NOT EXISTS (SELECT 1 FROM memory_blocks b WHERE b.key = h.key)"
    ).fetchall()
    assert [o["key"] for o in orphans] == []


def test_prune_is_noop_under_the_cap(tmp_db):
    """反向锁：没到上限就一条都不该删 —— 裁剪不该把刚画像好的主机清空。"""
    _make_profiles(3)
    assert mstore.prune_blocks_by_prefix(blocks.HOST_PREFIX, 50) == 0
    assert len([b for b in mstore.list_blocks() if b["key"].startswith(blocks.HOST_PREFIX)]) == 3


def test_wrapper_bounds_by_the_module_cap(tmp_db):
    _make_profiles(blocks.HOST_PROFILE_KEEP + 5)
    removed = mstore.prune_blocks_by_prefix(blocks.HOST_PREFIX, blocks.HOST_PROFILE_KEEP)
    assert removed == 5
    left = [b for b in mstore.list_blocks() if b["key"].startswith(blocks.HOST_PREFIX)]
    assert len(left) == blocks.HOST_PROFILE_KEEP
