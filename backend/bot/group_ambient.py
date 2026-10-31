"""群聊「可参与期」：@ 之后的 5 分钟窗口、接话节流、近期上下文。

全部是进程内状态，不落库（spec 2026-10-30-qq-group-ambient §1）：
窗口只有 5 分钟、上下文只服务这一次接话，重启丢掉没有任何长期损失。

跨线程：这张表是进程内单例，而每个机器人各有一条 `ckunbot-qq-*` 收包线程，
多个机器人的事件会落进同一个 dict（key 带 channel_id），所以读写都要过锁。

档位判定（`ambient_allowed`）只是纯函数：取人格字段由入站管线做，这里不碰库。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# 平台被动回复窗口：群里 5 分钟 / 每条消息最多 5 次。超出窗口的接话不再是
# 「回复」而是主动消息，要群主单独授权 —— 所以窗口一关就闭嘴，不拿主动消息顶上去。
AT_WINDOW_MIN = 5.0
# 两次接话的最小间隔：不设节流的话，活跃群里 5 分钟能刷出几十条回复。
REPLY_GAP_S = 60.0
# 上下文只保最近一段：更早的群消息既没人记得，也不该撑爆 prompt。
CONTEXT_TTL_MIN = 10.0
CONTEXT_MAX_LINES = 20
# 重投去重只看最近几条事件：同一条消息的重复投递只会紧邻发生，不需要长期台账。
EVENT_HISTORY = 64

# 群参与档位（人格字段，库里一列）：从最安静到最吵排序，UI 平铺顺序也按这一份。
GROUP_MODES = ("mention_only", "ambient", "proactive")
# 允许不 @ 就接话的档位。proactive 包含 ambient：开到最高档自然也能接。
AMBIENT_MODES = ("ambient", "proactive")


def ambient_allowed(group_mode) -> bool:
    """这个人格在群里能不能不 @ 就开口。

    默认不接：未知值/空值一律按最保守档读 —— 这里出错多说的话，用户听不到任何
    报错，只能看到机器人忽然在群里插嘴。
    """
    return (group_mode or "") in AMBIENT_MODES


@dataclass
class _State:
    at_until: "datetime | None" = None
    last_spoken_at: "datetime | None" = None
    lines: "deque[tuple[datetime, str, str]]" = field(
        default_factory=lambda: deque(maxlen=CONTEXT_MAX_LINES)
    )
    seen: "deque[str]" = field(default_factory=lambda: deque(maxlen=EVENT_HISTORY))


_states: dict[tuple[str, str], _State] = {}
_lock = threading.Lock()


def _key(channel_id: str, group_openid: str) -> tuple[str, str]:
    return (channel_id or "", group_openid or "")


def _now(now: "datetime | None") -> datetime:
    return now or datetime.now(timezone.utc)


def _state(key: tuple[str, str]) -> _State:
    state = _states.get(key)
    if state is None:
        state = _State()
        _states[key] = state
    return state


def note_mention(channel_id: str, group_openid: str, *, now: "datetime | None" = None) -> None:
    """有人在群里 @ 了机器人：把可参与期续到当前 + 5 分钟。"""
    deadline = _now(now) + timedelta(minutes=AT_WINDOW_MIN)
    with _lock:
        _state(_key(channel_id, group_openid)).at_until = deadline


def note_line(
    channel_id: str,
    group_openid: str,
    speaker: str,
    text: str,
    *,
    now: "datetime | None" = None,
) -> None:
    """记一条群消息（含机器人自己的发言），只用于接话时的临时上下文。"""
    text = (text or "").strip()
    if not text:
        return
    stamp = _now(now)
    name = (speaker or "").strip() or "群友"
    with _lock:
        _state(_key(channel_id, group_openid)).lines.append((stamp, name, text))


def is_open(channel_id: str, group_openid: str, *, now: "datetime | None" = None) -> bool:
    """可参与期是否还开着（@ 之后 5 分钟内）。"""
    stamp = _now(now)
    with _lock:
        state = _states.get(_key(channel_id, group_openid))
        return bool(state and state.at_until and stamp < state.at_until)


def should_reply(channel_id: str, group_openid: str, *, now: "datetime | None" = None) -> bool:
    """这条非 @ 消息该不该接：窗口开着 + 距上次开口够久。

    窗口判定直接写在这里而不是复用 `is_open`：`threading.Lock` 不可重入，
    嵌套拿锁会自锁；两处口径必须一起改，所以 `is_open` 只留给单测断言窗口本身。
    """
    stamp = _now(now)
    with _lock:
        state = _states.get(_key(channel_id, group_openid))
        if not state or not state.at_until or stamp >= state.at_until:
            return False
        if state.last_spoken_at and (stamp - state.last_spoken_at).total_seconds() < REPLY_GAP_S:
            return False
        return True


def seen_event(channel_id: str, group_openid: str, event_id: str) -> bool:
    """这条事件是否已经处置过（ True = 看过，别再接一次）。

    环境接话不落 `bot_messages`，没有 `answered_at` 可以做幂等；而平台在 ACK
    丢时会重投同一条事件 —— 只靠 60s 节流挡住是侥幸，节流参数一改就漏。
    """
    if not event_id:
        return False
    with _lock:
        state = _state(_key(channel_id, group_openid))
        if event_id in state.seen:
            return True
        state.seen.append(event_id)
        return False


def mark_spoken(channel_id: str, group_openid: str, *, now: "datetime | None" = None) -> None:
    """记一次开口：节流从这里起算。"""
    stamp = _now(now)
    with _lock:
        _state(_key(channel_id, group_openid)).last_spoken_at = stamp


def context_lines(
    channel_id: str, group_openid: str, *, now: "datetime | None" = None
) -> "list[tuple[str, str]]":
    """未过期的近期群消息，按时间正序。"""
    stamp = _now(now)
    cutoff = stamp - timedelta(minutes=CONTEXT_TTL_MIN)
    with _lock:
        state = _states.get(_key(channel_id, group_openid))
        lines = list(state.lines) if state else []
    return [(name, text) for at, name, text in lines if at >= cutoff]


def reset_for_tests() -> None:
    with _lock:
        _states.clear()
