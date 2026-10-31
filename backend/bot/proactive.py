"""陪聊主动性：她会主动找你说话。

三个纯函数 + 一个调度器，0 次额外模型调用用于**决策**（desire 由状态推导），
只有真正开口那一次才调模型生成「心里话」。

分渠道（协议约束，spec 2026-10-30-qq-proactive-message）：
- local：直投 —— 主动消息直接落 bot_messages（assistant），前端进会话即见；
- QQ 单聊：真主动。先试官方「互动召回」（当天 / 1-3 / 3-7 / 7-30 天各一次，
  is_wakeup=true），本档用掉走普通主动消息；平台拒绝（无权限/拒收）降级寄存；
- QQ 群聊：真主动，但要人格的群参与档开到 proactive 才进巡检清单（每群各自
  12h 一次、每日 2 条上限，不与人设名下的单聊共用额度）。群主没开「主动推送
  消息能力」时平台回 40034105，降级为寄存（spec 2026-10-30-qq-group-ambient §3）。

寄存有效期 48h，inbound_pipeline 在组回复前取出以「对了，」接入。

手动探口（ProactiveScheduler.probe，「试一句」）：绕开下面所有闸真发一句，但
照写账本、照占 12h 间隔与日配额 —— 只是把「该不该说」这一步交给人按按钮，
不是把约束拆掉。唯一豁免是负反馈冷却：试发没人接是调试现场，不是她说话让人
不想理，不能一次演示就把人设冻 48h。

抑制器四重闸（全部可 mock 时钟单测）：最小间隔 12h、静音窗、负反馈指数冷却、
每日配额。负反馈 = 主动消息投出 2h 内用户没回话 → 冷却 48h 起步，二次 ×2，
三次禁 7 天。平台直接拒收（skip）不占配额，但另有 30 分钟退避：频控按小时
恢复，每 60s 重试一次只会反复撞同一堵墙。
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import database as db
from bot import memory as memory_mod
from bot import relation as relation_mod
from bot import state as state_mod
from bot.memory import deterministic_uniform
from bot.plan import PLAN_FOLLOWUP_H
from bot.timeutil import fmt_ts as _fmt_ts, parse_ts as _parse

logger = logging.getLogger(__name__)

# ── 纯函数区（不碰 IO，全部可重放）──

FIRE_THRESHOLD = 0.55
# 负反馈冷却阶梯：第 1/2/3 次冷场
COLD_STEPS_H = (48.0, 96.0, 168.0)
QUIET_WINDOW = (23.5, 8.5)  # 本地小时；跨午夜
# 产品口径：大约每 12 小时最多一次。配额只做天花板，静音窗再削掉半夜。
DAILY_QUOTA = 2
MIN_INTERVAL_H = 12.0
# 关系节奏不再拉开间隔，而是通过「久不见」项影响 desire。
EXPECTED_GAP_H = {"fast": 24.0, "normal": 60.0, "slow": 120.0}
# 寄存的有效期：过了这个点就当她没说过（调度器会标 expired）
QUEUE_TTL_H = 48.0
# 互动召回分档（官方：用户主动对话后 30 天内 4 个周期各 1 条）
WAKEUP_PERIODS_H = ((0.0, 24.0), (24.0, 72.0), (72.0, 168.0), (168.0, 720.0))
COLD_REPLY_THRESHOLD = 0.4  # 寄存消息捎带时对当下 desire 的再门槛
PROACTIVE_MAX_TOKENS = 256
# 投递被平台拒了之后的退避：skip 不推进开口闸，不退避就是每 60s 重烧一次模型。
PUSH_BACKOFF_MIN = 30.0
# 手动探口那句的账本标记：负反馈检查只认它，间隔/配额/召回档照旧统计。
MANUAL_REASON = "manual:"


def _sigmoid(x: float) -> float:
    import math
    return 1.0 / (1.0 + math.exp(-x))


def desire(
    state: dict,
    *,
    speed: str = "normal",
    pending_topic: bool = False,
    gap_hours: float = 0.0,
    plan_due: bool = False,
) -> float:
    """「想找你」的意愿，0..1。

    权重即产品判断：关系不熟不打扰（0.30）、话说一半最硬（0.25）、
    计划到期了（0.40，独立项：有时效，过期作废）、有新鲜事想分享（0.20）、
    久不见会想念（0.15）、心情好想说话（0.10）；
    有疙瘩不想先低头（-0.60）、没精力不想说话（-0.50）。

    plan_due 单独拿 0.40 而不是复用 pending_topic 的 0.25：算过数才知道
    0.25 对陌生人根本过不了 0.55（intimacy=10 时 0.30 项只有 0.081）。
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
        + 0.40 * (1.0 if plan_due else 0.0)
        + 0.15 * miss
        + 0.10 * max(0.0, valence)
        - 0.60 * (tension / 100.0)
        - 0.50 * max(0.0, 0.5 - energy)
    )
    # 关系硬底线：不熟的人（intimacy<20）哪怕话说一半也不打扰 —— 未完话题
    # 是最硬的钩子，但那是对熟人的；对陌生人它只是"还不知道你叫什么"。
    # 计划回溯例外：他说过的事，对陌生关系也是最自然的开口依据（计划有 36h
    # 时效窗，错过就作废）。四重抑制闸仍然全程生效。
    if intimacy < 20.0 and not plan_due:
        value = min(value, FIRE_THRESHOLD - 0.01)
    return max(0.0, min(1.0, value))


