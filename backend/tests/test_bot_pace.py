"""出站投递节奏：QQ 没有「正在输入」，段间冷场就是观感的全部。"""

import random

import pytest

from bot.pace import (
    BUDGET_S,
    MAX_GAP_S,
    MIN_GAP_S,
    outbound_gaps,
    typing_gap_s,
)


def test_first_part_has_no_delay():
    """他在等答案：第一段立即发，节奏只发生在「已经说完一句」之后。"""
    assert outbound_gaps(["在的", "你吃了吗"])[0] == 0.0


def test_gap_grows_with_length_but_stays_within_one_breath():
    rng = random.Random(7)
    short = typing_gap_s("在的", rng)
    long = typing_gap_s("今晚八点开一把，来不来都得说一声，别又到点才说不打了", rng)
    assert short >= MIN_GAP_S
    assert long > short
    assert long <= MAX_GAP_S


def test_gaps_are_jittered_not_constant():
    """同一个长度也不该每次都停同一秒：人有快有慢。"""
    values = {typing_gap_s("在的", random.Random(seed)) for seed in range(8)}
    assert len(values) > 1


def test_total_wait_is_capped_so_it_never_reads_as_stuck():
    """三段都长时不能停到 9 秒 —— 那是卡住，不是停顿。"""
    parts = ["今晚八点开一把，来不来都得说一声，别又到点才说不打了"] * 3
    gaps = outbound_gaps(parts, random.Random(3))
    assert sum(gaps) <= BUDGET_S
    assert gaps[0] == 0.0


def test_gaps_are_reproducible_for_a_given_rng():
    parts = ["在的", "你吃了吗", "我在楼下"]
    assert outbound_gaps(parts, random.Random(11)) == outbound_gaps(parts, random.Random(11))


def test_single_part_needs_no_wait():
    assert outbound_gaps(["就一句"]) == [0.0]
    assert outbound_gaps([]) == []


@pytest.mark.asyncio
async def test_delivery_waits_between_parts_but_not_before_the_first(monkeypatch):
    """管线真的按排程等：首段立刻发，后面的每一段先等一段「在打字」。"""
    from bot import inbound_pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.db, "mark_bot_message_answered", lambda *a, **k: None)

    sleeps: list[float] = []
    sent: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    class Provider:
        name = "qq"

        def outbound_parts(self, text: str) -> list[str]:
            return ["在的", "你吃了吗", "我在楼下"]

    class Inbound:
        event_id = "e1"
        send_plain = None

        async def send(self, part: str) -> None:
            sent.append(part)

    pipeline = pipeline_mod.InboundPipeline(None, sleep=fake_sleep)
    await pipeline._deliver(Inbound(), Provider(), {"id": "c1"}, "随便")

    assert sent == ["在的", "你吃了吗", "我在楼下"]
    assert len(sleeps) == 2
    assert all(gap > 0 for gap in sleeps)

