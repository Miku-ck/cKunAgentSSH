"""统一上下文预算（spec §8）—— 按模型窗口一次性分配各记忆账户。

改造前各处各自拍脑袋：注入预算写在 memory.inject_budget_tokens、画像块写在
memory.blocks_budget_tokens（两者都不看窗口大小），摘要上限用 `窗口 × 0.25 × 3 字符`
的字符反推。结果是 8k 窗口的模型也可能被塞进 8000 token 注入，而摘要上限在中文下
比 intended 松 2~3 倍。这里把三本账加一份留量放在一起算：用户在设置里给的值当作"想要多少"，
窗口当作"能给多少"，取小者。

口径统一在 cost_tracker.estimate_tokens（中英混合启发式）：**所有预算与上限判定**都按 token。
只有 `cap_text_tokens` 的收紧步长仍按字符推进（那是搜索刻度，不是预算口径）。
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
    reserve: int

    @staticmethod
    def for_context(window: int | None, *, blocks_wanted: int | None = None,
                    injection_wanted: int | None = None) -> "MemoryBudget":
        """按模型上下文窗口分配；wanted 来自记忆设置，超窗则截到比例上限。"""
        win = int(window or 0)
        if win <= 0:
            win = _DEFAULT_WINDOW
        # 地板值必须随窗口缩放。原写法把留量下限（1024）和账户下限（200×4）写成绝对
        # 常量，于是窗口小于 1824 时「三本账 + 留量」必然超过窗口本身：实测 window=1024
        # 分到 1824、window=1536 分到 1824。而 max_tokens 是 `Field(16384, ge=1)`
        # （routes/settings.py），小窗口是合法输入 —— 超发的直接后果是注入照塞、请求
        # 超限、再触发 INVALID_ARGS 压缩重试。
        reserve_floor = min(1024, max(64, win // 2))
        reserve = min(win, max(reserve_floor, int(win * _RESERVE_SHARE)))
        headroom = max(0, win - reserve)
        account_floor = min(_MIN_ACCOUNT, headroom // 3)
        blocks = _clamp(blocks_wanted, int(headroom * _BLOCKS_SHARE), account_floor)
        injection = _clamp(injection_wanted, int(headroom * _INJECTION_SHARE), account_floor)
        summary = max(account_floor, int(headroom * _SUMMARY_SHARE))
        # 剩下那段不再单列「history 账户」：全仓没有任何地方读它（system 与 run 消息的
        # 实际闸门是 bridle 的压缩阈值 = window×0.7，与本对象无关）。留着一个算出来的
        # 字段只会让人以为调它能改变 history 的上限 —— 它谁也不管。
        return MemoryBudget(window=win, blocks=blocks, injection=injection,
                            summary=summary, reserve=reserve)


def _clamp(wanted: int | None, ceiling: int, floor: int = _MIN_ACCOUNT) -> int:
    """用户想要的值与窗口能给的值取小；未给值时用上限本身。"""
    ceiling = max(floor, ceiling)
    if not wanted or int(wanted) <= 0:
        return ceiling
    return max(floor, min(int(wanted), ceiling))


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
    """把文本截到 cap_tokens 附近（放得下折叠标记就带上标记）。

    先按 estimate_tokens 反推出字符上限，再逐步收紧——中英混合文本下一次性反推可能仍偏大，
    循环次数受文本长度约束（每轮至少砍 1/8）。上限判定全部用 token；`keep` 的 0.9 步长只是
    搜索刻度（按字符推进），不是预算口径。留 1.2 的宽容度：宁可结果略超 cap，也不为了塞下
    标记再砍正文。标记的成本按 `estimate_tokens(marker)`（6 token）计 —— 原写法拿
    `len(marker)`（11 个字符）参与 token 预算比较，多算 5，分歧带上的样本会被误判为
    「标记放不下」而把标记整个丢掉（见 tests/test_memory_budget_invariant.py）。
    """
    if cap_tokens <= 0 or estimate_tokens(text) <= cap_tokens:
        return text
    keep = max(16, int(len(text) * 0.9))
    while keep > 16 and estimate_tokens(text[-keep:]) > cap_tokens:
        keep = max(16, keep - max(1, keep // 8))
    trimmed = text[-keep:]
    if estimate_tokens(marker) + estimate_tokens(trimmed) <= int(cap_tokens * 1.2):
        return marker + trimmed
    return trimmed
