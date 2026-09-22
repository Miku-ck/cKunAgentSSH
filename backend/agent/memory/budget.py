"""统一上下文预算（spec §8）—— 按模型窗口一次性分配各记忆账户。

改造前各处各自拍脑袋：注入预算写在 memory.inject_budget_tokens、画像块写在
memory.blocks_budget_tokens（两者都不看窗口大小），摘要上限用 `窗口 × 0.25 × 3 字符`
的字符反推。结果是 8k 窗口的模型也可能被塞进 8000 token 注入，而摘要上限在中文下
比 intended 松 2~3 倍。这里把四本账放在一起算：用户在设置里给的值当作"想要多少"，
窗口当作"能给多少"，取小者。

口径统一在 cost_tracker.estimate_tokens（中英混合启发式），不再有任何字符估算。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..cost_tracker import estimate_tokens

# 各账户占"可分配空间"的比例上限：画像块常驻每轮，占比最小；注入按相关性可有可无；
# 摘要是溢出压缩的产物，允许更大但必须有界（否则一条摘要就能再撑爆窗口）。
_BLOCKS_SHARE = 0.15
_INJECTION_SHARE = 0.20
_SUMMARY_SHARE = 0.25
# 留给输出 + vLLM 内部开销（tool schema / 模板 token）的余量
_RESERVE_SHARE = 0.12
_MIN_ACCOUNT = 200
_DEFAULT_WINDOW = 16384


@dataclass(frozen=True)
class MemoryBudget:
    window: int
    blocks: int
    injection: int
    summary: int
    history: int
    reserve: int

    @staticmethod
    def for_context(window: int | None, *, blocks_wanted: int | None = None,
                    injection_wanted: int | None = None) -> "MemoryBudget":
        """按模型上下文窗口分配；wanted 来自记忆设置，超窗则截到比例上限。"""
        win = int(window or 0)
        if win <= 0:
            win = _DEFAULT_WINDOW
        reserve = max(1024, int(win * _RESERVE_SHARE))
        headroom = max(win - reserve, _MIN_ACCOUNT * 3)
        blocks = _clamp(blocks_wanted, int(headroom * _BLOCKS_SHARE))
        injection = _clamp(injection_wanted, int(headroom * _INJECTION_SHARE))
        summary = max(_MIN_ACCOUNT, int(headroom * _SUMMARY_SHARE))
        # 剩下的都归 history（system 与 run 消息实际也花这本账，压缩阈值由 bridle 管）
        history = max(_MIN_ACCOUNT, headroom - blocks - injection - summary)
        return MemoryBudget(window=win, blocks=blocks, injection=injection,
                            summary=summary, history=history, reserve=reserve)


def _clamp(wanted: int | None, ceiling: int) -> int:
    """用户想要的值与窗口能给的值取小；未给值时用上限本身。"""
    ceiling = max(_MIN_ACCOUNT, ceiling)
    if not wanted or int(wanted) <= 0:
        return ceiling
    return max(_MIN_ACCOUNT, min(int(wanted), ceiling))


def current(window: int | None) -> "MemoryBudget":
    """读记忆设置里的两个账户值，按给定窗口算预算。

    窗口由调用方给出（routes/agent 手上的 model_cfg["max_tokens"]）；读设置失败不致命，
    退到各账户上限。
    """
    try:
        import database as db
        settings = db.get_memory_settings()
    except Exception:
        settings = {}
    return MemoryBudget.for_context(
        window,
        blocks_wanted=settings.get("blocks_budget_tokens"),
        injection_wanted=settings.get("inject_budget_tokens"),
    )


def cap_text_tokens(text: str, cap_tokens: int, *, marker: str = "[早期上下文已折叠]\n") -> str:
    """把文本截到 cap_tokens 以内，保留尾部（近期内容更有价值）。

    先按 estimate_tokens 反推出字符上限，再逐步收紧——中英混合文本下一次性反推可能仍偏大，
    循环次数受文本长度约束（每轮至少砍 1/8）。
    """
    if cap_tokens <= 0 or estimate_tokens(text) <= cap_tokens:
        return text
    keep = max(16, int(len(text) * 0.9))
    while keep > 16 and estimate_tokens(text[-keep:]) > cap_tokens:
        keep = max(16, keep - max(1, keep // 8))
    trimmed = text[-keep:]
    return marker + trimmed if len(marker) + estimate_tokens(trimmed) <= cap_tokens * 1.2 else trimmed
