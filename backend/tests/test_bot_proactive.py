"""P3 主动性：desire、四重抑制闸、寄存队列、负反馈冷却。

全部纯函数 + 真实 SQLite 夹具，mock 时钟可重放。
"""
from datetime import datetime, timedelta, timezone

import pytest

import database as db
from bot import memory as mem
from bot import proactive as pro


# ── ① desire ──

def test_desire_low_intimacy_never_passes_threshold():
    """intimacy=10：关系不熟，任何状态组合都不过 θ（0.30 项封顶 0.22）。"""
    for pending in (False, True):
        for valence in (-1.0, 0.0, 1.0):
            d = pro.desire(
                {"intimacy": 10, "novelty": 100, "tension": 0, "valence": valence, "energy": 1.0},
                pending_topic=pending, gap_hours=1e9, speed="normal",
            )
            assert d < pro.FIRE_THRESHOLD, (pending, valence, d)


def test_desire_pending_topic_alone_can_pass():
    """话说一半 + 关系够熟：即使其他全空也能过 θ（最硬的钩子）。"""
    d = pro.desire(
        {"intimacy": 60, "novelty": 40, "tension": 0, "valence": 0, "energy": 0.8},
        pending_topic=True, gap_hours=0.0, speed="normal",
    )
    assert d >= pro.FIRE_THRESHOLD


def test_desire_tension_and_low_energy_suppress():
    """有疙瘩不想先低头（-0.60）+ 没精力（-0.50）：够熟也开不了口。"""
    base = {"intimacy": 90, "novelty": 100, "valence": 1.0, "energy": 1.0}
    high = pro.desire({**base, "tension": 0}, pending_topic=False, gap_hours=1e9)
    suppressed = pro.desire({**base, "tension": 100, "energy": 0.0}, pending_topic=False, gap_hours=1e9)
    assert suppressed < high
    assert suppressed < 0.3


# ── ② 四重抑制闸（mock 时钟）──

NOW = datetime(2026, 10, 29, 15, 0, 0, tzinfo=timezone.utc)


def _gate_ok(**kw) -> bool:
    base = dict(
        local_hour=15.0, last_send_at=None, cooldown_until=None,
        sent_today=0, now=NOW,
    )
    base.update(kw)
    ok, _reason = pro.gate(0.8, **base)
    return ok


def test_gate_allows_when_all_clear():
    assert _gate_ok()


def test_gate_min_interval_is_about_12_hours():
    """所有关系节奏统一 12h；节奏差异只留在 desire 的 EXPECTED_GAP_H 里。"""
    assert not _gate_ok(last_send_at=NOW - timedelta(hours=11))
    assert _gate_ok(last_send_at=NOW - timedelta(hours=13))


def test_gate_quiet_window():
    assert not _gate_ok(local_hour=23.6)  # 23:36 静音
    assert not _gate_ok(local_hour=5.0)   # 凌晨静音
    assert _gate_ok(local_hour=8.5)       # 8:30 起解禁


def test_gate_cold_cooldown():
    until = NOW + timedelta(hours=10)
    assert not _gate_ok(cooldown_until=until)
    expired = NOW - timedelta(hours=1)
    assert _gate_ok(cooldown_until=expired)


def test_gate_daily_quota_caps_at_two():
    """配额只是天花板：12h 间隔在自然日内本来最多两次，不能设成 1 把 12h 作废。"""
    assert _gate_ok(sent_today=1)
    assert not _gate_ok(sent_today=2)


def test_gate_low_desire_rejected_first():
    ok, reason = pro.gate(0.3, local_hour=15.0, last_send_at=None,
                          cooldown_until=None, sent_today=0, now=NOW)
    assert not ok and "desire" in reason


def test_cold_cooldown_escalates():
    assert pro.cold_cooldown_after(0) == timedelta(hours=48)
    assert pro.cold_cooldown_after(1) == timedelta(hours=96)
    assert pro.cold_cooldown_after(2) == timedelta(hours=168)
    assert pro.cold_cooldown_after(10) == timedelta(hours=168)  # 封顶


