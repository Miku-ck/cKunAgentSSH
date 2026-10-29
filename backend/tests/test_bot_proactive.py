"""P3 主动性：desire、四重抑制闸、寄存队列、负反馈冷却。

全部纯函数 + 真实 SQLite 夹具，mock 时钟可重放。
"""
from datetime import datetime, timedelta, timezone

import pytest

import database as db
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
        speed="normal", local_hour=15.0, last_send_at=None, cooldown_until=None,
        sent_today=0, now=NOW,
    )
    base.update(kw)
    ok, _reason = pro.gate(0.8, **base)
    return ok


def test_gate_allows_when_all_clear():
    assert _gate_ok()


def test_gate_min_interval_by_speed():
    recent = NOW - timedelta(hours=11)
    assert not _gate_ok(speed="fast", last_send_at=recent)      # fast 最小 12h
    assert not _gate_ok(last_send_at=NOW - timedelta(hours=19))  # normal 20h
    assert _gate_ok(speed="fast", last_send_at=NOW - timedelta(hours=13))


def test_gate_quiet_window():
    assert not _gate_ok(local_hour=23.6)  # 23:36 静音
    assert not _gate_ok(local_hour=5.0)   # 凌晨静音
    assert _gate_ok(local_hour=8.5)       # 8:30 起解禁


def test_gate_cold_cooldown():
    until = NOW + timedelta(hours=10)
    assert not _gate_ok(cooldown_until=until)
    expired = NOW - timedelta(hours=1)
    assert _gate_ok(cooldown_until=expired)


def test_gate_daily_quota():
    assert not _gate_ok(sent_today=1)
    assert _gate_ok(sent_today=0)


def test_gate_low_desire_rejected_first():
    ok, reason = pro.gate(0.3, local_hour=15.0, last_send_at=None,
                          cooldown_until=None, sent_today=0, now=NOW)
    assert not ok and "desire" in reason


def test_cold_cooldown_escalates():
    assert pro.cold_cooldown_after(0) == timedelta(hours=48)
    assert pro.cold_cooldown_after(1) == timedelta(hours=96)
    assert pro.cold_cooldown_after(2) == timedelta(hours=168)
    assert pro.cold_cooldown_after(10) == timedelta(hours=168)  # 封顶


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
