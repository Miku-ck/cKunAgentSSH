"""P1 状态层测试：事件映射、软饱和、冷却、基线回归、离散注入。

验收口径（plan §6 P1）：
① praise×3 第 3 条 Δ_eff ≤ 首条 30%；② mock 时钟 +7d intimacy 收敛到基线 ±1%；
③ 全轴恒在界内；④ 同事件序列两次执行状态完全一致；
⑤ describe_state 输出含档位词、不含裸数字；⑥ chat_stream 仍恰好调用 1 次。
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

import database as db
import bot.chat_service as cs_mod
from bot import state as state_mod
from bot.chat_service import BotChatService


@pytest.fixture
def core_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-state.db"))
    yield
    db._close_all_connections()


T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


async def _recall_none(*_args, **_kwargs):
    """P2：主链路记忆召回改为 async，测试里统一静默为空。"""
    return []


def _fresh() -> dict:
    return state_mod.state_from_row(None)


# ── ① 软饱和 ──

def test_praise_repeat_saturates():
    """praise 连发：第 3 次对 valence 的有效增量 ≤ 首次的 30%（冷却 0.3^k）。"""
    state = _fresh()
    v0 = state["valence"]
    state, _ = state_mod.apply_events(state, ["praise"], T0, "normal")
    first_gain = state["valence"] - v0
    v1 = state["valence"]
    state, _ = state_mod.apply_events(state, ["praise"], T0, "normal")
    v2 = state["valence"]
    state, _ = state_mod.apply_events(state, ["praise"], T0, "normal")
    third_gain = state["valence"] - v2
    assert third_gain <= first_gain * 0.3 + 1e-9
    assert (v2 - v1) <= first_gain * 0.3 + 1e-9, "第 2 次也应打折"


def test_praise_toward_baseline_not_saturated():
    """conflict 之后 praise 拉回基线方向：负向修复不饱和（吵架后哄得好）。"""
    state = _fresh()
    state, _ = state_mod.apply_events(state, ["conflict"], T0, "normal")
    low = state["valence"]
    state, _ = state_mod.apply_events(state, ["praise"], T0, "normal")
    # praise 冷却窗 1200s 内第二次 → 0.3 折，但方向是拉回，全额生效
    assert state["valence"] >= low + 0.25 * 0.3 - 1e-9


def test_values_never_leave_bounds():
    """③ 任意事件序列后全轴恒在界内（连发 50 次也不过界）。"""
    state = _fresh()
    events = ["praise", "conflict", "excite", "overreach", "ai_probe", "chat", "burst"] * 50
    now = T0
    for event in events:
        state, _ = state_mod.apply_events(state, [event], now, "normal")
        now += timedelta(seconds=30)
    for axis, (lo, hi, _) in state_mod.AXIS_BOUNDS.items():
        assert lo <= state[axis] <= hi, axis


# ── ② 基线回归 ──

def test_decay_converges_to_baseline_not_zero():
    """+7d 不聊：intimacy 向基线（8）收敛，不是向 0 坍缩。"""
    state = _fresh()
    state["intimacy"] = 50.0
    state["state_at"] = state_mod._fmt_ts(T0)
    later = T0 + timedelta(days=7)
    out = state_mod.decay(state, later, "normal", local_hour=15.0)
    assert out["intimacy"] == pytest.approx(8.0 + (50.0 - 8.0) * 0.5 ** (7 / 14), rel=1e-3)
    assert out["intimacy"] > 8.0, "回归目标是基线，老朋友不会掉回陌生人"


def test_energy_decays_toward_circadian_baseline():
    """energy 的回归目标是昼夜基线：凌晨的精力基线低于下午。"""
    state = _fresh()
    state["energy"] = 0.6
    state["state_at"] = state_mod._fmt_ts(T0)
    night = state_mod.decay(state, T0 + timedelta(hours=12), "normal", local_hour=3.0)
    day = state_mod.decay(state, T0 + timedelta(hours=12), "normal", local_hour=15.0)
    assert night["energy"] < day["energy"]
    assert night["energy"] < 0.6, "凌晨不该还是下午的精力"


# ── ④ 确定性可重放 ──

def test_same_sequence_same_trajectory():
    """同事件序列两次执行，状态完全一致（不依赖执行时刻以外的任何东西）。"""
    def run():
        state = _fresh()
        now = T0
        for event in ["praise", "chat", "doubt", "excite", "chat", "media"]:
            state, _ = state_mod.apply_events(state, [event], now, "normal")
            now += timedelta(minutes=2)
        return state

    a, b = run(), run()
    for axis in state_mod.AXIS_BOUNDS:
        assert a[axis] == b[axis], axis


def test_cooldown_window_expiry():
    """同事件冷却窗过期后恢复全额增量。"""
    state = _fresh()
    state, _ = state_mod.apply_events(state, ["praise"], T0, "normal")
    v_after_first = state["valence"]
    state, _ = state_mod.apply_events(state, ["praise"], T0 + timedelta(seconds=60), "normal")
    discounted_gain = state["valence"] - v_after_first
    state, _ = state_mod.apply_events(state, ["praise"], T0 + timedelta(seconds=60) + timedelta(seconds=1300), "normal")
    fresh_gain = state["valence"] - v_after_first - discounted_gain
    assert fresh_gain > discounted_gain * 2, "窗外的 praise 不该还在吃 0.3 折"


# ── ⑤ 离散注入 ──

def test_describe_state_has_no_raw_numbers():
    """describe_state 只喂档位词，任何状态下都不出现裸数字。"""
    import re

    for valence in (-1.0, -0.5, 0.0, 0.3, 0.9):
        for tension in (0.0, 40.0, 80.0):
            state = _fresh() | {"valence": valence, "tension": tension}
            out = state_mod.describe_state(state, local_hour=14.0, speed="normal")
            assert "【此刻状态】" in out
            assert not re.search(r"\d", out), f"出现了裸数字: {out}"


def test_describe_state_mentions_abnormal_axes_only():
    """平静+精力正常+无疙瘩时不说精力/疙瘩；偏离时必须点名。"""
    calm = _fresh()
    out = state_mod.describe_state(calm, local_hour=14.0)
    assert "心情" in out and "关系" in out
    assert "精力" not in out and "疙瘩" not in out

    tired = _fresh() | {"energy": 0.2}
    assert "精力" in state_mod.describe_state(tired, local_hour=14.0)
    torn = _fresh() | {"tension": 70.0}
    torn_out = state_mod.describe_state(torn, local_hour=14.0)
    assert "疙瘩" in torn_out and "别追问" in torn_out


# ── 阶段双门 ──

def test_derive_stage_trust_gate():
    """亲近但没信任，不给默契；intimacy 阈值随升温档缩放（对齐旧口径）。"""
    assert state_mod.derive_stage(75, 70, "normal")["stage"] == "默契"
    assert state_mod.derive_stage(75, 40, "normal")["stage"] == "亲近", "信任不够压不住默契门"
    assert state_mod.derive_stage(30, 20, "normal")["stage"] == "亲近"
    assert state_mod.derive_stage(22, 55, "normal")["stage"] == "亲近", "i≥20 且 trust≥50 的捷径档"
    assert state_mod.derive_stage(22, 20, "normal")["stage"] == "熟悉"
    assert state_mod.derive_stage(10, 30, "normal")["stage"] == "熟悉"
    assert state_mod.derive_stage(5, 30, "normal")["stage"] == "陌生"
    # fast 档阈值 ×0.6：同样的 intimacy 更快升档（捷径档仍要 trust≥50）
    assert state_mod.derive_stage(15, 55, "fast")["stage"] == "亲近"
    assert state_mod.derive_stage(15, 30, "fast")["stage"] == "熟悉", "捷径档信任不够不生效"


# ── 事件组装 ──

def test_turn_events_composition():
    now = T0
    base = state_mod.turn_events(None, has_media=False, user_created_at=[], now=now, local_hour=14.0)
    assert base == ["chat"]

    with_media = state_mod.turn_events("praise", has_media=True, user_created_at=[], now=now, local_hour=14.0)
    assert with_media == ["chat", "praise", "media"]

    # burst：60s 内 ≥3 条（history 含当前这条）
    stamps = [state_mod._fmt_ts(now - timedelta(seconds=d)) for d in (5, 20, 40)]
    burst = state_mod.turn_events(None, has_media=False, user_created_at=stamps, now=now, local_hour=14.0)
    assert "burst" in burst
    stale = state_mod.turn_events(
        None, has_media=False,
        user_created_at=[state_mod._fmt_ts(now - timedelta(seconds=d)) for d in (90, 120, 150)],
        now=now, local_hour=14.0,
    )
    assert "burst" not in stale

    late = state_mod.turn_events(None, has_media=False, user_created_at=[], now=now, local_hour=3.0)
    assert "late_night" in late
    day = state_mod.turn_events(None, has_media=False, user_created_at=[], now=now, local_hour=5.6)
    assert "late_night" not in day


def test_chat_gain_scales_with_speed():
    """普通聊天的 intimacy 增量按升温档缩放：自来熟 > 慢热。"""
    fast = _fresh()
    slow = _fresh()
    fast, _ = state_mod.apply_events(fast, ["chat"], T0, "fast")
    slow, _ = state_mod.apply_events(slow, ["chat"], T0, "slow")
    assert fast["intimacy"] - 5.0 > slow["intimacy"] - 5.0


# ── 落库回路 ──

def test_update_for_turn_persists_and_decays(core_db):
    persona = db.create_bot_persona(name="小暖")
    pid = persona["id"]
    state = state_mod.update_for_turn(pid, "normal", ["praise"], now=T0)
    assert state["valence"] > 0
    row = db.get_bot_relation(pid)
    assert row["valence"] == pytest.approx(state["valence"])
    assert row["state_at"] == state_mod._fmt_ts(T0)
    assert row["last_event_kind"] == "praise"
    # score / turns 不被状态层碰（record_turn 的领地）
    assert row["score"] == 0.0 and row["turns"] == 0

    # 7 天后再来一轮：旧值先衰减再施加新事件
    later = T0 + timedelta(days=7)
    state2 = state_mod.update_for_turn(pid, "normal", ["chat"], now=later)
    expected_decayed = state["valence"] * 0.5 ** (7 * 24 / 5)  # valence 半衰期 5h
    assert state2["valence"] == pytest.approx(expected_decayed, rel=0.05)


# ── 确定性后处理 ──

def test_deterministic_postprocessing():
    assert state_mod.strip_exclaim("好耶！！真的吗！") == "好耶。真的吗。"
    assert state_mod.bubble_cap({"patience": 0.2}) == 2
    assert state_mod.bubble_cap({"patience": 0.8}) == 3
    assert state_mod.downgrade_style("long", {"patience": 0.2}) == "moderate"
    assert state_mod.downgrade_style("long", {"patience": 0.8}) == "long"
    assert state_mod.probe_line("c1") == state_mod.probe_line("c1")
    assert state_mod.probe_line("c1") != state_mod.probe_line("c2") or len({state_mod.probe_line(f"c{i}") for i in range(20)}) > 1


# ── ⑥ 主链路回归：chat_stream 仍恰好调用 1 次 ──

_MODEL = {"base_url": "u", "api_key": "k", "model_id": "m", "max_tokens": 8192,
          "max_output_tokens": 4096, "reasoning_tokens": 1024,
          "temperature": 0.7, "top_p": 0.9}


@pytest.mark.asyncio
async def test_stream_reply_calls_llm_once_and_injects_state(core_db, monkeypatch):
    persona = db.create_bot_persona(name="小暖")
    conv = db.create_bot_conversation(persona["id"])
    calls: list[dict] = []

    async def fake_chat_stream(**kwargs):
        calls.append(kwargs)
        yield type("C", (), {"content": "谢谢夸奖，开心。", "reasoning": None})()

    monkeypatch.setattr("llm_adapter.chat_stream", fake_chat_stream)
    monkeypatch.setattr(BotChatService, "_model_for_persona", lambda self, p: dict(_MODEL))
    monkeypatch.setattr(cs_mod.memory_mod, "recall", _recall_none)
    monkeypatch.setattr(cs_mod.memory_mod, "remember", lambda *a, **k: 0)

    svc = BotChatService()
    out = [kind async for kind, _ in svc.stream_reply(conv["id"], "你真棒")]
    assert "content" in out
    assert len(calls) == 1, "状态层不得引入额外模型调用"
    system = calls[0]["messages"][0]["content"]
    assert "【此刻状态】" in system, "被夸之后状态段必须注入"
    assert "心情" in system


@pytest.mark.asyncio
async def test_stream_reply_without_persona_still_works(core_db, monkeypatch):
    """persona 被删的会话（persona_id 为空）：状态层静默跳过，回复照常。"""
    calls: list[dict] = []

    async def fake_chat_stream(**kwargs):
        calls.append(kwargs)
        yield type("C", (), {"content": "嗯。", "reasoning": None})()

    monkeypatch.setattr("llm_adapter.chat_stream", fake_chat_stream)
    monkeypatch.setattr(BotChatService, "_model_for_persona", lambda self, p: dict(_MODEL))
    monkeypatch.setattr(
        cs_mod.db, "get_bot_conversation",
        lambda cid: {"persona": {"name": "n", "reply_style": "moderate",
                                 "enable_thinking": 0, "personality": "", "background": ""}},
    )
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", lambda cid, n: [])
    monkeypatch.setattr(cs_mod.db, "add_bot_message", lambda *a, **k: {})
    monkeypatch.setattr(cs_mod.memory_mod, "recall", _recall_none)
    monkeypatch.setattr(cs_mod.memory_mod, "remember", lambda *a, **k: 0)

    svc = BotChatService()
    async for _ in svc.stream_reply("c1", "hi"):
        pass
    assert len(calls) == 1
    assert "【此刻状态】" not in calls[0]["messages"][0]["content"]