# ── ②' 互动召回分档（官方：30 天内 4 个周期各 1 条）──


def test_wakeup_period_buckets():
    cases = [
        (0.0, 0), (23.9, 0),
        (24.0, 1), (71.9, 1),
        (72.0, 2), (167.9, 2),
        (168.0, 3), (719.9, 3),
        (720.0, None),  # 超 30 天召回窗口
    ]
    for gap, expected in cases:
        assert pro.wakeup_period(gap) == expected, gap
    assert pro.wakeup_period(-5.0) == 0, "时钟漂移不该把负间隔判成不可召回"


def test_should_wakeup_only_private_and_once_per_period():
    assert pro.should_wakeup("c2c:user-1", 30, set()) == 1
    assert pro.should_wakeup("c2c:user-1", 30, {1}) is None, "同一档只撩一次"
    assert pro.should_wakeup("group:group-1", 30, set()) is None, "群聊没有召回能力"
    assert pro.should_wakeup("c2c:user-1", 800, set()) is None


# ── ③ 寄存队列 ──

def test_queued_entry_lifecycle(base_db):
    persona = db.create_bot_persona(name="晚风")
    conv = db.create_bot_conversation(persona_id=persona["id"])
    log_id = db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.7, "queued", "ok", "对了，你昨天说的那家店…",
        expires_at="2099-01-01 00:00:00",
    )
    pending = db.get_pending_proactive(conv["id"])
    assert pending and pending["id"] == log_id
    db.mark_proactive_delivered(log_id)
    assert db.get_pending_proactive(conv["id"]) is None


def test_expired_queued_not_picked(base_db):
    persona = db.create_bot_persona(name="晚风")
    conv = db.create_bot_conversation(persona_id=persona["id"])
    db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.7, "queued", "ok", "过期的话",
        expires_at="2000-01-01 00:00:00",
    )
    assert db.get_pending_proactive(conv["id"]) is None


def test_targets_include_local_and_recent_qq_only(base_db):
    persona = db.create_bot_persona(name="晚风")
    db.update_bot_persona(persona["id"], proactive_enabled=True)
    local = db.create_bot_conversation(persona_id=persona["id"], channel="local")
    qq_old = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", channel_id=None, external_id="g-old"
    )
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE bot_conversations SET updated_at = '2020-01-01 00:00:00' WHERE id = ?",
            (qq_old["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    targets = {t["conversation_id"]: t["channel"] for t in db.list_proactive_targets()}
    assert targets.get(local["id"]) == "local"
    assert qq_old["id"] not in targets, "沉寂的 QQ 会话不该进巡检清单"


def test_targets_keep_qq_conversations_inside_the_30_day_wakeup_window(base_db):
    """巡检窗口必须覆盖召回法定窗口：7 天会把「7-30 天」整档砍掉。"""
    persona = db.create_bot_persona(name="晚风")
    db.update_bot_persona(persona["id"], proactive_enabled=True)
    conv = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", channel_id=None, external_id="c2c:user-1"
    )
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE bot_conversations SET updated_at = datetime('now', '-20 days') "
            "WHERE id = ?",
            (conv["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    targets = {t["conversation_id"]: t for t in db.list_proactive_targets()}
    assert conv["id"] in targets
    assert targets[conv["id"]]["external_id"] == "c2c:user-1"


# ── ③' 记账口径：谁算「已经开过口」──


def _persona_conv(**conv_kw):
    persona = db.create_bot_persona(name="晚风")
    return persona, db.create_bot_conversation(persona_id=persona["id"], **conv_kw)


def test_send_push_and_queued_all_count_as_spoken(base_db):
    """三种「已开口」都要进间隔与配额统计，否则 QQ 侧每 60s 重说一遍。"""
    persona, conv = _persona_conv()
    for action in ("send", "push", "queued"):
        db.add_bot_proactive_log(persona["id"], conv["id"], 0.7, action, "ok")
    assert db.proactive_sent_today(persona["id"], "2000-01-01 00:00:00") == 3
    assert db.last_proactive_at(persona["id"]) is not None


def test_skip_and_expired_do_not_count_as_spoken(base_db):
    persona, conv = _persona_conv()
    db.add_bot_proactive_log(persona["id"], conv["id"], 0.3, "skip", "desire 0.30 < 0.55")
    db.add_bot_proactive_log(persona["id"], conv["id"], 0.7, "expired", "queued:offline:free")
    assert db.proactive_sent_today(persona["id"], "2000-01-01 00:00:00") == 0
    assert db.last_proactive_at(persona["id"]) is None


def test_has_wakeup_period_matches_the_reason_prefix(base_db):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.7, "push", "wakeup1:ok:fresh_fact", "在忙吗"
    )
    assert db.has_wakeup_period(conv["id"], 1) is True
    assert db.has_wakeup_period(conv["id"], 0) is False


def test_expire_stale_proactive_flips_only_overdue_stashes(base_db):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    stale = db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.7, "queued", "queued:offline:free", "旧话",
        expires_at="2000-01-01 00:00:00",
    )
    fresh = db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.7, "queued", "queued:offline:free", "新话",
        expires_at="2099-01-01 00:00:00",
    )

    assert db.expire_stale_proactive("2026-10-30 00:00:00") == 1
    pending = db.get_pending_proactive(conv["id"])
    assert pending and pending["id"] == fresh
    conn = db._get_conn()
    try:
        action = conn.execute(
            "SELECT action FROM bot_proactive_log WHERE id = ?", (stale,)
        ).fetchone()["action"]
    finally:
        conn.close()
    assert action == "expired", "过期寄存保留审计行，不物理删"


