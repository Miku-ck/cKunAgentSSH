"""出站投递节奏：把人「一条一条打字」的时间还原出来。

QQ 被动回复没有「正在输入」这个状态可发，所以拆成几条时，段与段之间就是纯冷场。
同一段文本在本地看着像在打字（前端 bubbleDeliveryPlan 逐条揭开），在 QQ 上却是
几毫秒内一起弹出来 —— 这就是「像广播，不像人」的来源。这里只补时间，不改条数：
一旦在这里改条数，落库记录与平台实际收到的消息就会对不上。

数字取自打字速度：常速约每两秒十个汉字，加上「想一下再发」的启动时间。
"""

import random
from typing import Sequence

MIN_GAP_S = 0.8
MAX_GAP_S = 3.5
SEC_PER_CHAR = 0.045
JITTER = (0.85, 1.25)
# 一轮回复的总等待上限：超过这个数就不是「在打字」，是「卡住了」。
BUDGET_S = 6.0


def typing_gap_s(text: str, rng: random.Random | None = None) -> float:
    """发完上一段之后、开始发这一段之前的等待。"""
    rng = rng or random
    raw = MIN_GAP_S + SEC_PER_CHAR * len(text or "")
    jittered = raw * rng.uniform(*JITTER)
    return round(max(MIN_GAP_S, min(MAX_GAP_S, jittered)), 2)


def outbound_gaps(parts: Sequence[str], rng: random.Random | None = None) -> list[float]:
    """每一段之前的等待；首段为 0 —— 他在等答案，第一段不该再拖。

    总等待超预算时按比例压缩而不是并条：并条会改变「平台收到几条」，
    与本地记录不一致。
    """
    rng = rng or random
    if not parts:
        return []
    gaps = [0.0] + [typing_gap_s(part, rng) for part in parts[1:]]
    total = sum(gaps)
    if total > BUDGET_S:
        scale = BUDGET_S / total
        gaps = [round(gap * scale, 2) for gap in gaps]
    return gaps
