"""关系算法 —— 让「同一个人」在陌生人面前和老朋友面前表现不同。

三条设计原则：

1. **只增不减地记账，读时衰减**。score 落库是历史累计，衰减按「距上次互动多久」
   在读取时计算。写时衰减会让关系单向坍缩：一次长时间没聊就永久降档，再也回不去。
2. **阶段由分数阈值推，阈值由升温速度缩放**。慢热人格要聊更久才走到「亲近」，
   自来熟人格几次就到——这不是难度设置，是人格设定的一部分。
3. **每轮对话的增量很小（1 分），会话开场额外给一次**。关系是聊出来的，
   不是刷出来的；但「隔了很久重新开一段」应该比「连着说十句」更有分量。
"""

from datetime import datetime, timezone
from typing import Any

import database as db

SPEEDS: dict[str, dict[str, float]] = {
    # half_life_days: 多久不聊会掉一半熟悉度
    # threshold_factor: 阶段阈值倍率（>1 更难升，<1 更快升）
    # gain: 每轮得分倍率
    "slow": {"half_life_days": 30.0, "threshold_factor": 1.5, "gain": 0.7},
    "normal": {"half_life_days": 14.0, "threshold_factor": 1.0, "gain": 1.0},
    "fast": {"half_life_days": 7.0, "threshold_factor": 0.6, "gain": 1.4},
}

# 阶段门槛（normal 口径）。第一档从 0 起，所以新人格天然是「陌生」。
BASE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.0, "陌生"),
    (8.0, "熟悉"),
    (28.0, "亲近"),
    (70.0, "默契"),
)

TURN_GAIN = 1.0
SESSION_OPEN_BONUS = 2.0
SESSION_GAP_HOURS = 6.0

_TIMESTAMP = "%Y-%m-%d %H:%M:%S"


def _parse(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip()[:19], _TIMESTAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _days_since(raw: Any, now: datetime) -> float | None:
    stamp = _parse(raw)
    if not stamp:
        return None
    return max(0.0, (now - stamp).total_seconds() / 86400.0)


def _speed(speed: str | None) -> dict[str, float]:
    return SPEEDS.get(speed or "normal", SPEEDS["normal"])


def _thresholds(speed: str | None) -> list[tuple[float, str]]:
    factor = _speed(speed)["threshold_factor"]
    return [(value * factor, stage) for value, stage in BASE_THRESHOLDS]


def stage_for(score: float, speed: str | None = None) -> dict:
    """按有效分算阶段与进度。progress 给前端画进度条用，不必精确到小数后三位。"""
    thresholds = _thresholds(speed)
    stage = thresholds[0][1]
    next_at: float | None = None
    floor = thresholds[0][0]
    for value, name in thresholds:
        if score >= value:
            stage = name
            floor = value
        else:
            next_at = value
            break
    if next_at is None:
        progress = 1.0
    else:
        span = max(next_at - floor, 0.001)
        progress = round(min(max((score - floor) / span, 0.0), 1.0), 3)
    return {
        "stage": stage,
        "progress": progress,
        "next_at": None if next_at is None else round(next_at, 1),
        "score": round(score, 2),
    }


def stage_from_row(row: dict, speed: str | None = None, now: datetime | None = None) -> dict:
    """由账本行算阶段（纯函数，便于批量列表时避免 N+1 查询）。"""
    now = now or datetime.now(timezone.utc)
    days = _days_since(row.get("last_at"), now)
    effective = float(row.get("score") or 0.0)
    if days and days > 0:
        half_life = _speed(speed)["half_life_days"]
        effective *= 0.5 ** (days / half_life)
    state = stage_for(effective, speed)
    state.update({
        "persona_id": row.get("persona_id"),
        "turns": int(row.get("turns") or 0),
        "days_since": None if days is None else round(days, 2),
        "raw_score": round(float(row.get("score") or 0.0), 2),
    })
    return state


def snapshot(persona_id: str, speed: str | None = None) -> dict:
    """读当前关系状态：累计分 → 衰减 → 阶段。"""
    return stage_from_row(db.get_bot_relation(persona_id), speed)


def record_turn(persona_id: str, speed: str | None = None) -> dict:
    """记一轮用户发言：基础分 + （隔了很久的）开场加成。"""
    row = db.get_bot_relation(persona_id)
    now = datetime.now(timezone.utc)
    days = _days_since(row.get("last_at"), now)
    gain = _speed(speed)["gain"]
    delta = TURN_GAIN * gain
    if days is None or days * 24.0 >= SESSION_GAP_HOURS:
        delta += SESSION_OPEN_BONUS * gain
    db.bump_bot_relation(persona_id, delta)
    return snapshot(persona_id, speed)


def gap_days(persona_id: str) -> float | None:
    """距上次互动的天数，供情境判定用（久未联系）。"""
    row = db.get_bot_relation(persona_id)
    return _days_since(row.get("last_at"), datetime.now(timezone.utc))