def _in_quiet_window(local_hour: float) -> bool:
    start, end = QUIET_WINDOW
    return local_hour >= start or local_hour < end


def gate(
    desire_value: float,
    *,
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
    if last_send_at is not None and (now - last_send_at) < timedelta(hours=MIN_INTERVAL_H):
        return False, f"min_interval {MIN_INTERVAL_H:g}h"
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


def wakeup_period(gap_hours: float) -> "int | None":
    """距用户上次说话落到哪一档召回（0/1/2/3）；超 30 天或负数 → None。"""
    gap = max(0.0, float(gap_hours))
    for index, (start, end) in enumerate(WAKEUP_PERIODS_H):
        if start <= gap < end:
            return index
    return None


def should_wakeup(
    conversation_key: str, gap_hours: float, used_periods: "set[int]"
) -> "int | None":
    """该发互动召回吗？返回档位号，不发返回 None。

    只有单聊有召回能力（群聊无 is_wakeup），且同一档只能撩一次 —— 平台会再兜一次
    （40034122 召回已达区间上限），本地记账是为了不浪费一次模型与 HTTP 往返。
    """
    if not (conversation_key or "").startswith("c2c:"):
        return None
    period = wakeup_period(gap_hours)
    if period is None or period in used_periods:
        return None
    return period


# ── IO 区 ──


def _is_group(target: dict) -> bool:
    """该巡检目标是不是群聊会话。"""
    return (target.get("external_id") or "").startswith("group:")


async def _suppressors_for(target: dict) -> "tuple[datetime | None, int]":
    """间隔与配额读数：(上次开口时刻, 今日已开口数)。自动轮与手动探口读同一份。

    两套口径不是可以共用一个数的事：
    - 群：间隔与日配额都按会话算，一个群热闹不该挤掉另一个群的额度；
    - 单聊：exclude_groups 把群的开口剔掉，否则两个群各发一条就能把人设
      当天的单聊主动性饿死（每群独立，spec 2026-10-30-qq-group-ambient §3）。
    """
    persona_id = target["persona_id"]
    conversation_id = target["conversation_id"]
    day_start = datetime.now().astimezone().strftime("%Y-%m-%d 00:00:00")
    if _is_group(target):
        last_send = _parse(await asyncio.to_thread(
            db.last_proactive_at_for_conversation, conversation_id
        ))
        sent_today = await asyncio.to_thread(
            db.proactive_sent_today_for_conversation, conversation_id, day_start
        )
        return last_send, sent_today
    last_send = _parse(await asyncio.to_thread(
        db.last_proactive_at, persona_id, exclude_groups=True
    ))
    sent_today = await asyncio.to_thread(
        db.proactive_sent_today, persona_id, day_start, exclude_groups=True
    )
    return last_send, sent_today


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


def used_wakeup_periods(conversation_id: str) -> "set[int]":
    """该会话已经用掉的召回档位（reason 里的 wakeup{n}: 标记）。"""
    return {
        index
        for index in range(len(WAKEUP_PERIODS_H))
        if db.has_wakeup_period(conversation_id, index)
    }


def _proactive_sender(channel_id: str):
    """取该渠道的主动发送能力；未连接或平台不支持返回 None。

    刻意用惰性导入：channel_runtime 拉起 chat_service / qq_manager，proactive 是
    它们的下游，模块级互相 import 会成环。
    """
    if not channel_id:
        return None
    try:
        from bot.channel_runtime import get_channel_runtime

        handle = get_channel_runtime().handle(channel_id)
    except Exception as exc:  # noqa: BLE001 —— 发送能力缺失只降级，不拖垮调度
        logger.warning("主动消息渠道不可用: %s", exc)
        return None
    return getattr(handle, "send_proactive", None)


def _state_snapshot(persona_id: str, speed: str) -> "tuple[dict, float, bool, str | None, str]":
    """(状态向量, 距上次互动小时数, 话说一半?, 冷却截止, 关系阶段)。

    pending_topic / proactive_cooldown_until 不在状态向量里（那是
    relation.record_turn 的领地），直接从账本行取。阶段走 relation 的
    score 口径——全项目唯一阶段事实源，与 UI 进度条、聊天 prompt 同源。
    """
    row = db.get_bot_relation(persona_id) or {}
    state = state_mod.state_from_row(row)
    local_now = datetime.now().astimezone()
    local_hour = local_now.hour + local_now.minute / 60.0
    state = state_mod.decay(state, datetime.now(timezone.utc), speed, local_hour)
    gap_hours = 1e9
    if row and row.get("last_at"):
        last = _parse(row["last_at"])
        if last:
            gap_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    pending = bool(row.get("pending_topic"))
    cooldown = row.get("proactive_cooldown_until")
    stage = relation_mod.stage_from_row(row, speed)["stage"]
    return state, gap_hours, pending, cooldown, stage


async def _compose(
    persona: dict, state_desc: str, topic: "tuple[str, str] | None" = None, *,
    channel: str = "local",
) -> str:
    """让模型替她说一句「心里话」。极小预算、无工具、禁思考。"""
    from bot.character import SAFETY_CHARTER  # noqa: F401 —— 确保人格口径一致
    from bot.chat_service import BotChatService
    from llm_adapter import chat_stream

    svc = BotChatService()
    model = await asyncio.to_thread(svc._model_for_persona, persona)
    # 渠道要传对：QQ 发出去是纯文本，Markdown 会原样显示成符号。
    # 必须走实例而不是类：拿 `BotChatService._system_prompt(...)` 调时，persona 会被
    # 当成 self、channel 当成 persona，下一行就在字符串上 .get() 当场炸。
    system = svc._system_prompt(persona, channel, state_desc=state_desc)
    # 有选题时把「说什么」钉住：别报菜名、别一字不差背原话，让她用自己的口气接
    if topic:
        ask = (
            "（内部指令，别当对话）你想主动找他说件事，围绕这件事开口："
            f"{topic[0]}。自然口语，只输出消息本身，1-2 条，空行分隔，"
            "别解释为什么发，也别把原话一字不差地背出来。"
        )
    else:
        ask = (
            "（内部指令，别当对话）你现在想起了一件事或单纯想他了，"
            "主动给他发一条消息。只输出消息本身，1-2 条，空行分隔，"
            "自然口语，别解释为什么发。"
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": ask},
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


# ── 选题器：她开口时说「什么」（0 次额外模型调用，确定性可重放）──

TOPIC_FRESH_DAYS = 3.0   # 近窗新事实：刚记下没几天的事才够「新鲜」
TOPIC_OLD_P = 0.12       # 旧事钩子的每日触发概率（确定性 RNG）


class Topic(NamedTuple):
    """选题：给 _compose 的描述 + 来源标记 + 计划钩子 id（仅 plan_due 非空）。"""

    describe: str
    source: str
    plan_id: str = ""


def plan_due_topic(persona_id: str, now: datetime) -> "Topic | None":
    """到期可回溯的计划选题：窗口结束后 36h 内，最早到期的那件事先问。

    文案不出现时间词、不出现具体日期：相对时间在几天后才说出口是错乱的。
    """
    # 落库比较用 UTC 字符串：传进来的若是本地 aware datetime，不换就会错 8 小时
    now = now.astimezone(timezone.utc)
    row = db.due_plan_hook(
        persona_id,
        _fmt_ts(now),
        _fmt_ts(now - timedelta(hours=PLAN_FOLLOWUP_H)),
    )
    if not row:
        return None
    return Topic(
        f"他之前提过：{row['plan_text']}，问问后来怎么样了",
        "plan_due",
        row["id"],
    )


def _fresh_fact_topic(rows: "list[dict]", now: datetime) -> "dict | None":
    """近窗最新一条记忆。rows 按 updated_at 新→旧，首条超窗即整表超窗。"""
    for row in rows[:1]:
        ts = _parse(row.get("updated_at"))
        if ts and (now - ts).total_seconds() <= TOPIC_FRESH_DAYS * 86400.0:
            return row
    return None


def _older_rows(rows: "list[dict]", now: datetime) -> "list[dict]":
    """超出新鲜窗的记忆，按新→旧（旧事钩子只从这里翻）。"""
    out: list[dict] = []
    for row in rows:
        ts = _parse(row.get("updated_at"))
        if ts and (now - ts).total_seconds() > TOPIC_FRESH_DAYS * 86400.0:
            out.append(row)
    return out


def pick_topic(
    persona_id: str, state: dict, *, now: "datetime | None" = None
) -> "Topic | None":
    """主动消息的选题器：同一输入永远同一选题，可重放可单测。

    优先级即产品判断：
    1. fatigue_care —— 上一轮停在他喊累：人还没缓过来就别聊别的；
    2. plan_due —— 他说过的事到期了（窗口结束后 36h 内）：最具体的开口依据；
    3. fresh_fact —— 近 3 天新记的事实；
    4. old_memory —— 旧事钩子，确定性 RNG 低频翻一件，演出「忽然想起」。

    返回 Topic（与二元组下标/解包兼容）；今天没得说返回 None。
    """
    now = now or datetime.now(timezone.utc)
    if (state.get("last_event_kind") or "") == "fatigue":
        return Topic("他上次聊到很累，问问他现在缓过来没有", "fatigue_care")
    due = plan_due_topic(persona_id, now)
    if due is not None:
        return due
    rows = memory_mod.list_memories(persona_id)
    fresh = _fresh_fact_topic(rows, now)
    if fresh:
        return Topic(fresh["content"], "fresh_fact")
    older = _older_rows(rows, now)
    if older and deterministic_uniform(f"topic-old:{persona_id}", now) < TOPIC_OLD_P:
        index = int(deterministic_uniform(f"topic-pick:{persona_id}", now) * len(older))
        return Topic(older[index]["content"], "old_memory")
    return None


class ProactiveScheduler:
    """60s + 抖动轮询。范式同 agent/schedule_engine（轮询-到期-触发），
    但独立实现：schedule_engine 与无头 run 强耦合，import 会带进反向依赖。"""

    INTERVAL_S = 60.0

    def __init__(self):
        self._task: "asyncio.Task | None" = None
        self._stop = asyncio.Event()
        self._maintain_cursor = 0

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
        """一轮巡检：过期清理→过闸→生成→分渠道投递 + 负反馈检查。返回触发数。"""
        now = datetime.now(timezone.utc)
        # 过期寄存每轮收一次：不清的话 48h 前的「心里话」会永远躺在队列里
        await asyncio.to_thread(db.expire_stale_proactive, _fmt_ts(now))
        # 计划钩子同样每轮收一次：超窗没问出去的 pending 置 expired
        await asyncio.to_thread(
            db.expire_plan_hooks,
            _fmt_ts(now - timedelta(hours=PLAN_FOLLOWUP_H)),
        )
        targets = await asyncio.to_thread(db.list_proactive_targets)
        fired = 0
        for target in targets:
            persona_id = target["persona_id"]
            conversation_id = target["conversation_id"]
            channel = target.get("channel") or "local"
            speed = target.get("relationship_speed") or "normal"

            # 已经憋着一句没等到人：先让她把那一句说出去，别再生成第二句。
            # 不落 skip 日志：这一步每 60s 会命中一次，写库会把账本淹了。
            if channel != "local" and await asyncio.to_thread(
                db.get_pending_proactive, conversation_id
            ):
                continue

            # 刚刚投出去被平台拒了（频控/内容/网络）：这一类不推进开口闸，
            # 不退避就会每 60s 重生成一句、再 skip 一次，模型与账本一起刷爆。
            # 同样不写 skip 日志：这一步也是每轮都命中。
            last_fail = _parse(await asyncio.to_thread(
                db.last_proactive_push_failure_at, conversation_id
            ))
            if last_fail and (now - last_fail) < timedelta(minutes=PUSH_BACKOFF_MIN):
                continue

            state, gap_hours, pending_topic, cooldown_raw, stage = await asyncio.to_thread(
                _state_snapshot, persona_id, speed
            )
            # 钩子查询炸了不能拖垮整轮巡检（spec §10）：当作没有到期计划继续走
            try:
                due = await asyncio.to_thread(plan_due_topic, persona_id, now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("计划到期查询失败: %s", exc)
                due = None
            d = desire(
                state, speed=speed, pending_topic=pending_topic,
                gap_hours=gap_hours, plan_due=due is not None,
            )

            local_now = datetime.now().astimezone()
            local_hour = local_now.hour + local_now.minute / 60.0
            last_send, sent_today = await _suppressors_for(target)
            cooldown_until = _parse(cooldown_raw)
            ok, reason = gate(
                d, local_hour=local_hour, last_send_at=last_send,
                cooldown_until=cooldown_until, sent_today=sent_today, now=now,
            )
            if not ok:
                await asyncio.to_thread(
                    db.add_bot_proactive_log,
                    persona_id, conversation_id, d, "skip", reason,
                )
                continue

            state_desc = state_mod.describe_state(state, local_hour, speed, stage=stage)
            persona = await asyncio.to_thread(db.get_bot_persona, persona_id)
            if not persona:
                continue
            # 选题失败（记忆层炸了等）不阻断主动性：回退到无选题的自由发挥
            try:
                topic = await asyncio.to_thread(pick_topic, persona_id, state, now=now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("主动消息选题失败: %s", exc)
                topic = None
            text = await _compose(persona, state_desc, topic=topic, channel=channel)
            if not text:
                continue
            if await self._deliver(
                target, persona, text, desire_value=d, topic=topic, now=now
            ):
                fired += 1

        await self._check_cold_replies()
        await self._maintain_memories()
        return fired

    async def probe(self, target: dict) -> dict:
        """手动探口：不等「聊熟」也不等窗口，真发一句并回显它去哪了。

        自动路径对新人格几乎是空转：intimacy 建表默认 5，而 desire 有不到 20
        就不开口的硬底线 —— 档位、静音窗、配额调到什么程度都看不见效果。
        这里把抑制器照算一遍但不拦：结果（本来差多少、卡在哪一道闸）原样
        回显给操作者，账本、间隔、配额照旧推进 —— 不拿「只是试一下」骗自己。
        """
        persona_id = target["persona_id"]
        conversation_id = target["conversation_id"]
        channel = target.get("channel") or "local"
        speed = target.get("relationship_speed") or "normal"

        persona = await asyncio.to_thread(db.get_bot_persona, persona_id)
        if not persona:
            # 人格都没了就不必再烧一次模型：当场报 missing 比拿空 persona 生成一句好。
            return {
                "conversation_id": conversation_id, "action": "missing",
                "desire": 0.0, "gate_reason": "persona not found",
                "text": "", "detail": "",
            }

        state, gap_hours, pending_topic, cooldown_raw, stage = await asyncio.to_thread(
            _state_snapshot, persona_id, speed
        )
        now = datetime.now(timezone.utc)
        try:
            due = await asyncio.to_thread(plan_due_topic, persona_id, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("计划到期查询失败: %s", exc)
            due = None
        d = desire(
            state, speed=speed, pending_topic=pending_topic,
            gap_hours=gap_hours, plan_due=due is not None,
        )
        local_now = datetime.now().astimezone()
        local_hour = local_now.hour + local_now.minute / 60.0
        last_send, sent_today = await _suppressors_for(target)
        # 算而不拦：原因是要给人看的正文（「本来这句话过不了闸」就是想看的东西）。
        # 过得去时留空串：前端据此决定要不要挂那句「本来会卡在」，
        # 把 gate 的 "ok" 原样带回去等于每句话都谎称「本来会被拦」。
        _ok, why = gate(
            d, local_hour=local_hour, last_send_at=last_send,
            cooldown_until=_parse(cooldown_raw), sent_today=sent_today,
        )
        gate_reason = "" if _ok else why

        state_desc = state_mod.describe_state(state, local_hour, speed, stage=stage)
        try:
            topic = await asyncio.to_thread(pick_topic, persona_id, state, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("主动消息选题失败: %s", exc)
            topic = None
        text = await _compose(persona, state_desc, topic=topic, channel=channel)
        if not text:
            return {
                "conversation_id": conversation_id, "action": "skip",
                "desire": d, "gate_reason": gate_reason,
                "text": "", "detail": "模型没生成出一句话",
            }

        await self._deliver(
            target, persona, text, desire_value=d, topic=topic,
            now=now, manual=True,
        )
        # 回显念的就是账本上那一行：屏幕上的结论与落库结果不会两套口径。
        row = await asyncio.to_thread(db.latest_proactive_row, conversation_id) or {}
        return {
            "conversation_id": conversation_id,
            "action": row.get("action") or "skip",
            "desire": d,
            "gate_reason": gate_reason,
            "text": text,
            "detail": row.get("reason") or "",
        }

    async def _deliver(
        self, target: dict, persona: dict, text: str, *,
        desire_value: float, topic: "tuple[str, str] | None", now: datetime,
        manual: bool = False,
    ) -> bool:
        """按渠道投递。返回「这句话是不是真说出去了/憋在手里了」。

        manual=True 只多干一件事：给 reason 挂上 manual: 标记。落账、间隔、
        配额、寄存、召回档全部走同一条路 —— 试一句与自动开口的区别只到
        负反馈那一层为止。
        """
        persona_id = target["persona_id"]
        conversation_id = target["conversation_id"]
        channel = target.get("channel") or "local"
        pfx = MANUAL_REASON if manual else ""
        # 选题来源供调 θ（ok:fresh_fact / ok:fatigue_care / ok:old_memory / ok:plan_due / ok:free）
        src = topic[1] if topic else "free"

        async def _mark_spoken() -> None:
            # 用 getattr 而不是 topic.plan_id：既有用例（如
            # test_deliver_records_a_pushed_message_as_delivered）仍用二元组传 topic，
            # 直接取属性会 AttributeError。
            plan_id = getattr(topic, "plan_id", "")
            if plan_id:
                await asyncio.to_thread(db.mark_plan_asked, plan_id, _fmt_ts(now))

        if channel == "local":
            await asyncio.to_thread(
                db.add_bot_proactive_log,
                persona_id, conversation_id, desire_value, "send", f"{pfx}ok:{src}", text,
            )
            await _fire_local(persona, conversation_id, text)
            await _mark_spoken()
            return True

        action, tag, period = await self._push_qq(target, text, now=now)
        if action == "push":
            prefix = f"wakeup{period}:" if period is not None else ""
            await asyncio.to_thread(
                db.add_bot_proactive_log,
                persona_id, conversation_id, desire_value, "push",
                f"{pfx}{prefix}ok:{src}", text, None, _fmt_ts(now),
            )
            await _mark_spoken()
            return True
        if action == "skip":
            await asyncio.to_thread(
                db.add_bot_proactive_log,
                persona_id, conversation_id, desire_value, "skip", f"{pfx}push:{tag}", text,
            )
            return False

        # fallback：平台不让主动发（群聊/离线/无权限）→ 存一句，等她下次来
        expires_at = _fmt_ts(now + timedelta(hours=QUEUE_TTL_H))
        await asyncio.to_thread(
            db.add_bot_proactive_log,
            persona_id, conversation_id, desire_value, "queued",
            f"{pfx}queued:{tag}:{src}", text, expires_at,
        )
        # 寄存也算说出口：等她下次来时这句会捎上，这件事不必再问一遍
        await _mark_spoken()
        logger.info("QQ 主动消息降级寄存（%s）：%s", tag, conversation_id)
        return True

    async def _push_qq(
        self, target: dict, text: str, *, now: datetime
    ) -> "tuple[str, str, int | None]":
        """QQ 两个会话类型都尽量真主动。返回 (动作, 失败标签, 召回档位)。

        动作：push=已发出，skip=平台拒了等下一轮，fallback=改成寄存。
        群聊发的是不带 msg_id 的主动消息：需要群主开「主动推送消息能力」，
        否则平台回 40034105（这里表现为 permission）；群聊也没有召回这个东西，
        召回档位恒为 None。
        """
        conversation_id = target["conversation_id"]
        conversation_key = target.get("external_id") or ""
        sender = _proactive_sender(target.get("bot_channel_id") or "")
        if sender is None:
            return "fallback", "offline", None
        if not conversation_key.startswith(("c2c:", "group:")):
            return "fallback", "group", None
        if _is_group(target):
            kind, detail = await sender(conversation_key, text, wakeup=False)
            if kind == "ok":
                return "push", "", None
            if kind in ("permission", "invalid", "offline"):
                return "fallback", kind, None
            logger.info("QQ 群主动消息未发出（%s）：%s", kind, detail)
            return "skip", kind, None

        period = None
        last_user_at = _parse(await asyncio.to_thread(db.last_user_message_at, conversation_id))
        if last_user_at is not None:
            gap_user_h = (now - last_user_at).total_seconds() / 3600.0
            used = await asyncio.to_thread(used_wakeup_periods, conversation_id)
            period = should_wakeup(conversation_key, gap_user_h, used)

        kind, detail = await sender(conversation_key, text, wakeup=period is not None)
        if kind == "ok":
            return "push", "", period
        if kind == "wakeup_limit" and period is not None:
            # 平台说这一档召回已经用掉：同一句话退回普通主动消息
            kind, detail = await sender(conversation_key, text, wakeup=False)
            if kind == "ok":
                return "push", "", None
        if kind in ("permission", "invalid", "offline"):
            return "fallback", kind, None
        logger.info("QQ 主动消息未发出（%s）：%s", kind, detail)
        return "skip", kind, None

    async def _maintain_memories(self) -> None:
        """记忆治理搭车：每轮最多维护一个 persona（轮转），降权 + 容量边界。

        挂在调度器尾部而不是聊天热路径：60s 一轮、单 persona、小事务。
        治理是增强项，失败只记日志，绝不拖垮主动性与后端。
        """
        try:
            personas = await asyncio.to_thread(db.list_bot_personas)
            if not personas:
                return
            self._maintain_cursor %= len(personas)
            persona = personas[self._maintain_cursor]
            self._maintain_cursor += 1
            summary = await asyncio.to_thread(
                memory_mod.maintain_persona_memories, persona["id"]
            )
            if summary.get("decayed") or summary.get("evicted"):
                logger.info(
                    "人格记忆维护 %s（%s）：降权 %d 条，淘汰 %d 条",
                    persona.get("name") or persona["id"], persona["id"],
                    summary["decayed"], summary["evicted"],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("人格记忆维护失败: %s", exc)

    async def _check_cold_replies(self) -> None:
        """负反馈：投出 2h 内用户没回话 → 冷却阶梯记一笔（每次只查一次）。"""
        now = datetime.now(timezone.utc)
        cutoff = _fmt_ts(now - timedelta(hours=2))
        unchecked = await asyncio.to_thread(db.list_unchecked_proactive, cutoff)
        for entry in unchecked:
            await asyncio.to_thread(db.mark_proactive_checked, entry["id"])
            # 负反馈：投出去 2h 没人接 → 冷却。手动试的那句不算：没人在群里
            # 接台是常态，拿它冷却人设会把所有单聊一起冻住（账照占，只退这一项）。
            if (entry.get("reason") or "").startswith(MANUAL_REASON):
                continue
            conversation = await asyncio.to_thread(
                db.get_bot_conversation, entry["conversation_id"]
            )
            # 群里「没人接话」不是冷淡：人一多谁都不回是常态，拿它冷却人设
            # 会把这个人所有单聊一起冻住。群聊只靠每群 12h 间隔自约束。
            if ((conversation or {}).get("external_id") or "").startswith("group:"):
                continue
            replied = await asyncio.to_thread(
                db.bot_conversation_has_user_reply_since,
                entry["conversation_id"], entry["delivered_at"],
            )
            if replied:
                continue
            cold_count = await asyncio.to_thread(_count_past_colds, entry["persona_id"])
            cooldown = cold_cooldown_after(cold_count)
            until = _fmt_ts(now + cooldown)
            await asyncio.to_thread(
                db.add_bot_proactive_log,
                entry["persona_id"], entry["conversation_id"], entry["desire"],
                "skip", f"cold:{cold_count}:{until}",
            )
            # 冷却写进关系状态行的 proactive_cooldown_until 列
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
