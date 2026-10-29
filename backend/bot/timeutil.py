"""bot 包共享的时间小工具。

此前「落库时间戳 ↔ UTC datetime」的解析/格式化在 memory / state / relation /
proactive 各有一份私有实现，语义相同（"%Y-%m-%d %H:%M:%S"，[:19] 截断，UTC 解析）。
收敛到这里：一处修口径，四处受益。各模块以私有别名引用（_parse_ts / _fmt_ts），
既有调用方与测试引用的私有名不受影响。
"""

from datetime import datetime, timezone

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def parse_ts(raw) -> "datetime | None":
    """解析落库时间戳；空值 / 坏格式一律 None（调用方按缺失处理，不抛错）。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return datetime.strptime(str(raw).strip()[:19], _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_ts(dt: datetime) -> str:
    """datetime → 落库时间戳字符串（UTC 口径由调用方保证）。"""
    return dt.strftime(_TS_FMT)


def days_since(raw, now: datetime, default: "float | None") -> "float | None":
    """raw 时间戳距 now 的天数；缺失/坏格式返回 default（调用方定语义）。"""
    ts = parse_ts(raw)
    if ts is None:
        return default
    return max(0.0, (now - ts).total_seconds() / 86400.0)