# ── ④ 负反馈冷却 ──

@pytest.mark.asyncio
async def test_cold_reply_sets_cooldown(base_db, monkeypatch):
    persona = db.create_bot_persona(name="晚风")
    conv = db.create_bot_conversation(persona_id=persona["id"])
    log_id = db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.8, "send", "ok", "在干嘛",
    )
    # 直接置 delivered_at 为 3 小时前（绕过 mark 的 now），模拟「投出后没人理」
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE bot_proactive_log SET delivered_at = datetime('now', '-3 hours') WHERE id = ?",
            (log_id,),
        )
        conn.commit()
    finally:
        conn.close()

    scheduler = pro.ProactiveScheduler()
    await scheduler._check_cold_replies()

    row = db.get_bot_relation(persona["id"])
    assert row and row.get("proactive_cooldown_until"), "冷场后必须记冷却"
    assert conn is not None
    conn2 = db._get_conn()
    try:
        cold = conn2.execute(
            "SELECT COUNT(*) AS n FROM bot_proactive_log WHERE persona_id = ? "
            "AND reason LIKE 'cold:%'",
            (persona["id"],),
        ).fetchone()
    finally:
        conn2.close()
    assert int(cold["n"]) == 1


def test_per_conversation_quota_is_independent_of_the_persona(base_db):
    """每个群各自 12h / 每日 2 条：一个群说过了，不挤掉另一个群的额度。"""
    persona = db.create_bot_persona(name="晚风")
    g1 = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g1"
    )
    g2 = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g2"
    )
    db.add_bot_proactive_log(persona["id"], g1["id"], 0.7, "push", "ok")

    assert db.proactive_sent_today_for_conversation(g1["id"], "2000-01-01 00:00:00") == 1
    assert db.proactive_sent_today_for_conversation(g2["id"], "2000-01-01 00:00:00") == 0
    assert db.last_proactive_at_for_conversation(g1["id"]) is not None
    assert db.last_proactive_at_for_conversation(g2["id"]) is None
    # 人设全量口径仍把群开口算进去（账本要看总开口数），调度器另有 exclude_groups 口径
    assert db.proactive_sent_today(persona["id"], "2000-01-01 00:00:00") == 1


