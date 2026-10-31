"""计划时间窗：把「周末要去看展」里的时间词解析成可判定的窗口。

纯函数 + 固定基准时刻，全部可重放。
"""
from datetime import datetime, timedelta, timezone

import database as db
from bot import plan as plan_mod

# 本地时区用固定 +08:00 模拟，避免测试机时区差异
CST = timezone(timedelta(hours=8))
# 2026-10-28 是周三
WED = datetime(2026, 10, 28, 20, 0, tzinfo=CST)


def _window(text, base=WED):
    return plan_mod.plan_window(text, base)


def test_tomorrow_is_the_whole_next_day():
    start, end = _window("明天")
    assert start == datetime(2026, 10, 29, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 10, 30, 0, 0, tzinfo=CST)


def test_day_after_tomorrow():
    start, end = _window("后天")
    assert start == datetime(2026, 10, 30, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 10, 31, 0, 0, tzinfo=CST)


def test_tonight_starts_at_18_and_ends_at_midnight():
    start, end = _window("今晚")
    assert start == datetime(2026, 10, 28, 18, 0, tzinfo=CST)
    assert end == datetime(2026, 10, 29, 0, 0, tzinfo=CST)


def test_later_is_a_four_hour_window_from_now():
    start, end = _window("待会")
    assert start == WED
    assert end == WED + timedelta(hours=4)


def test_weekend_from_a_weekday_is_saturday_to_monday():
    start, end = _window("周末")
    assert start == datetime(2026, 10, 31, 0, 0, tzinfo=CST)   # 周六
    assert end == datetime(2026, 11, 2, 0, 0, tzinfo=CST)     # 周一 00:00


def test_weekend_on_saturday_still_covers_sunday():
    start, end = _window("周末", datetime(2026, 10, 31, 9, 0, tzinfo=CST))
    assert start == datetime(2026, 10, 31, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 11, 2, 0, 0, tzinfo=CST)


def test_weekend_on_sunday_collapses_to_today():
    """周日说「周末」，事情还在进行中，窗口只剩今天 —— 不能提前判成结束。"""
    start, end = _window("周末", datetime(2026, 11, 1, 15, 0, tzinfo=CST))
    assert start == datetime(2026, 11, 1, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 11, 2, 0, 0, tzinfo=CST)


def test_next_week_is_a_full_seven_days():
    start, end = _window("下周")
    assert start == datetime(2026, 11, 2, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 11, 9, 0, 0, tzinfo=CST)


def test_next_month_crosses_the_year_boundary():
    start, end = _window("下个月", datetime(2026, 12, 15, 10, 0, tzinfo=CST))
    assert start == datetime(2027, 1, 1, 0, 0, tzinfo=CST)
    assert end == datetime(2027, 2, 1, 0, 0, tzinfo=CST)


def test_unknown_or_empty_when_returns_none():
    assert _window("") is None
    assert _window("   ") is None
    assert _window("明年") is None


# ── 钩子表与数据访问（spec 2026-10-31-plan-followup）──


def _persona():
    return db.create_bot_persona(name="晚风")


def test_add_plan_hook_supersedes_the_same_text(base_db):
    """同一件事改了时间：旧钩子让位，新的才是对的。"""
    persona = _persona()
    first = db.add_plan_hook(
        persona["id"], None, None, "看展", "周末",
        "2026-10-31 00:00:00", "2026-11-02 00:00:00", "2026-10-28 12:00:00",
    )
    second = db.add_plan_hook(
        persona["id"], None, None, "看展", "下周",
        "2026-11-02 00:00:00", "2026-11-09 00:00:00", "2026-10-30 12:00:00",
    )
    assert first != second
    row = db.due_plan_hook(persona["id"], "2026-11-10 00:00:00", "2026-11-01 00:00:00")
    assert row is not None and row["id"] == second


def test_due_plan_hook_respects_window_and_followup_horizon(base_db):
    persona = _persona()
    db.add_plan_hook(
        persona["id"], None, None, "看展", "周末",
        "2026-10-31 00:00:00", "2026-11-02 00:00:00", "2026-10-28 12:00:00",
    )
    # 窗口还没结束
    assert db.due_plan_hook(persona["id"], "2026-11-01 23:00:00", "2026-10-31 00:00:00") is None
    # 窗口结束后 12h：可回溯
    assert db.due_plan_hook(persona["id"], "2026-11-02 12:00:00", "2026-10-31 00:00:00")
    # 超出回溯期（36h 前的边界之外）
    assert db.due_plan_hook(persona["id"], "2026-11-04 12:00:00", "2026-11-03 00:00:00") is None


def test_mark_plan_asked_is_one_way_and_one_shot(base_db):
    persona = _persona()
    hook = db.add_plan_hook(
        persona["id"], None, None, "看展", "周末",
        "2026-10-31 00:00:00", "2026-11-02 00:00:00", "2026-10-28 12:00:00",
    )
    assert db.mark_plan_asked(hook, "2026-11-02 01:00:00") is True
    assert db.mark_plan_asked(hook, "2026-11-02 02:00:00") is False, "只问一次"
    assert db.due_plan_hook(persona["id"], "2026-11-02 01:00:00", "2026-10-31 00:00:00") is None


def test_expire_plan_hooks_only_touches_overdue_pending(base_db):
    persona = _persona()
    db.add_plan_hook(
        persona["id"], None, None, "看展", "今晚",
        "2026-10-28 18:00:00", "2026-10-29 00:00:00", "2026-10-28 12:00:00",
    )
    fresh = db.add_plan_hook(
        persona["id"], None, None, "交房租", "下个月",
        "2026-11-01 00:00:00", "2026-12-01 00:00:00", "2026-10-28 12:00:00",
    )
    assert db.expire_plan_hooks("2026-10-30 00:00:00") == 1
    assert db.due_plan_hook(persona["id"], "2026-10-30 01:00:00", "2026-10-29 00:00:00") is None
    assert db.due_plan_hook(persona["id"], "2026-12-02 00:00:00", "2026-11-30 00:00:00")["id"] == fresh

