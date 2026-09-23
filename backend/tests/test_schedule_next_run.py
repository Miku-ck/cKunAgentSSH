"""compute_next_run 契约测试 — 定时任务"下次执行时间"计算。

历史缺陷（本轮修复，防回归）：
- daily/weekly 旧实现无条件从"明天/下周"起步、不与 now 比较 → 今天时刻
  还没到时也晚一天（daily 23:00 在 10:00 触发算出明天 23:00）。
- `time_hour or 9` 把 0 点任务静默挪到 9 点（路由校验明确允许 0）。
不变量：返回值恒 > now（或 == 触发边界语义下的下一周期）。
"""
from datetime import datetime

from agent.schedule_engine import compute_next_run

# now = 2026-09-18 02:00 UTC = 本地 10:00 (UTC+8)，周五（weekday=4）
NOW = datetime(2026, 9, 18, 2, 0, 0)


def _d(s: str) -> str:
    """把 'MM-DD HH:MM' 补成年份便于断言（测试期均为 2026-09/10）。"""
    return f"2026-{s}"


# ── daily ──────────────────────────────────────────────
def test_daily_today_not_passed():
    # 今天 23:00 本地（15:00 UTC）还没到 → 取今天
    assert compute_next_run({"schedule_type": "daily", "time_hour": 23, "time_minute": 0}, NOW) == _d("09-18 15:00:00")


def test_daily_today_passed():
    # 今天 01:00 本地（17:00 UTC 前一天）已过 → 明天
    assert compute_next_run({"schedule_type": "daily", "time_hour": 1, "time_minute": 0}, NOW) == _d("09-18 17:00:00")


def test_daily_exact_now_rolls_to_tomorrow():
    # cand == now（今天 10:00 本地 = 02:00 UTC）→ 不取当前时刻，取明天
    assert compute_next_run({"schedule_type": "daily", "time_hour": 2, "time_minute": 0}, NOW) == _d("09-18 18:00:00")


def test_daily_midnight_zero_not_swallowed():
    # 回归：`time_hour or 9` 会把 0 点挪到 9 点
    assert compute_next_run({"schedule_type": "daily", "time_hour": 0, "time_minute": 0}, NOW) == _d("09-18 16:00:00")


def test_daily_rolls_monotonically():
    # 连续触发后 next 严格 +1 天（无跳周、无停滞）
    d = NOW
    seq = []
    s = {"schedule_type": "daily", "time_hour": 9, "time_minute": 0}
    for _ in range(3):
        n = datetime.strptime(compute_next_run(s, d), "%Y-%m-%d %H:%M:%S")
        seq.append(n)
        d = n
    assert seq == sorted(seq) and len(set(seq)) == 3


# ── weekly ─────────────────────────────────────────────
def test_weekly_target_today_not_passed():
    # 今天是目标周（周五）且 23:00 未到 → 取今天
    s = {"schedule_type": "weekly", "weekday": 4, "time_hour": 23, "time_minute": 0}
    assert compute_next_run(s, NOW) == _d("09-18 15:00:00")


def test_weekly_target_today_passed():
    # 今天是目标周但 00:00 已过 → 下下周（本地 9-25 00:00 = UTC 9-24 16:00）
    s = {"schedule_type": "weekly", "weekday": 4, "time_hour": 0, "time_minute": 0}
    assert compute_next_run(s, NOW) == _d("09-24 16:00:00")


def test_weekly_target_future_weekday():
    # 下周一 09:00 本地 = 01:00 UTC
    s = {"schedule_type": "weekly", "weekday": 0, "time_hour": 9, "time_minute": 0}
    assert compute_next_run(s, NOW) == _d("09-21 01:00:00")


def test_weekly_midnight_zero_not_swallowed():
    # 回归：0 点不被 or 吞掉（下周一 00:00 本地 = UTC 9-20 16:00）
    s = {"schedule_type": "weekly", "weekday": 0, "time_hour": 0, "time_minute": 0}
    assert compute_next_run(s, NOW) == _d("09-20 16:00:00")


# ── monthly ────────────────────────────────────────────
def test_monthly_today_not_passed():
    s = {"schedule_type": "monthly", "day_of_month": 18, "time_hour": 23, "time_minute": 0}
    assert compute_next_run(s, NOW) == _d("09-18 15:00:00")


def test_monthly_day31_clamped():
    # 9 月只有 30 天 → 31 号钳到 30 号；9-30 00:00 本地未到 → 取 9-30（UTC 9-29 16:00）
    s = {"schedule_type": "monthly", "day_of_month": 31, "time_hour": 0, "time_minute": 0}
    assert compute_next_run(s, NOW) == _d("09-29 16:00:00")


# ── interval / 兜底 ────────────────────────────────────
def test_interval_simple():
    assert compute_next_run({"schedule_type": "interval", "interval_minutes": 30}, NOW) == _d("09-18 02:30:00")


def test_interval_zero_falls_back_safe():
    # 0 是危险值（每 tick 触发）→ 落 60（路由另有 ge=1 校验，此为最后防线）
    assert compute_next_run({"schedule_type": "interval", "interval_minutes": 0}, NOW) == _d("09-18 03:00:00")


def test_unknown_type_falls_back_one_hour():
    assert compute_next_run({"schedule_type": "bogus"}, NOW) == _d("09-18 03:00:00")