def test_group_opens_do_not_eat_the_private_quota(base_db):
    """两个群各说一条，不能把这个人格当天的单聊主动性饿死。"""
    persona, c2c = _persona_conv(channel="qq", external_id="c2c:user-1")
    g1 = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g1"
    )
    g2 = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g2"
    )
    for group in (g1, g2):
        db.add_bot_proactive_log(persona["id"], group["id"], 0.7, "push", "ok")

    assert db.proactive_sent_today(persona["id"], "2000-01-01 00:00:00") == 2
    assert db.proactive_sent_today(
        persona["id"], "2000-01-01 00:00:00", exclude_groups=True
    ) == 0
    assert db.last_proactive_at(persona["id"], exclude_groups=True) is None
    assert db.last_proactive_at(persona["id"]) is not None

    db.add_bot_proactive_log(persona["id"], c2c["id"], 0.7, "push", "ok")
    assert db.proactive_sent_today(
        persona["id"], "2000-01-01 00:00:00", exclude_groups=True
    ) == 1


def test_push_failure_is_visible_for_the_backoff(base_db):
    """退避只认「生成好了但平台没收下」的 skip：闸内 skip 不算失败。"""
    persona, conv = _persona_conv(channel="qq", external_id="group:g1")
    assert db.last_proactive_push_failure_at(conv["id"]) is None
    db.add_bot_proactive_log(persona["id"], conv["id"], 0.3, "skip", "desire 0.30 < 0.55")
    assert db.last_proactive_push_failure_at(conv["id"]) is None, "闸内 skip 不该触发退避"
    db.add_bot_proactive_log(persona["id"], conv["id"], 0.7, "skip", "push:quota")
    assert db.last_proactive_push_failure_at(conv["id"]) is not None


def test_conversation_quota_counts_queued_but_not_skips(base_db):
    persona = db.create_bot_persona(name="晚风")
    conv = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g1"
    )
    db.add_bot_proactive_log(persona["id"], conv["id"], 0.7, "queued", "queued:permission:free")
    db.add_bot_proactive_log(persona["id"], conv["id"], 0.3, "skip", "desire 0.30 < 0.55")

    assert db.proactive_sent_today_for_conversation(conv["id"], "2000-01-01 00:00:00") == 1


# ── ④' 群聊不参与冷场冷却 ──

@pytest.mark.asyncio
async def test_group_push_is_not_cold_checked(base_db):
    """群里没人接话是常态：不能因此把这个人设的所有单聊一起冻住。"""
    persona = db.create_bot_persona(name="晚风")
    conv = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g1"
    )
    log_id = db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.8, "push", "ok", "晚上开黑吗",
    )
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE bot_proactive_log SET delivered_at = datetime('now', '-3 hours') WHERE id = ?",
            (log_id,),
        )
        conn.commit()
    finally:
        conn.close()

    await pro.ProactiveScheduler()._check_cold_replies()

    row = db.get_bot_relation(persona["id"])
    assert not (row or {}).get("proactive_cooldown_until"), "群聊冷场不该记冷却"


# ── ⑤ 调度器开关：未开启人格时 tick 零触发、零模型调用 ──

@pytest.mark.asyncio
async def test_tick_no_targets_no_llm(base_db, monkeypatch):
    called = []

    async def fake_stream(*a, **kw):
        called.append(kw)
        yield type("C", (), {"content": "x", "reasoning": None})()

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    scheduler = pro.ProactiveScheduler()
    fired = await scheduler.tick()
    assert fired == 0 and called == []


# ── ⑤' 调度器投递：QQ 三档（push / fallback / skip）──


TARGET_NOW = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc)


def _qq_target(persona, conv, *, external_id="c2c:user-1", bot_channel_id="ch-1"):
    return {
        "persona_id": persona["id"],
        "conversation_id": conv["id"],
        "channel": "qq",
        "external_id": external_id,
        "bot_channel_id": bot_channel_id,
    }


