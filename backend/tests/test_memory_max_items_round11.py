"""memory.max_items 的读取区间 —— 越界读数会把「修剪最旧记忆」变成「清空记忆池」。

链路：consciousness_tick 取 get_memory_settings()["max_items"] 喂给
mstore.trim_pools(max_items)，而后者 SQL 结尾是
`ORDER BY importance DESC, use_count DESC, created_at DESC LIMIT -1 OFFSET ?` ——
max_items=0 等于「一条都不跳过、无上限 retire」，该 kind 下全部生效记忆一次被打成
invalidated（v2 失效不物理删，可恢复，但召回当场归零）。
写入侧 Field(ge=10, le=500) 只管得住走接口的写：升级前的旧行与直接改库不受管。
所以读取侧兜住，并用 routes 的 Field 元数据卡区间（两边漂移本测试直接失败）。
"""
from pathlib import Path

import pytest

import database as db
from agent.memory import mstore


@pytest.fixture
def memdb():
    """固定同一个库文件 + 逐测试清表。

    database.py 的连接是线程本地的：init_db 换路径不会让已建立的连接重开，
    逐测试换 tmp 路径会让主线程连到已删除的旧文件（Windows 下挂起/幽灵数据）。
    这里与 test_memory_v2 共用同一个库文件，而不是再开一个新文件 —— 多开一个
    _tmp/*.db 只是多一份要清理的幽灵库。
    """
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    original = db._db_path
    db.init_db(str(base / "memv2-test.db"))
    conn = db._get_conn()
    for t in ("memories", "memory_audit", "memory_fts"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    mstore.reset_flags()
    yield
    mstore.reset_flags()
    try:
        db.set_app_setting("memory.max_items", "100")
    except Exception:
        pass
    if original:
        db.init_db(original)


def _read(monkeypatch, raw):
    monkeypatch.setattr(
        db, "get_app_setting",
        lambda key, default="": raw if key == "memory.max_items" else default,
    )
    return db.get_memory_settings()["max_items"]


@pytest.mark.parametrize("raw", ["0", "-1", "9", "501", "99999", "", "abc"])
def test_out_of_range_max_items_falls_back_to_default(monkeypatch, raw):
    assert _read(monkeypatch, raw) == 100


@pytest.mark.parametrize("raw", ["10", "100", "500"])
def test_in_range_max_items_read_as_written(monkeypatch, raw):
    assert _read(monkeypatch, raw) == int(raw)


def test_read_bounds_match_the_write_side_field(monkeypatch):
    """区间取自 routes 的 Field 元数据：收紧/放宽写入侧时本测试跟着走。"""
    from routes.settings import MemorySettingsRequest

    meta = MemorySettingsRequest.model_fields["max_items"].metadata
    ge = next(int(m.ge) for m in meta if getattr(m, "ge", None) is not None)
    le = next(int(m.le) for m in meta if getattr(m, "le", None) is not None)
    assert _read(monkeypatch, str(ge)) == ge
    assert _read(monkeypatch, str(le)) == le
    assert _read(monkeypatch, str(ge - 1)) == 100
    assert _read(monkeypatch, str(le + 1)) == 100


def test_dirty_row_cannot_retire_whole_pool(memdb):
    for i in range(3):
        mstore.add_memory(kind="semantic", content=f"语义记忆 {i}", source="test")
    db.set_app_setting("memory.max_items", "0")  # 升级前旧行 / 手工改库

    retired = mstore.trim_pools(db.get_memory_settings()["max_items"])
    assert retired == 0, "越界读数漏到修剪侧，整池记忆被批量失效"

    # 反证：0 正是未经读取侧兜住的读数会带来的后果（OFFSET 0 + LIMIT -1 = 全池 retire）
    assert mstore.trim_pools(0) == 3
    # v2 的 retire 是标记不是删除：行还在，只是退出召回
    assert len(mstore.list_memories(kind="semantic", status="invalidated")) == 3
