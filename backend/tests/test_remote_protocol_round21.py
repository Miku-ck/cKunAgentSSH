"""哨兵协议单一实现源的回归钉（第 21 轮冗余清理）。

此前 `[exit_code=` 被 host_profile / ops_job / ops_obs / reins.flow 各自手抄，
并抄出三种写法（带 `=`、少个 `=`、正则）。这里钉两件事：
  1) 四种判定的边界语义（前缀 vs 行内片段 vs 数字完整）不互相吞并；
  2) 生产者拼装 ↔ strip_remote_tail / drop_sentinel_lines 无损往返。
"""
from agent.remote_protocol import (
    EXIT_CODE_PREFIX,
    STDERR_SENTINEL,
    has_exit_sentinel,
    has_sentinel_fragment,
    is_exit_code_line,
    is_sentinel_line,
)
from agent.tools.builtin.ops_common import drop_sentinel_lines, strip_remote_tail


def test_wire_format_literals_unchanged():
    """线上字节是跨模块契约（远端脚本/历史测试都认它），改字面量即破坏契约。"""
    assert EXIT_CODE_PREFIX == "[exit_code="
    assert STDERR_SENTINEL == "[stderr]"


def test_is_exit_code_line_requires_equals_sign():
    """少一个 `=` 的宽松写法会把以 "[exit_code" 开头的真实数据行一起删掉，
    对账时 rcs[i] 与 dead_dirs 的下标就会错位。"""
    assert is_exit_code_line("[exit_code=0]")
    assert not is_exit_code_line("[exit_code0]")
    assert not is_exit_code_line("12345")


def test_is_sentinel_line_covers_both_tails_only_as_whole_line():
    assert is_sentinel_line("[stderr]")
    assert is_sentinel_line("[exit_code=-9]")
    # 数据行里出现同名字样不算哨兵行
    assert not is_sentinel_line("[stderr] something else")


def test_fragment_predicate_is_looser_than_line_predicate():
    """截断会把末行哨兵粘到数据行尾部，那时只有片段判定能认出它。"""
    glued = "some/data/line" + EXIT_CODE_PREFIX + "0]"
    assert has_sentinel_fragment(glued)
    assert not is_sentinel_line(glued)


def test_has_exit_sentinel_needs_a_complete_number():
    assert has_exit_sentinel("out\n[exit_code=1]")
    assert has_exit_sentinel("[exit_code=-1]")
    assert not has_exit_sentinel("no sentinel here")
    assert not has_exit_sentinel(EXIT_CODE_PREFIX)  # 被截断、无数字


def test_producer_consumer_roundtrip_is_lossless():
    """按 exec_remote 的拼装规则造输出，再走两个消费方。

    两者语义不同、都必须保住：strip_remote_tail 只从**尾部**连续剥哨兵行
    （[stderr] 之后的正文不是哨兵，留下）；drop_sentinel_lines 全文过滤标记行。
    """
    full = f"line1\nline2\n{STDERR_SENTINEL}\nboom\n{EXIT_CODE_PREFIX}1]"
    assert strip_remote_tail(full) == f"line1\nline2\n{STDERR_SENTINEL}\nboom"
    assert drop_sentinel_lines(full) == ["line1", "line2", "boom"]
