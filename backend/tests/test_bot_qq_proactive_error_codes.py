"""QQ 主动消息失败分类的结构化码层 — 码优先 / marker 兜底的回归测试。

背景：botpy 1.2.1 丢弃响应体业务码，码只剩异常文案开头残留的数字。原实现纯靠
中文文案 marker 匹配，平台改措辞分类即失准。现在码命中优先，marker 降级为兜底。

守护点：
- 码命中优先于 marker：文案同时含码与 quota 词 → 按 code 判 permission；
- 精确码与前缀规则都生效（400341 前缀族）；
- 码未知 → 落入 marker 兜底（不因码认不出就粗判 network）；
- 无码纯文案 → marker 三组 + wakeup_limit 全部回归不变；
- 码形态边界：非 4 开头 / 位数不符 / 非开头位置 都不命中。

运行: python -m pytest backend/tests/test_bot_qq_proactive_error_codes.py -v
"""
from bot.qq_channel import (
    _extract_error_code,
    classify_proactive_error,
)


class _Exc(Exception):
    pass


# ── 码提取形态 ────────────────────────────────────────────────────────────────

def test_extract_code_from_leading_digits():
    assert _extract_error_code("40034105 invalid group message") == "40034105"
    assert _extract_error_code("  400341007: something") == "400341007"
    assert _extract_error_code("4003410 (8 digits)") is None  # 7 位 4 开头？8 位才匹配


def test_extract_code_format_boundaries():
    # 非 4 开头不命中
    assert _extract_error_code("20034105 xxx") is None
    assert _extract_error_code("50034105 xxx") is None
    # 位数不符（总长 <8 或 >9）不命中
    assert _extract_error_code("4003410 xxx") is None          # 7 位
    assert _extract_error_code("40034105555 xxx") is None      # 11 位
    # 码不在开头不命中（防把输出里的普通数字当码）
    assert _extract_error_code("错误码 40034105") is None
    assert _extract_error_code("") is None


# ── 分类：码优先 ──────────────────────────────────────────────────────────────

def test_exact_code_wins_over_text_markers():
    """文案同时含码与 quota 词 → 码优先判 permission（marker 只是兜底）。"""
    cat, _text = classify_proactive_error(_Exc("40034105 频控达到上限"))
    assert cat == "permission"


def test_prefix_code_wins():
    cat, _text = classify_proactive_error(_Exc("40034102 无权限发送"))
    assert cat == "permission"


def test_unknown_code_falls_back_to_markers():
    """码认不出（未知业务码）→ 落入 marker 兜底，而不是粗判 network。"""
    assert classify_proactive_error(_Exc("49999999 频控了"))[0] == "quota"
    assert classify_proactive_error(_Exc("49999999 内容违规"))[0] == "content"
    assert classify_proactive_error(_Exc("49999999 无权限发送"))[0] == "permission"


def test_known_code_beats_contradicting_markers():
    """码判 permission，即使文案同时出现 quota marker（码是权威）。"""
    assert classify_proactive_error(_Exc("40034105 超过频率上限"))[0] == "permission"


# ── 分类：无码路径全部回归 ────────────────────────────────────────────────────

def test_wakeup_limit_still_matched_by_text():
    assert classify_proactive_error(_Exc("召回消息已达上限"))[0] == "wakeup_limit"


def test_permission_markers_regression():
    for text in ("无权限发送主动消息", "非群成员", "对方拒收了消息"):
        assert classify_proactive_error(_Exc(text))[0] == "permission"
    # 异常类型名判定保留
    class ForbiddenError(Exception):
        pass
    assert classify_proactive_error(ForbiddenError("some english text"))[0] == "permission"


def test_quota_and_content_markers_regression():
    assert classify_proactive_error(_Exc("触发频控，请稍后再试"))[0] == "quota"
    assert classify_proactive_error(_Exc("消息内容无效"))[0] == "content"


def test_no_marker_defaults_to_network():
    assert classify_proactive_error(_Exc("read timed out"))[0] == "network"
    assert classify_proactive_error(_Exc(""))[0] == "network"
