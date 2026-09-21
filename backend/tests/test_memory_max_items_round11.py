"""memory.max_items 的读取区间 —— 越界读数会把「清理最旧记忆」变成「清空记忆」。

链路：consciousness_tick._prune_stale_memories 取 get_memory_settings()["max_items"]
喂给 db.prune_agent_memories(max_count=...)，而后者 SQL 结尾是
`ORDER BY last_used_at DESC, use_count DESC LIMIT -1 OFFSET ?` ——
max_count=0 等于「一条都不跳过、无上限删除」，该 mode/type 下全部记忆一次删空。
写入侧 Field(ge=10, le=500) 只管得住走接口的写：升级前的旧行与直接改库不受管，
而记忆设置没有任何面板可改回来。所以读取侧兜住，并用 routes 的 Field 元数据卡区间
（两边漂移本测试直接失败）。
"""
from pathlib import Path

import pytest

import database as db


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


def test_dirty_row_cannot_wipe_memories(tmp_workspace):
    original = db._db_path
    db._thread_local.__dict__.clear()
    db.init_db(str(Path(tmp_workspace) / "t.db"))
    try:
        for i in range(3):
            db.add_agent_memory(mode="ssh", mem_type="episodic", content=f"m{i}")
        db.set_app_setting("memory.max_items", "0")  # 升级前旧行 / 手工改库

        deleted = db.prune_agent_memories(
            mode="ssh", mem_type="episodic",
            max_count=db.get_memory_settings()["max_items"],
        )
        assert deleted == 0, "越界读数漏到清理侧，记忆被批量删除"

        # 反证：0 正是未经读取侧兜住的读数会带来的后果（OFFSET 0 + LIMIT -1 = 全删）
        assert db.prune_agent_memories(mode="ssh", mem_type="episodic", max_count=0) == 3
    finally:
        db._thread_local.__dict__.clear()
        db.init_db(original)
