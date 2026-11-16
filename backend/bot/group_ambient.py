"""群聊「可参与期」：@ 之后的 5 分钟窗口、接话节流、近期上下文。

可参与期窗口（at_until / last_spoken_at / seen 事件台账）仍是进程内状态：
窗口只有 5 分钟、节流只服务本次接话，重启丢掉没有长期损失。近期上下文
（lines）则经 `bot_ambient_lines` 表 write-through 落库（M9-5）：重启后把
TTL 窗口内的行灌回内存，群里刚聊过什么不会因为后端重启就「失忆」；
at_until 不持久化 —— 重启后不主动接话，等新的 @（保守正确）。

跨线程：这张表是进程内单例，而每个机器人各有一条 `ckunbot-qq-*` 收包线程，
多个机器人的事件会落进同一个 dict（key 带 channel_id），所以读写都要过锁。

档位判定（`ambient_allowed`）只是纯函数：取人格字段由入站管线做。
"""

from __future__ import annotations

import logging
import threading
import time as _time_mod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

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
    """记一条群消息进内存上下文（不落库；落库走 note_line_persisted）。"""
    text = (text or "").strip()
    if not text:
        return
    stamp = _now(now)
    name = (speaker or "").strip() or "群友"
    with _lock:
        _state(_key(channel_id, group_openid)).lines.append((stamp, name, text))


# 过期行清理的节流：活跃群里每条消息都写一行，TTL 清理最多 5 分钟一次就够
_PURGE_THROTTLE_S = 300.0
_last_purge_at: "float | None" = None


def note_line_persisted(
    channel_id: str,
    group_openid: str,
    speaker: str,
    text: str,
    *,
    now: "datetime | None" = None,
) -> None:
    """记一条群消息：内存上下文 + `bot_ambient_lines` 落库（write-through）。

    落库失败只留痕 —— ambient 是增强项，降级为仅内存与旧行为完全一致；
    内存上下文无论如何都更新（生成失败也该记得群里说了什么）。
    """
    stamp = _now(now)
    note_line(channel_id, group_openid, speaker, text, now=stamp)
    text = (text or "").strip()
    if not text:
        return
    try:
        # created_at 显式传 UTC 时间戳（datetime('now') 同格式），恢复侧按 UTC 读回
        from database import add_bot_ambient_line, purge_ambient_lines

        global _last_purge_at
        add_bot_ambient_line(
            channel_id or "", group_openid or "", (speaker or "").strip(), text,
            stamp.strftime("%Y-%m-%d %H:%M:%S"),
        )
        now_mono = _time_mod.monotonic()
        if (_last_purge_at is None
                or now_mono - _last_purge_at >= _PURGE_THROTTLE_S):
            _last_purge_at = now_mono
            purge_ambient_lines()
    except Exception as exc:  # noqa: BLE001 —— 落库失败降级为仅内存
        logger.warning("群环境上下文落库失败（仅保留内存）: %s", exc)


def restore_from_db() -> None:
    """重启恢复：把 TTL 窗口内的落库行灌回内存上下文（跨 channel 一次完成）。

    at_until / 节流台账不恢复 —— 重启后不主动接话，等新的 @（保守正确：
    重启后的主动接话比沉默危险）。
    """
    try:
        from database import list_recent_ambient_lines, purge_ambient_lines

        purge_ambient_lines()
        rows = list_recent_ambient_lines(minutes=CONTEXT_TTL_MIN)
    except Exception as exc:  # noqa: BLE001 —— 恢复失败退化为纯内存启动
        logger.warning("群环境上下文恢复失败（按空上下文启动）: %s", exc)
        return
    for row in rows:
        try:
            stamp = datetime.strptime(
                row.get("created_at") or "", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        note_line(
            row.get("channel_id") or "",
            row.get("group_openid") or "",
            row.get("speaker") or "",
            row.get("content") or "",
            now=stamp,
        )
    if rows:
        logger.info("群环境上下文已恢复 %d 行", len(rows))


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