# ── schedule_runs 清理 ─────────────────────────────────
def _fresh_db(path) -> None:
    """测试间切换 DB：重置线程池连接，避免复用指向已删除库文件的旧连接。"""
    import database as db
    db._thread_local.__dict__.clear()
    db.init_db(str(path))


def test_create_schedule_persists_midnight_hour(tmp_path):
    """回归：create_schedule 曾用 `int(data.get("time_hour") or 9)` 落库，
    把午夜(0)静默改成 9——而路由允许 0、compute_next_run 也已按 is-not-None 修，
    唯独落库这处漏改。首帧 next_run 从入参 dict 算(对)，之后每次 trigger 重算
    读库行(9) → 任务永久挪到 09:00。钉死 time_hour=0 能原样落库。"""
    import database as db

    _fresh_db(tmp_path / "t.db")
    db.create_schedule({
        "id": "mid", "name": "mid", "schedule_type": "daily",
        "time_hour": 0, "time_minute": 30,
    })
    row = db.get_schedule("mid")
    assert row["time_hour"] == 0, row
    assert row["time_minute"] == 30, row


def test_prune_schedule_runs_keeps_latest_per_schedule(tmp_path):
    import database as db

    _fresh_db(tmp_path / "t.db")
    conn = db._get_conn()
    try:
        # schedule_runs.schedule_id 有外键 → 先建父 schedule
        db.create_schedule({"id": "A", "name": "A"})
        db.create_schedule({"id": "B", "name": "B"})
        # schedule A：5 条（created_at 递增，直接 SQL 控制时间）；schedule B：1 条
        for i in range(5):
            db.create_schedule_run("A", f"ra{i}")
        db.create_schedule_run("B", "rb0")
        for i in range(5):
            conn.execute(
                "UPDATE schedule_runs SET created_at=? WHERE run_id=?",
                (f"2026-01-0{i+1} 00:00:00", f"ra{i}"),
            )
        conn.commit()

        deleted = db.prune_schedule_runs(max_count=3)
        # A 保留最近 3 条（ra2..ra4），删 ra0/ra1；B 只有 1 条不删
        assert deleted == 2
        a_ids = {r[0] for r in conn.execute(
            "SELECT run_id FROM schedule_runs WHERE schedule_id='A'").fetchall()}
        b_ids = {r[0] for r in conn.execute(
            "SELECT run_id FROM schedule_runs WHERE schedule_id='B'").fetchall()}
        assert a_ids == {"ra2", "ra3", "ra4"}, a_ids
        assert b_ids == {"rb0"}, b_ids
    finally:
        conn.close()


def test_prune_schedule_runs_no_op_under_cap(tmp_path):
    import database as db

    _fresh_db(tmp_path / "t2.db")
    db.create_schedule({"id": "A", "name": "A"})
    for i in range(3):
        db.create_schedule_run("A", f"r{i}")
    assert db.prune_schedule_runs(max_count=5) == 0


# ── 审查R2修复：UTC 日期 ≠ 本地日期（跨午夜窗口）────────────────────────────
# now 存的是 UTC naive；旧实现直接拿它的 .date() 当"今天"去拼本地时分，
# 于是 UTC+8 下每天 16:00–24:00 UTC（本地次日 00:00–08:00）这段窗口里
# "今天"被算错一天：daily 会跳过今天已到点的时刻、monthly 会把已过去的
# 本月某天当成还没到。修复后一律以**本地日期**为基准，再加 while 滚动兜底。
NOW_LATE_UTC = datetime(2026, 9, 17, 20, 0, 0)  # 本地 2026-09-18 04:00（周五）


def test_daily_after_utc_midnight_uses_local_today():
    # 本地 9-18 09:00 还没到 → 今天（UTC 9-18 01:00），不是"明天"
    s = {"schedule_type": "daily", "time_hour": 9, "time_minute": 0}
    assert compute_next_run(s, NOW_LATE_UTC) == _d("09-18 01:00:00")


def test_daily_after_utc_midnight_passed_time_rolls_forward():
    # 本地 9-18 03:00 已过（现在是 04:00）→ 下一个本地日 9-19 03:00 = UTC 9-18 19:00
    s = {"schedule_type": "daily", "time_hour": 3, "time_minute": 0}
    assert compute_next_run(s, NOW_LATE_UTC) == _d("09-18 19:00:00")


def test_monthly_after_utc_midnight_does_not_repeat_past_day():
    # 本地今天就是 18 号且 00:30 已过 → 下月 18 号 00:30 本地 = UTC 10-17 16:30。
    # 旧实现按 UTC 日期(17) 判断"18 号还没到"，会算出已过去的 9-18 00:30。
    s = {"schedule_type": "monthly", "day_of_month": 18, "time_hour": 0, "time_minute": 30}
    assert compute_next_run(s, NOW_LATE_UTC) == "2026-10-17 16:30:00"


def test_weekly_after_utc_midnight_same_local_day_not_passed():
    # 本地 9-18 是周五，09:00 未到 → 今天（UTC 9-18 01:00）
    s = {"schedule_type": "weekly", "weekday": 4, "time_hour": 9, "time_minute": 0}
    assert compute_next_run(s, NOW_LATE_UTC) == _d("09-18 01:00:00")


def test_weekly_after_utc_midnight_same_local_day_passed():
    # 本地周五 03:00 已过 → 下周五 03:00 本地 = UTC 9-24 19:00
    s = {"schedule_type": "weekly", "weekday": 4, "time_hour": 3, "time_minute": 0}
    assert compute_next_run(s, NOW_LATE_UTC) == _d("09-24 19:00:00")
