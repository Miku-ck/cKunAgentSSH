"""陪聊的状态层 —— 让「同一个人」在不同时刻有不一样的状态。

对齐 L/M/S 心理状态模型（ACL 2026, Beyond Static Persona Consistency）：
- L 长期身份：bot_personas，本模块不动；
- M 中期关系：trust / intimacy / tension / novelty 四轴（取代单一 score 的语义，
  score 列仍由 relation.record_turn 维护，供 UI 进度条继续工作）；
- S 短期 affect：valence / energy / patience 三轴，每轮可动。

三条设计原则：

1. **0 次额外模型调用**。状态由「事件 → 增量」的确定性函数推导，不让模型自报
   JSON——可重放、可单测、零延迟、零噪声。规则管骨架，模型只负责说血肉。
2. **软饱和 + 事件冷却 + 基线回归**。推向远离基线方向的增量按剩余量程打折
   （praise 连发自然趋缓、永不爆表）；拉回基线方向的增量不打折（吵架后哄得好
   是数学上成立的）；同一事件在冷却窗内重复，增量按 0.3^k 衰减。
3. **衰减读时惰性计算 + 写回**（与 relation.py 同一哲学）：state_at 记录上次
   写入时刻，读取时按各轴半衰期向基线回归。回归目标是**基线而不是 0**——
   老朋友三个月不聊，重逢时是「有点生疏的老朋友」，不是陌生人。

处理顺序固定：先衰减旧状态 → 再逐个施加事件增量 → 写回。
同一输入序列永远得到同一状态轨迹。
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any

from .relation import SPEEDS
from .timeutil import fmt_ts as _fmt_ts, parse_ts as _parse_ts

# ── 轴定义 ──
# (lo, hi, baseline)。valence/energy/patience 是各自区间的绝对量，
# trust/intimacy/tension/novelty 是 0-100 刻度上的点数。

AXIS_BOUNDS: dict[str, tuple[float, float, float]] = {
    "trust": (0.0, 100.0, 15.0),
    "intimacy": (0.0, 100.0, 8.0),
    "tension": (0.0, 100.0, 0.0),
    "novelty": (0.0, 100.0, 40.0),
    "valence": (-1.0, 1.0, 0.0),
    "energy": (0.0, 1.0, 0.6),  # 基线动态（昼夜节律），见 energy_baseline
    "patience": (0.0, 1.0, 0.8),
}

# 全新关系的开局状态：有点好奇、心态开放，但信任还没建立。
INITIAL_STATE: dict[str, float] = {
    "trust": 10.0,
    "intimacy": 5.0,
    "tension": 0.0,
    "novelty": 60.0,
    "valence": 0.0,
    "energy": 0.6,
    "patience": 0.8,
}

# 半衰期（小时）。trust/intimacy 随升温档缩放（对齐 relation.SPEEDS 的哲学），
# 其余各轴三档同速：情绪几个小时就平，疙瘩一天半自己消，新鲜感撑四天。
_HALF_LIFE_HOURS: dict[str, dict[str, float] | float] = {
    "trust": {"normal": 40 * 24.0, "fast": 20 * 24.0, "slow": 60 * 24.0},
    "intimacy": {"normal": 14 * 24.0, "fast": 7 * 24.0, "slow": 30 * 24.0},
    "tension": 36.0,
    "novelty": 4 * 24.0,
    "valence": 5.0,
    "energy": 3.0,
    "patience": 8.0,
}

# ── 事件 → 增量映射 ──
# 量纲：valence/energy/patience 为各自区间绝对量，其余为 0-100 点数。
# 依据示例：overreach 的 -6 trust ≈ 抵掉两天日常积累的信任增益。

EVENT_DELTAS: dict[str, dict[str, float]] = {
    "ai_probe": {"valence": -0.15, "patience": -0.05, "trust": -2.0, "tension": 8.0, "novelty": -5.0},
    "overreach": {"valence": -0.25, "patience": -0.10, "trust": -6.0, "intimacy": -2.0, "tension": 20.0, "novelty": -10.0},
    "doubt": {"valence": -0.10, "trust": -3.0, "tension": 6.0, "novelty": -3.0},
    "fatigue": {"valence": -0.08, "energy": -0.05, "patience": 0.10, "intimacy": 1.0, "tension": -5.0, "novelty": -3.0},
    "excite": {"valence": 0.20, "energy": 0.05, "intimacy": 1.0, "tension": -3.0, "novelty": 12.0},
    "praise": {"valence": 0.25, "energy": 0.05, "trust": 1.0, "intimacy": 2.0, "tension": -6.0, "novelty": 6.0},
    "farewell": {"valence": -0.05, "novelty": -8.0},
    "neglect": {"valence": -0.15, "patience": -0.10, "trust": -2.0, "intimacy": -3.0, "tension": 10.0, "novelty": -15.0},
    "question": {"valence": 0.05, "patience": -0.02, "novelty": 15.0},
    "conflict": {"valence": -0.30, "patience": -0.15, "trust": -4.0, "intimacy": -1.0, "tension": 25.0, "novelty": -5.0},
    "media": {"valence": 0.10, "trust": 1.0, "intimacy": 1.0, "novelty": 10.0},
    "burst": {"valence": 0.08, "patience": -0.08, "intimacy": 1.0, "novelty": 8.0},
    "late_night": {"energy": -0.10, "patience": -0.05, "trust": 1.0, "intimacy": 2.0},
    "cold_reply": {"valence": -0.20, "trust": -3.0, "intimacy": -1.0, "tension": 8.0},
}

# 每轮基础事件：普通聊天也加深熟悉度（对齐 relation.TURN_GAIN 的哲学）。
# intimacy 增量按升温档 gain 缩放——慢热人格聊得慢是设定的一部分。
EVENT_DELTAS["chat"] = {"intimacy": 1.0}
_GAIN_SCALED = frozenset({"chat"})

# 同一事件在冷却窗内重复出现，增量 × 0.3^k（k = 窗口内第几次重复，0 起）。
# burst / late_night 由调用侧保证每轮至多一次，不再设窗。
COOLDOWN_S: dict[str, float] = {
    "ai_probe": 600.0,
    "overreach": 900.0,
    "doubt": 1800.0,
    "fatigue": 1800.0,
    "excite": 900.0,
    "praise": 1200.0,
    "question": 300.0,
    "conflict": 1800.0,
    "media": 300.0,
    "burst": 0.0,
    "late_night": 0.0,
    "farewell": 0.0,
    "neglect": 0.0,
    "cold_reply": 0.0,
    "chat": 0.0,
}

def _speed_key(speed: str | None) -> str:
    return speed if speed in SPEEDS else "normal"


def energy_baseline(hour: float) -> float:
    """昼夜节律基线：15 点峰值 0.7，凌晨谷值 0.35。

    energy 的回归目标是这条曲线而不是常数——深夜她自然「没精神」，
    不需要任何额外规则。
    """
    return 0.35 + 0.35 * math.sqrt(max(0.0, math.cos(2 * math.pi * (hour - 15) / 24)))


def _half_life_hours(axis: str, speed: str | None) -> float:
    spec = _HALF_LIFE_HOURS[axis]
    if isinstance(spec, dict):
        return spec[_speed_key(speed)]
    return spec


def decay(state: dict, now: datetime, speed: str | None, local_hour: float) -> dict:
    """按 state_at 起算的惰性衰减：各轴向基线回归，energy 向昼夜基线回归。"""
    state_at = _parse_ts(state.get("state_at"))
    if state_at is None:
        return dict(state)
    hours = max(0.0, (now - state_at).total_seconds() / 3600.0)
    out = dict(state)
    for axis, (lo, hi, baseline) in AXIS_BOUNDS.items():
        if axis not in out:
            continue
        half = _half_life_hours(axis, speed)
        if axis == "energy":
            baseline = energy_baseline(local_hour)
        target = baseline + (float(out[axis]) - baseline) * (0.5 ** (hours / half))
        out[axis] = min(max(target, lo), hi)
    return out


def _saturate(value: float, delta: float, lo: float, hi: float, baseline: float) -> float:
    """软饱和：推向远离基线方向按剩余量程打折；拉回基线方向全额。"""
    if delta == 0.0:
        return value
    if (delta > 0 and value < baseline) or (delta < 0 and value > baseline):
        return value + delta
    room = 1.0 - abs(value - baseline) / (hi - lo)
    return value + delta * max(room, 0.0)


def apply_events(
    state: dict, events: list[str], now: datetime, speed: str | None
) -> tuple[dict, dict]:
    """按顺序施加事件增量（含饱和 / 冷却 / 限幅），返回 (新状态, 事件记账)。

    事件记账 last_event_* 落库供下一次冷却判定；同一批 events 内的重复事件
    视为连续重复（burst 之类由调用侧去重，这里不重复拦）。
    """
    out = dict(state)
    gain = SPEEDS[_speed_key(speed)]["gain"]
    kind = out.get("last_event_kind")
    last_at = _parse_ts(out.get("last_event_at"))
    count = int(out.get("last_event_count") or 0)
    applied: list[str] = []
    for event in events:
        deltas = EVENT_DELTAS.get(event)
        if not deltas:
            continue
        # 冷却：同事件且仍在窗口内 → 第 k 次重复（k 从 0 起）
        window = COOLDOWN_S.get(event, 0.0)
        if event == kind and window > 0 and last_at is not None:
            gap = (now - last_at).total_seconds()
            if 0 <= gap < window:
                factor = 0.3 ** count
            else:
                factor, count = 1.0, 0
        else:
            factor, count = 1.0, 0
        for axis, delta in deltas.items():
            if axis not in AXIS_BOUNDS:
                continue
            scaled = delta * gain if event in _GAIN_SCALED else delta
            lo, hi, baseline = AXIS_BOUNDS[axis]
            out[axis] = min(max(_saturate(float(out[axis]), scaled * factor, lo, hi, baseline), lo), hi)
        kind, count = event, count + 1
        last_at = now
        applied.append(event)
    out["last_event_kind"] = kind
    out["last_event_at"] = _fmt_ts(now) if kind else out.get("last_event_at")
    out["last_event_count"] = count
    return out, {"applied": applied}


def state_from_row(row: dict | None) -> dict:
    """账本行 → 状态向量。缺列 / NULL 一律回 INITIAL_STATE（升级平滑）。"""
    state: dict[str, Any] = dict(INITIAL_STATE)
    state["last_event_kind"] = None
    state["last_event_at"] = None
    state["last_event_count"] = 0
    state["state_at"] = None
    if not row:
        return state
    for axis in AXIS_BOUNDS:
        raw = row.get(axis)
        if raw is not None:
            lo, hi, _ = AXIS_BOUNDS[axis]
            state[axis] = min(max(float(raw), lo), hi)
    state["last_event_kind"] = row.get("last_event_kind")
    state["last_event_at"] = row.get("last_event_at")
    state["last_event_count"] = int(row.get("last_event_count") or 0)
    state["state_at"] = row.get("state_at")
    return state


# ── 离散档位注入 ──
# 只喂档位词，不喂裸数字：数值进 prompt 会被模型复述进台词（「我耐心只剩 0.3 了」）。

def describe_state(
    state: dict,
    local_hour: float,
    speed: str | None = None,
    stage: str | None = None,
) -> str:
    """状态 → 一段中文描述。只提非中性的轴：永远说心情，其余偏离基线才说。

    stage 由调用方传入（relation 的 score 口径，与 UI 进度条同源）：
    阶段只允许一个事实源，否则同一份 prompt 会出现两个可能矛盾的阶段。
    不传 stage 时不提关系，只说此刻的情绪与分寸。
    """
    segs: list[str] = []

    valence = float(state.get("valence") or 0.0)
    if valence <= -0.4:
        mood = "低落"
    elif valence <= -0.1:
        mood = "有点闷"
    elif valence <= 0.2:
        mood = "平静"
    elif valence <= 0.55:
        mood = "愉快"
    else:
        mood = "来劲"
    segs.append(f"心情：{mood}")

    energy = float(state.get("energy") or 0.6)
    late = local_hour >= 23.0 or local_hour < 5.5
    if energy <= 0.3:
        segs.append("精力：很困" + ("（现在很晚了，是撑着陪你）" if late else ""))
    elif energy <= 0.55:
        segs.append("精力：有点累" + ("（现在很晚了，是撑着陪你）" if late else ""))
    elif energy > 0.8:
        segs.append("精力：很足")

    tension = float(state.get("tension") or 0.0)
    if stage:
        rel_seg = f"关系：{stage}"
        if tension >= 60.0:
            rel_seg += "，但有个小疙瘩没解开"
        elif tension >= 30.0:
            rel_seg += "，但有点别扭"
        segs.append(rel_seg)
    elif tension >= 30.0:
        segs.append("有个小疙瘩没解开" if tension >= 60.0 else "有点别扭")

    patience = float(state.get("patience") or 0.8)
    if patience <= 0.3:
        segs.append("耐心：快见底了")

    lines = "；".join(segs) + "。"
    if tension >= 60.0:
        lines += "分寸：别装作没事——可以提一句那个疙瘩，但别追问。"
    elif patience <= 0.3:
        lines += "分寸：回复可以短一点，别硬撑着长聊。"
    return "【此刻状态】" + lines


# ── 确定性后处理开关 ──

def downgrade_style(style: str, state: dict | None) -> str:
    """没耐心时回复长度档降一档（long→moderate→concise）。"""
    if state and float(state.get("patience") or 1.0) < 0.3:
        return {"long": "moderate", "moderate": "concise"}.get(style, style)
    return style


_PROBE_LINES = ("……我们还好吧？", "你还在想刚才的事吗？", "那个事，我们说开好吗？")


def probe_line(conversation_id: str) -> str:
    """tension 高且回复里没有问句时补的试探性收尾。按会话 id 确定性选一条。"""
    digest = hashlib.md5(conversation_id.encode("utf-8")).hexdigest()
    return _PROBE_LINES[int(digest, 16) % len(_PROBE_LINES)]


def strip_exclaim(text: str) -> str:
    """没精力时禁感叹号：文本级后处理比 prompt 约束可靠。"""
    return text.replace("！！", "。").replace("！", "。").replace("。。", "。")


def pacing_hint(state: dict | None) -> str:
    """在场感节奏提示（纯函数，空串 = 正常打字节奏）。

    - hesitate：有疙瘩（tension 高）或没耐心——出现、停一下、再出现；
    - relief：有点别扭但在缓和（30≤tension<60）——不演停顿，节奏放平；
    - eager：新鲜感高且心情好——有话想说、按捺不住，气泡间的等待收短。
    分寸优先于活泼：hesitate 永远压过 relief/eager。
    """
    if not state:
        return ""
    tension = float(state.get("tension") or 0.0)
    if tension >= 60.0 or float(state.get("patience") or 1.0) < 0.3:
        return "hesitate"
    if tension >= 30.0:
        return "relief"
    if float(state.get("novelty") or 40.0) >= 70.0 and float(state.get("valence") or 0.0) > 0.2:
        return "eager"
    return ""


# ── 每轮组装与落库（唯一接触 DB 的两个函数，其余全纯函数） ──

def turn_events(
    situation: str | None,
    *,
    has_media: bool,
    user_created_at: list,
    now: datetime,
    local_hour: float,
) -> list[str]:
    """一轮对话的事件清单。顺序即施加顺序：chat 恒在最前（基础互动），
    情境其次（keyword 优先级已在 detect_situation 里排好），非文本事件殿后。"""
    events = ["chat"]
    if situation:
        events.append(situation)
    if has_media:
        events.append("media")
    # burst：60s 内 ≥3 条用户消息（含当前这条——history 在落库之后读取，
    # 恰好包含当前消息一次）。
    recent = 0
    for raw in user_created_at:
        ts = _parse_ts(raw)
        if ts is not None and 0 <= (now - ts).total_seconds() <= 60.0:
            recent += 1
    if recent >= 3:
        events.append("burst")
    if local_hour < 5.5:
        events.append("late_night")
    return events


def update_for_turn(
    persona_id: str,
    speed: str | None,
    events: list[str],
    now: datetime | None = None,
    local_hour: float | None = None,
) -> dict | None:
    """读 → 衰减 → 施加事件 → 写回。返回更新后的状态向量；无人格时返回 None。

    now / local_hour 可注入（测试重放）；缺省取真实时钟。
    """
    if not persona_id or not events:
        return None
    import database as db  # 局部导入：state.py 保持可独立单测（纯函数不被 DB 拖累）

    now = now or datetime.now(timezone.utc)
    if local_hour is None:
        local_now = datetime.now().astimezone()
        local_hour = local_now.hour + local_now.minute / 60.0
    row = db.get_bot_relation(persona_id)
    state = state_from_row(row)
    state = decay(state, now, speed, local_hour)
    state, _ = apply_events(state, events, now, speed)
    db.update_bot_state(
        persona_id,
        trust=state["trust"], intimacy=state["intimacy"], tension=state["tension"],
        novelty=state["novelty"], valence=state["valence"], energy=state["energy"],
        patience=state["patience"],
        last_event_kind=state["last_event_kind"], last_event_at=state["last_event_at"],
        last_event_count=state["last_event_count"], state_at=_fmt_ts(now),
    )
    return state