def _backdate_user_message(conversation_id: str, hours_ago: float) -> None:
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE bot_messages SET created_at = datetime('now', ?) "
            "WHERE conversation_id = ? AND role = 'user'",
            (f"-{hours_ago} hours", conversation_id),
        )
        conn.commit()
    finally:
        conn.close()


def _proactive_rows(persona_id: str) -> "list[dict]":
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM bot_proactive_log WHERE persona_id = ? ORDER BY created_at",
            (persona_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _stub_sender(monkeypatch, results):
    """按调用顺序返回结果的假发送器，同时记录每次的 wakeup 标记。"""
    calls = []
    queue = list(results)

    async def sender(conversation_key, text, *, wakeup=False):
        calls.append((conversation_key, text, wakeup))
        return queue.pop(0) if queue else ("ok", "")

    monkeypatch.setattr(pro, "_proactive_sender", lambda _channel_id: sender)
    return calls


@pytest.mark.asyncio
async def test_push_qq_speaks_in_groups_without_wakeup(base_db, monkeypatch):
    """群聊真主动：发不带 msg_id 的主动消息（需群主开「主动推送」）。

    群聊没有互动召回这东西，wakeup 必须恒为 False —— 带上 flag 平台会直接拒。
    """
    persona, conv = _persona_conv(channel="qq", external_id="group:group-1")
    target = _qq_target(persona, conv, external_id="group:group-1")
    calls = _stub_sender(monkeypatch, [("ok", "")])

    assert await pro.ProactiveScheduler()._push_qq(target, "在吗", now=TARGET_NOW) == (
        "push", "", None,
    )
    assert calls == [("group:group-1", "在吗", False)]


@pytest.mark.asyncio
async def test_group_push_falls_back_when_the_owner_did_not_grant_proactive(base_db, monkeypatch):
    """40034105（未开主动推送能力）反推成 permission → 降级寄存，等下次 @ 时捎上。"""
    persona, conv = _persona_conv(channel="qq", external_id="group:group-1")
    target = _qq_target(persona, conv, external_id="group:group-1")
    _stub_sender(monkeypatch, [("permission", "主动消息无权限")])

    assert await pro.ProactiveScheduler()._push_qq(target, "在吗", now=TARGET_NOW) == (
        "fallback", "permission", None,
    )


@pytest.mark.asyncio
async def test_group_push_skips_on_rate_limit_instead_of_stashing(base_db, monkeypatch):
    persona, conv = _persona_conv(channel="qq", external_id="group:group-1")
    target = _qq_target(persona, conv, external_id="group:group-1")
    _stub_sender(monkeypatch, [("quota", "主动消息超频控")])

    assert await pro.ProactiveScheduler()._push_qq(target, "在吗", now=TARGET_NOW) == (
        "skip", "quota", None,
    )


@pytest.mark.asyncio
async def test_push_qq_falls_back_when_the_bot_is_offline(base_db, monkeypatch):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    target = _qq_target(persona, conv)
    monkeypatch.setattr(pro, "_proactive_sender", lambda _channel_id: None)
    assert await pro.ProactiveScheduler()._push_qq(target, "在吗", now=TARGET_NOW) == (
        "fallback", "offline", None,
    )


@pytest.mark.asyncio
async def test_push_qq_uses_wakeup_for_a_cold_private_chat(base_db, monkeypatch):
    """用户上次说话在 48h 前 → 落在「1-3 天」档，先试一次互动召回。"""
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    db.add_bot_message(conv["id"], "user", "早")
    _backdate_user_message(conv["id"], 48)
    target = _qq_target(persona, conv)
    calls = _stub_sender(monkeypatch, [("ok", "")])

    action, tag, period = await pro.ProactiveScheduler()._push_qq(
        target, "在干嘛", now=datetime.now(timezone.utc)
    )

    assert (action, tag, period) == ("push", "", 1)
    assert calls == [("c2c:user-1", "在干嘛", True)]


@pytest.mark.asyncio
async def test_push_qq_skips_wakeup_once_the_period_is_used(base_db, monkeypatch):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    db.add_bot_message(conv["id"], "user", "早")
    _backdate_user_message(conv["id"], 48)
    db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.7, "push", "wakeup1:ok:fresh_fact", "在忙吗"
    )
    calls = _stub_sender(monkeypatch, [("ok", "")])

    action, _tag, period = await pro.ProactiveScheduler()._push_qq(
        _qq_target(persona, conv), "在干嘛", now=datetime.now(timezone.utc)
    )

    assert (action, period) == ("push", None)
    assert calls == [("c2c:user-1", "在干嘛", False)]


