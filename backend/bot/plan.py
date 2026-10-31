"""计划钩子：把「周末要去看展」落成可判定的到期窗口。

spec 2026-10-31-plan-followup

纯函数区（`plan_window`）不碰 IO：给定时间词与基准时刻，返回左闭右开的本地
时区窗口。IO 区（`record_hooks`）每条 Fact 一次 supersede + 一次插入，整段包在
`try` 里 —— 钩子是增强项，不能拖垮记忆写入与聊天主链路。

时区口径：窗口按 `base` 自带的时区做日历运算，落库前统一转 UTC（`fmt_ts`）。
解析不出的时间词一律不建钩子：宁可不问，不能问错。
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone

import database as db

from .timeutil import fmt_ts as _fmt_ts

logger = logging.getLogger(__name__)

# 窗口结束后多久之内还可以问
PLAN_FOLLOWUP_H = 36.0
# 「待会 / 等会儿」的窗口长度
PLAN_NEAR_WINDOW_H = 4.0

# 计划正文的坏开头：长一点的时间词被切剩的碎片（「下周末」→「末」、
# 「下个月底交房租」→「月底交房租」）与否定式（「周末不去了」）。
# spec §2：宁可不问，不能问错 —— 这类正文一旦问出去就是错话。代价是「明天会下雨」这种
# 把「会」当真动词的说法也不会建钩子，接受。
_BAD_SUBJECT_PREFIX = ("不", "没", "别", "末", "个", "天", "周", "月", "晚", "会", "儿")


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
    if text in ("下周末", "下个周末"):
        # 与「周末」同构，基准整个挪一周：不能落到「下周」的整周窗口 —— 那样
        # 会在用户还没去看展之前就问「后来怎么样了」。
        monday = today - timedelta(days=base.weekday()) + timedelta(days=7)
        saturday = monday + timedelta(days=5)
        return saturday, monday + timedelta(days=7)
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


def record_hooks(
    persona_id: str,
    conversation_id: "str | None",
    facts,
    base: datetime,
) -> int:
    """把抽取到的计划落成钩子。返回新建条数。

    只认 `title == "计划"` 且时间词、主题都在的 Fact。任何异常只记 warning：
    钩子是增强项，不能拖垮记忆写入与聊天主链路。
    """
    if not persona_id or not facts:
        return 0
    created_at = _fmt_ts(base.astimezone(timezone.utc))
    written = 0
    for fact in facts:
        try:
            if fact.title != "计划" or not fact.when or not fact.subject:
                continue
            if fact.subject.startswith(_BAD_SUBJECT_PREFIX):
                logger.debug("计划正文是时间词碎片或否定式，不建钩子: %r", fact.subject)
                continue
            window = plan_window(fact.when, base)
            if window is None:
                logger.debug("计划时间词无法解析，不建钩子: %r", fact.when)
                continue
            start, end = window
            db.add_plan_hook(
                persona_id,
                conversation_id,
                None,
                fact.subject,
                fact.when,
                _fmt_ts(start.astimezone(timezone.utc)),
                _fmt_ts(end.astimezone(timezone.utc)),
                created_at,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 —— 钩子失败不阻断记忆写入
            logger.warning("计划钩子写入失败: %s", exc)
    return written
