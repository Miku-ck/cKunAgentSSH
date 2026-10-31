"""计划钩子：把「周末要去看展」落成可判定的到期窗口。

spec 2026-10-31-plan-followup

纯函数区（`plan_window`）不碰 IO：给定时间词与基准时刻，返回左闭右开的本地
时区窗口。IO 区（`record_hooks`）只做一次索引查询 + 一次写库，异常一律吞掉 ——
钩子是增强项，不能拖垮记忆写入与聊天主链路。

时区口径：窗口按 `base` 自带的时区做日历运算，落库前统一转 UTC（`fmt_ts`）。
解析不出的时间词一律不建钩子：宁可不问，不能问错。
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 窗口结束后多久之内还可以问
PLAN_FOLLOWUP_H = 36.0
# 「待会 / 等会儿」的窗口长度
PLAN_NEAR_WINDOW_H = 4.0


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def plan_window(when_text: str, base: datetime) -> "tuple[datetime, datetime] | None":
    """时间词 → 本地时区窗口 [start, end)。未知时间词返回 None。

    base 必须 aware：窗口用它的时区做日历运算，不做任何时区换算。
    """
    text = (when_text or "").strip()
    if not text:
        return None
    today = _day_start(base)

    if text in ("待会", "等会儿"):
        return base, base + timedelta(hours=PLAN_NEAR_WINDOW_H)
    if text == "今晚":
        return today.replace(hour=18), today + timedelta(days=1)
    if text == "明天":
        return today + timedelta(days=1), today + timedelta(days=2)
    if text == "后天":
        return today + timedelta(days=2), today + timedelta(days=3)
    if text == "周末":
        # 周一=0 … 周六=5、周日=6。周末中说「周末」指当下这个周末。
        weekday = base.weekday()
        if weekday >= 5:
            end = today + timedelta(days=2 if weekday == 5 else 1)
            return today, end
        saturday = today + timedelta(days=5 - weekday)
        return saturday, saturday + timedelta(days=2)
    if text == "下周":
        monday = today - timedelta(days=base.weekday()) + timedelta(days=7)
        return monday, monday + timedelta(days=7)
    if text == "下个月":
        year, month = base.year, base.month + 1
        if month > 12:
            year, month = year + 1, 1
        start = datetime(year, month, 1, tzinfo=base.tzinfo)
        return start, start + timedelta(days=calendar.monthrange(year, month)[1])
    return None