@pytest.mark.asyncio
async def test_push_qq_retries_plain_when_wakeup_quota_is_exhausted(base_db, monkeypatch):
    """40034122 召回已达区间上限：同一句话退回普通主动消息，别浪费这次生成。"""
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    db.add_bot_message(conv["id"], "user", "早")
    _backdate_user_message(conv["id"], 48)
    calls = _stub_sender(
        monkeypatch,
        [("wakeup_limit", "召回已达区间上限"), ("ok", "")],
    )

    action, _tag, period = await pro.ProactiveScheduler()._push_qq(
        _qq_target(persona, conv), "在干嘛", now=datetime.now(timezone.utc)
    )

    assert (action, period) == ("push", None)
    assert [flag for _key, _text, flag in calls] == [True, False]


@pytest.mark.asyncio
async def test_push_qq_queues_when_the_platform_forbids_proactive(base_db, monkeypatch):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    _stub_sender(monkeypatch, [("permission", "主动消息无权限")])

    assert await pro.ProactiveScheduler()._push_qq(
        _qq_target(persona, conv), "在吗", now=TARGET_NOW
    ) == ("fallback", "permission", None)


@pytest.mark.asyncio
async def test_push_qq_skips_on_rate_limit_instead_of_stashing(base_db, monkeypatch):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    _stub_sender(monkeypatch, [("quota", "主动消息超频控")])

    assert await pro.ProactiveScheduler()._push_qq(
        _qq_target(persona, conv), "在吗", now=TARGET_NOW
    ) == ("skip", "quota", None)


@pytest.mark.asyncio
async def test_deliver_records_a_pushed_message_as_delivered(base_db, monkeypatch):
    """push 必须落 delivered_at：2h 没回话照样要能记冷场。"""
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    scheduler = pro.ProactiveScheduler()

    async def fake_push(_target, _text, *, now):
        return "push", "", 1

    monkeypatch.setattr(scheduler, "_push_qq", fake_push)
    ok = await scheduler._deliver(
        _qq_target(persona, conv), persona, "在忙吗",
        desire_value=0.8, topic=("看展", "fresh_fact"), now=TARGET_NOW,
    )

    assert ok is True
    row = _proactive_rows(persona["id"])[-1]
    assert row["action"] == "push"
    assert row["reason"] == "wakeup1:ok:fresh_fact"
    assert row["delivered_at"]


@pytest.mark.asyncio
async def test_deliver_stashes_when_the_push_falls_back(base_db, monkeypatch):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    scheduler = pro.ProactiveScheduler()

    async def fake_push(_target, _text, *, now):
        return "fallback", "offline", None

    monkeypatch.setattr(scheduler, "_push_qq", fake_push)
    ok = await scheduler._deliver(
        _qq_target(persona, conv), persona, "在忙吗",
        desire_value=0.8, topic=None, now=TARGET_NOW,
    )

    assert ok is True
    row = _proactive_rows(persona["id"])[-1]
    assert row["action"] == "queued"
    assert row["reason"] == "queued:offline:free"
    assert row["expires_at"], "寄存必须有有效期，否则 48h 前的心里话永远躺在队列里"


