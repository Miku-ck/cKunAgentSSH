"""陪聊主动性（P3）：她会主动找你说话。

三个纯函数 + 一个调度器，0 次额外模型调用用于**决策**（desire 由状态推导），
只有真正开口那一次才调模型生成「心里话」。

分渠道（协议约束）：
- local：直投 —— 主动消息直接落 bot_messages（assistant），前端进会话即见；
- QQ：寄存 —— 被动回复窗口只有 150s，协议上无法主动发。过闸的心里话写
  bot_proactive_log(action=queued, 有效期 48h)，inbound_pipeline 在组回复前
  取出以「对了，」接入 —— 协议完全合法，行为上等价于「她憋了一句话，
  见到你终于说了」。

抑制器四重闸（全部可 mock 时钟单测）：最小间隔、静音窗、负反馈指数冷却、
每日配额。负反馈 = 主动消息投出 2h 内用户没回话 → 冷却 48h 起步，二次 ×2，
三次禁 7 天。
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

import database as db
from bot import state as state_mod

logger = logging.getLogger(__name__)

# ── 纯函数区（不碰 IO，全部可重放）──

FIRE_THRESHOLD = 0.55
# 负反馈冷却阶梯：第 1/2/3 次冷场
COLD_STEPS_H = (48.0, 96.0, 168.0)
QUIET_WINDOW = (23.5, 8.5)  # 本地小时；跨午夜
DAILY_QUOTA = 1
MIN_INTERVAL_H = {"fast": 12.0, "normal": 20.0, "slow": 36.0}
EXPECTED_GAP_H = {"fast": 24.0, "normal": 60.0, "slow": 120.0}
COLD_REPLY_THRESHOLD = 0.4  # 寄存消息捎带时对当下 desire 的再门槛
PROACTIVE_MAX_TOKENS = 256


def _sigmoid(x: float) -> float:
    import math
    return 1.0 / (1.0 + math.exp(-x))


def desire(
    state: dict,
    *,
    speed: str = "normal",
    pending_topic: bool = False,
    gap_hours: float = 0.0,
) -> float:
    """「想找你」的意愿，0..1。

    权重即产品判断：关系不熟不打扰（0.30）、话说一半最硬（0.25）、
    有新鲜事想分享（0.20）、久不见会想念（0.15）、心情好想说话（0.10）；
    有疙瘩不想先低头（-0.60）、没精力不想说话（-0.50）。
    """
    intimacy = float(state.get("intimacy") or 0.0)
    novelty = float(state.get("novelty") or 0.0)
    tension = float(state.get("tension") or 0.0)
    valence = float(state.get("valence") or 0.0)
    energy = float(state.get("energy") or 0.0)

    expected = EXPECTED_GAP_H.get(speed or "normal", 60.0)
    miss = max(0.0, min(1.0, (gap_hours - expected) / expected)) if expected > 0 else 0.0

    value = (
        0.30 * _sigmoid((intimacy - 30.0) / 20.0)
        + 0.20 * (novelty / 100.0)
        + 0.25 * (1.0 if pending_topic else 0.0)
        + 0.15 * miss
        + 0.10 * max(0.0, valence)
        - 0.60 * (tension / 100.0)
        - 0.50 * max(0.0, 0.5 - energy)
    )
    # 关系硬底线：不熟的人（intimacy<20）哪怕话说一半也不打扰 —— 未完话题
    # 是最硬的钩子，但那是对熟人的；对陌生人它只是"还不知道你叫什么"。
    if intimacy < 20.0:
        value = min(value, FIRE_THRESHOLD - 0.01)
    return max(0.0, min(1.0, value))


def _in_quiet_window(local_hour: float) -> bool:
    start, end = QUIET_WINDOW
    return local_hour >= start or local_hour < end


def gate(
    desire_value: float,
    *,
    speed: str = "normal",
    local_hour: float,
    last_send_at: "datetime | None",
    cooldown_until: "datetime | None",
    sent_today: int,
    now: "datetime | None" = None,
) -> "tuple[bool, str]":
    """四重抑制闸。返回 (是否触发, 原因)。原因供调 θ 与排障。"""
    now = now or datetime.now(timezone.utc)
    if desire_value < FIRE_THRESHOLD:
        return False, f"desire {desire_value:.2f} < {FIRE_THRESHOLD}"
    min_interval = MIN_INTERVAL_H.get(speed or "normal", 20.0)
    if last_send_at is not None and (now - last_send_at) < timedelta(hours=min_interval):
        return False, f"min_interval {min_interval}h"
    if _in_quiet_window(local_hour):
        return False, "quiet_window"
    if cooldown_until is not None and now < cooldown_until:
        return False, "cold_cooldown"
    if sent_today >= DAILY_QUOTA:
        return False, "daily_quota"
    return True, "ok"


def cold_cooldown_after(cold_count: int) -> "timedelta":
    """第 n 次冷场的冷却时长；超出阶梯按最重档。"""
    idx = max(0, min(cold_count, len(COLD_STEPS_H) - 1))
    return timedelta(hours=COLD_STEPS_H[idx])


# ── IO 区 ──

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _parse(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _count_past_colds(persona_id: str) -> int:
    """历史上因冷场被记冷却的次数（按 reason 前缀计）。"""
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM bot_proactive_log "
            "WHERE persona_id = ? AND reason LIKE 'cold:%'",
            (persona_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"] or 0)


def _state_snapshot(persona_id: str, speed: str) -> "tuple[dict, float, bool, str | None]":
    """(状态向量, 距上次互动小时数, 话说一半?, 冷却截止)。

    pending_topic / proactive_cooldown_until 不在 P1 状态向量里（那是
    relation.record_turn 的领地），直接从账本行取。
    """
    row = db.get_bot_relation(persona_id)
    state = state_mod.state_from_row(row)
    local_now = datetime.now().astimezone()
    local_hour = local_now.hour + local_now.minute / 60.0
    state = state_mod.decay(state, datetime.now(timezone.utc), speed, local_hour)
    gap_hours = 1e9
    if row and row.get("last_at"):
        last = _parse(row["last_at"])
        if last:
            gap_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    pending = bool((row or {}).get("pending_topic"))
    cooldown = (row or {}).get("proactive_cooldown_until")
    return state, gap_hours, pending, cooldown


async def _compose(persona: dict, state_desc: str) -> str:
    """让模型替她说一句「心里话」。极小预算、无工具、禁思考。"""
    from bot.character import SAFETY_CHARTER  # noqa: F401 —— 确保人格口径一致
    from bot.chat_service import BotChatService
    from llm_adapter import chat_stream

    svc = BotChatService()
    model = await asyncio.to_thread(svc._model_for_persona, persona)
    system = BotChatService._system_prompt(persona, "local", state_desc=state_desc)
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "（内部指令，别当对话）你现在想起了一件事或单纯想他了，"
                "主动给他发一条消息。只输出消息本身，1-2 条，空行分隔，"
                "自然口语，别解释为什么发。"
            ),
        },
    ]
    parts: list[str] = []
    async for chunk in chat_stream(
        model.get("base_url"), model.get("api_key"), model.get("model_id"),
        messages, temperature=1.0, max_tokens=PROACTIVE_MAX_TOKENS,
        enable_thinking=False, disable_thinking=True,
    ):
        if chunk.content:
            parts.append(chunk.content)
    return "".join(parts).strip()


async def _fire_local(persona: dict, conversation_id: str, text: str) -> None:
    await asyncio.to_thread(
        db.add_bot_message, conversation_id, "assistant", text, None, None
    )


def take_pending_for_reply(conversation_id: str) -> "str | None":
    """inbound_pipeline 组回复前调：取一条未过期寄存，取出即标记已投。

    desire 再门槛（≥0.4）在入站热路径上省略 —— 寄存本身已过全闸，
    对方主动来了就是最好的时机信号，再卡一次反而丢「终于说出口」的戏。
    """
    pending = db.get_pending_proactive(conversation_id)
    if not pending:
        return None
    db.mark_proactive_delivered(pending["id"])
    return pending.get("text") or ""


class ProactiveScheduler:
    """60s + 抖动轮询。范式同 agent/schedule_engine（轮询-到期-触发），
    但独立实现：schedule_engine 与无头 run 强耦合，import 会带进反向依赖。"""

    INTERVAL_S = 60.0

    def __init__(self):
        self._task: "asyncio.Task | None" = None
        self._stop = asyncio.Event()

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 —— 调度器永不被单轮失败拖死
                logger.warning("主动性调度轮失败: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.INTERVAL_S + random.uniform(0, 15)
                )
                break
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> int:
        """一轮巡检：过闸→生成→分渠道投递 + 负反馈检查。返回触发数。"""
        targets = await asyncio.to_thread(db.list_proactive_targets)
        fired = 0
        for target in targets:
            persona_id = target["persona_id"]
            speed = target.get("relationship_speed") or "normal"
            state, gap_hours, pending_topic, cooldown_raw = await asyncio.to_thread(
                _state_snapshot, persona_id, speed
            )
            d = desire(state, speed=speed, pending_topic=pending_topic, gap_hours=gap_hours)

            local_now = datetime.now().astimezone()
            local_hour = local_now.hour + local_now.minute / 60.0
            now = datetime.now(timezone.utc)
            day_start = local_now.strftime("%Y-%m-%d 00:00:00")
            last_send = _parse(await asyncio.to_thread(db.last_proactive_at, persona_id))
            cooldown_until = _parse(cooldown_raw)
            sent_today = await asyncio.to_thread(
                db.proactive_sent_today, persona_id, day_start
            )
            ok, reason = gate(
                d, speed=speed, local_hour=local_hour, last_send_at=last_send,
                cooldown_until=cooldown_until, sent_today=sent_today, now=now,
            )
            if not ok:
                await asyncio.to_thread(
                    db.add_bot_proactive_log,
                    persona_id, target["conversation_id"], d, "skip", reason,
                )
                continue

            state_desc = state_mod.describe_state(state, local_hour, speed)
            persona = await asyncio.to_thread(db.get_bot_persona, persona_id)
            if not persona:
                continue
            text = await _compose(persona, state_desc)
            if not text:
                continue
            channel = target.get("channel") or "local"
            expires_at = None
            if channel == "local":
                action = "send"
            else:
                # QQ：协议上无法主动发，寄存 48h，等对方下次来消息时捎上
                action = "queued"
                expires_at = (now + timedelta(hours=48)).strftime(_TS_FMT)
            await asyncio.to_thread(
                db.add_bot_proactive_log,
                persona_id, target["conversation_id"], d, action, "ok", text, expires_at,
            )
            if action == "send":
                await _fire_local(persona, target["conversation_id"], text)
            fired += 1

        await self._check_cold_replies()
        return fired

    async def _check_cold_replies(self) -> None:
        """负反馈：投出 2h 内用户没回话 → 冷却阶梯记一笔（每次只查一次）。"""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=2)).strftime(_TS_FMT)
        unchecked = await asyncio.to_thread(db.list_unchecked_proactive, cutoff)
        for entry in unchecked:
            await asyncio.to_thread(db.mark_proactive_checked, entry["id"])
            replied = await asyncio.to_thread(
                db.bot_conversation_has_user_reply_since,
                entry["conversation_id"], entry["delivered_at"],
            )
            if replied:
                continue
            cold_count = await asyncio.to_thread(_count_past_colds, entry["persona_id"])
            cooldown = cold_cooldown_after(cold_count)
            until = (now + cooldown).strftime(_TS_FMT)
            await asyncio.to_thread(
                db.add_bot_proactive_log,
                entry["persona_id"], entry["conversation_id"], entry["desire"],
                "skip", f"cold:{cold_count}:{until}",
            )
            # 冷却写进关系状态行（P1 的 proactive_cooldown_until 列）
            await asyncio.to_thread(
                db.set_bot_proactive_cooldown, entry["persona_id"], until
            )
            logger.info(
                "主动消息冷场（第 %d 次）：%s 冷却至 %s", cold_count + 1,
                entry["persona_id"], until,
            )


_scheduler: "ProactiveScheduler | None" = None


def get_scheduler() -> ProactiveScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ProactiveScheduler()
    return _scheduler
