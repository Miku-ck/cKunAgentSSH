"""记忆预算的地毯式不变量：账户分出去的量不能超过窗口本身。

`MemoryBudget.for_context` 的四本账 + 留量必须 ≤ window，否则「按窗口分配」这句话是空的：
注入侧照额度塞、请求超限、再回 INVALID_ARGS 触发一次压缩重试。原实现把两个下限写成绝对
常量（reserve 至少 1024、每本账至少 200），窗口小于 1824 时无论怎么算都超发。实测（修复前）：

    window=1024 → reserve 1024 + 200×4 = 1824（超 800）
    window=1536 → 同样 1824（超 288）
    window=2048 → 2048（刚好）

`max_tokens` 是 `Field(16384, ge=1)`（backend/routes/settings.py），小窗口是合法输入，
不是假想值。修法是把地板随窗口缩放，并让 history 不再设下限（它是吸收余项的那一本）。

反向锁同样重要：**≥2048 的窗口一个 token 都不许动**。上面那行探测同时给出了 2048/4096/16384
的修复前额度，本文件把它们逐字钉住 —— 收紧小窗口不该顺手改变生产环境（16384）的分配。

`cap_text_tokens` 那条是单位错：拿 `len(marker)`（11 个字符）去比 token 预算，而标记实际只值
6 token。`"经"*200` + cap=24 正好落在两种口径的分歧带上，旧写法因为多算 5 而把折叠标记整个丢掉，读者看到的是"文本
凭空少了一截"，没有「已折叠」的提示。

运行: python -m pytest backend/tests/test_memory_budget_invariant.py -v
"""
import pytest

from agent.cost_tracker import estimate_tokens
from agent.memory.budget import MemoryBudget, cap_text_tokens

_MARKER = "[早期上下文已折叠]\n"


def _total(b: MemoryBudget) -> int:
    return b.blocks + b.injection + b.summary + b.reserve


@pytest.mark.parametrize("window", [1, 8, 64, 128, 256, 512, 1024, 1536, 2048,
                                    4096, 8192, 16384, 32768, 128000])
def test_三本账加留量不得超过窗口(window):
    b = MemoryBudget.for_context(window)
    assert _total(b) <= window, (
        f"window={window} 分出 {b.blocks}+{b.injection}+{b.summary}+{b.reserve}"
        f"={_total(b)}，超出 {b.window} —— 按窗口分配是空话")
    assert b.reserve <= window
    assert min(b.blocks, b.injection, b.summary) >= 0
    assert not hasattr(b, "history"), (
        "history 账户已删：全仓无人读它，留着只会被当成『调它能改 history 上限』的旋钮")


@pytest.mark.parametrize("window,expect", [
    # 修复前实测值（HEAD 副本直接跑出来的），逐字钉住：大窗口的分配口径不许被小窗口的修法带偏
    (2048, (1024, 200, 204, 256)),
    (4096, (1024, 460, 614, 768)),
    (16384, (1966, 2162, 2883, 3604)),
    (32768, (3932, 4325, 5767, 7209)),
    (128000, (15360, 16896, 22528, 28160)),
])
def test_大窗口额度与修复前逐字一致(window, expect):
    b = MemoryBudget.for_context(window)
    got = (b.reserve, b.blocks, b.injection, b.summary)
    assert got == expect, f"window={window} 的分配从 {expect} 变成 {got}"


def test_未给窗口退回默认窗口而不是零():
    """`int(window or 0)` 之后 win<=0 走 _DEFAULT_WINDOW：0/None 都不能算出零额度。"""
    for bad in (0, None, -1):
        b = MemoryBudget.for_context(bad)
        assert b.window == 16384
        assert b.injection > 0


@pytest.mark.parametrize("wanted,expected", [
    (1500, 1500),      # 窗口给得起 ⇒ 原样尊重
    (99999, 2883),     # 超窗 ⇒ 截到比例上限
    (0, 2883),         # 0 视为未给值（沿用既有口径：走上限而不是走 0）
    (None, 2883),
])
def test_wanted_值按窗口取小(wanted, expected):
    b = MemoryBudget.for_context(16384, injection_wanted=wanted)
    assert b.injection == expected


def test_折叠标记按_token_计价而不是字符数():
    """标记实际值 6 token，旧写法拿 len(marker)=11 个字符参与比较，多算 5。

    样本必须落在两种口径的分歧带上才测得出差别：`"经"*200` + cap=24 时正文占 22 token、
    容差 28 ⇒ 旧口径 11+22=33 > 28 判「放不下」把标记整个丢掉（HEAD 实测 kept=False），
    读者看到的是文本凭空少一截、没有「已折叠」提示；新口径 6+22=28 ≤ 28 保留。
    """
    text = "经" * 200
    out = cap_text_tokens(text, 24)
    body = out[len(_MARKER):] if out.startswith(_MARKER) else out
    assert len(_MARKER) + estimate_tokens(body) > int(24 * 1.2), (
        "样本不在分歧带上，这条测试无法失败")
    assert out.startswith(_MARKER), f"标记按字符数误判为放不下：{out[:16]!r}"
    assert estimate_tokens(out) <= int(24 * 1.2)


def test_结果保留尾部且不越过容差():
    """折叠永远保留尾部（近期内容更有价值），且总代价不超过文档承诺的 1.2×cap。"""
    text = "记" * 400
    out = cap_text_tokens(text, 50)
    assert out.endswith(text[-1:])
    assert estimate_tokens(out) <= int(50 * 1.2)


def test_未超限原样返回():
    assert cap_text_tokens("短文本", 200) == "短文本"
    assert cap_text_tokens("任意", 0) == "任意"