@pytest.mark.asyncio
async def test_deliver_records_a_platform_refusal_as_skip(base_db, monkeypatch):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    scheduler = pro.ProactiveScheduler()

    async def fake_push(_target, _text, *, now):
        return "skip", "quota", None

    monkeypatch.setattr(scheduler, "_push_qq", fake_push)
    ok = await scheduler._deliver(
        _qq_target(persona, conv), persona, "在忙吗",
        desire_value=0.8, topic=None, now=TARGET_NOW,
    )

    assert ok is False, "没发出去就不该计入本轮触发数"
    row = _proactive_rows(persona["id"])[-1]
    assert row["action"] == "skip" and row["reason"] == "push:quota"


@pytest.mark.asyncio
async def test_tick_does_not_stack_a_second_stash(base_db, monkeypatch):
    """已经憋着一句没等到人：不再生成第二句，也不白烧一次模型。"""
    called = []

    async def fake_stream(*a, **kw):
        called.append(kw)
        yield type("C", (), {"content": "x", "reasoning": None})()

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    db.update_bot_persona(persona["id"], proactive_enabled=True)
    before = db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.7, "queued", "queued:offline:free", "憋着的话",
        expires_at="2099-01-01 00:00:00",
    )

    fired = await pro.ProactiveScheduler().tick()

    assert fired == 0 and called == []
    rows = _proactive_rows(persona["id"])
    assert [row["id"] for row in rows] == [before]


@pytest.mark.asyncio
async def test_tick_uses_the_group_quota_for_group_conversations(base_db, monkeypatch):
    """巡检到群会话时，间隔与配额读会话级口径，不读人设级口径。"""
    persona, conv = _persona_conv(channel="qq", external_id="group:group-1")
    target = _qq_target(persona, conv, external_id="group:group-1")
    monkeypatch.setattr(db, "list_proactive_targets", lambda: [target])
    monkeypatch.setattr(
        pro, "_state_snapshot",
        lambda *_a: ({"intimacy": 90, "novelty": 50, "tension": 0,
                      "valence": 1.0, "energy": 1.0}, 1.0, False, None, "朋友"),
    )
    calls: list = []
    monkeypatch.setattr(
        db, "last_proactive_at",
        lambda pid: calls.append(("persona", pid)) or None,
    )
    monkeypatch.setattr(
        db, "last_proactive_at_for_conversation",
        lambda cid: calls.append(("conversation", cid)) or None,
    )
    monkeypatch.setattr(db, "proactive_sent_today", lambda *a: calls.append("persona") or 0)
    monkeypatch.setattr(
        db, "proactive_sent_today_for_conversation",
        lambda *a: calls.append("conversation") or 0,
    )
    # 闸门直接拦下：这一轮只验证统计口径，不生成也不投递
    monkeypatch.setattr(pro, "gate", lambda *a, **kw: (False, "probe"))

    await pro.ProactiveScheduler().tick()

    assert calls == [("conversation", conv["id"]), "conversation"]


@pytest.mark.asyncio
async def test_tick_reads_the_private_quota_without_group_opens(base_db, monkeypatch):
    """单聊口径必须带 exclude_groups：群的开口不占单聊的间隔与日配额。"""
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    target = _qq_target(persona, conv, external_id="c2c:user-1")
    monkeypatch.setattr(db, "list_proactive_targets", lambda: [target])
    monkeypatch.setattr(
        pro, "_state_snapshot",
        lambda *_a: ({"intimacy": 90, "novelty": 50, "tension": 0,
                      "valence": 1.0, "energy": 1.0}, 1.0, False, None, "朋友"),
    )
    seen: list = []
    monkeypatch.setattr(
        db, "last_proactive_at",
        lambda pid, **kw: seen.append(("interval", kw)) or None,
    )
    monkeypatch.setattr(
        db, "proactive_sent_today",
        lambda *a, **kw: seen.append(("today", kw)) or 0,
    )
    monkeypatch.setattr(pro, "gate", lambda *a, **kw: (False, "probe"))

    await pro.ProactiveScheduler().tick()

    assert seen == [
        ("interval", {"exclude_groups": True}),
        ("today", {"exclude_groups": True}),
    ]


@pytest.mark.asyncio
async def test_tick_backs_off_after_a_rejected_push(base_db, monkeypatch):
    """平台刚拒过一次：下一轮不许再烧模型重试，频控不是 60 秒能恢复的。"""
    called = []

    async def fake_stream(*a, **kw):
        called.append(kw)
        yield type("C", (), {"content": "x", "reasoning": None})()

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    db.update_bot_persona(persona["id"], proactive_enabled=True)
    target = _qq_target(persona, conv, external_id="c2c:user-1")
    monkeypatch.setattr(db, "list_proactive_targets", lambda: [target])
    before = db.add_bot_proactive_log(
        persona["id"], conv["id"], 0.8, "skip", "push:quota", "刚被拒的那句"
    )

    fired = await pro.ProactiveScheduler().tick()

    assert fired == 0 and called == [], "退避期内不生成、不投递"
    assert [row["id"] for row in _proactive_rows(persona["id"])] == [before], "也不刷账本"


# ── ⑥ 选题器：0 次额外模型调用的确定性选题 ──


def _backdate_memory(memory_id: str, days_ago: int) -> None:
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE memories SET updated_at = datetime('now', ?) WHERE id = ?",
            (f"-{days_ago} days", memory_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_pick_topic_cares_about_fatigue_first(base_db):
    """上一轮停在他喊累：先问缓过来没有，新鲜事也得靠边。"""
    persona = db.create_bot_persona(name="晚风")
    mem.remember(persona["id"], "c1", "周末要去看展")
    topic = pro.pick_topic(persona["id"], {"last_event_kind": "fatigue"})
    assert topic == ("他上次聊到很累，问问他现在缓过来没有", "fatigue_care")


def test_pick_topic_shares_recent_fact(base_db):
    """近 3 天新记的事实 = 「有新鲜事想分享」，对应 desire 里的 novelty 轴。"""
    persona = db.create_bot_persona(name="晚风")
    mem.remember(persona["id"], "c1", "周末要去看展")
    topic = pro.pick_topic(persona["id"], {})
    assert topic and topic[1] == "fresh_fact" and "看展" in topic[0]


def test_pick_topic_old_memory_is_rare_and_deterministic(base_db, monkeypatch):
    """旧事钩子：确定性 RNG 触发，同一天重放同一选题，概率不满则闭嘴。"""
    persona = db.create_bot_persona(name="晚风")
    mem.remember(persona["id"], "c1", "我喜欢辣")
    row = mem.list_memories(persona["id"])[0]
    _backdate_memory(row["id"], 30)

    monkeypatch.setattr(pro, "deterministic_uniform", lambda _key, _now: 0.0)
    first = pro.pick_topic(persona["id"], {})
    second = pro.pick_topic(persona["id"], {})
    assert first and first[1] == "old_memory" and first == second

    monkeypatch.setattr(pro, "deterministic_uniform", lambda _key, _now: 0.99)
    assert pro.pick_topic(persona["id"], {}) is None


def test_pick_topic_recent_fact_is_not_old_news(base_db, monkeypatch):
    """旧事钩子只翻新鲜窗之外的账：刚记 3 天内的不算旧事。"""
    persona = db.create_bot_persona(name="晚风")
    mem.remember(persona["id"], "c1", "我喜欢辣")  # 刚记下，还在新鲜窗内
    monkeypatch.setattr(pro, "deterministic_uniform", lambda _key, _now: 0.0)
    topic = pro.pick_topic(persona["id"], {})
    assert topic and topic[1] == "fresh_fact"


def test_pick_topic_nothing_to_say(base_db):
    persona = db.create_bot_persona(name="晚风")
    assert pro.pick_topic(persona["id"], {}) is None
