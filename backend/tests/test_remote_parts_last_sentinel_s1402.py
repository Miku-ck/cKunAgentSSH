# -*- coding: utf-8 -*-
"""S14-02：`remote_parts` 必须按生产侧的拼装顺序拆，判据取**最后**一次出现。

`ops_common.exec_remote` 的形状是 `正文 → [stderr] 段 → [exit_code=N]`，而本模块的
docstring 自己就写着「拿正文里的字样当判据会把日志本身当成故障」。此前实现用的是
`_EXIT_SENTINEL_RE.search()`（第一处）与 `STDERR_SENTINEL in head`（子串），于是：

- 被读的文件里有一行 `[exit_code=0]`（CI 日志、agent 自己的输出文件都会）⇒ rc 是从
  **文件内容**里读出来的，真退出码被忽略；
- `raw[:m.start()]` 把那个字样之后的整截正文丢掉，少读一截还报成功；
- 有真 stderr 段时 `split(...,1)` 取的是**正文里**那一处 ⇒ 真错误丢了，
  上层拿着文件内容当远端报错转述给模型。

本仓另一处早就用 `rfind` 处理同一个哨兵（`ops_job.py` 剥 `[exit_code=..]` 那段），
这条是把口径收回来。

⚠️ 已知残余、本轮没解决：正文里**独占一行**的 `[stderr]` 而这次调用根本没有 stderr 段时，
仍然分不出「日志里的一句话」与「远端真报了错」—— 那需要每次调用带唯一 nonce 或换更不可能
撞见的标记，属于跨层协议变更，已登记待裁决（见 90-ADJUDICATION-SHEET 的 S14-02 后半）。
"""
from agent.remote_protocol import (
    EXIT_CODE_PREFIX,
    STDERR_SENTINEL,
    remote_parts,
)


def assemble(output: str, error: str = "", exit_code=None) -> str:
    """逐字照 ops_common.exec_remote 的追加顺序拼（:137-144）。

    这条是本片全部用例的地基：解析器只要和生产侧对不上，第一个撞车的正文就把结论换掉。
    """
    full = output
    if error:
        full += f"\n{STDERR_SENTINEL}\n{error}"
    if exit_code is not None:
        full += f"\n{EXIT_CODE_PREFIX}{exit_code}]"
    return full


def test_rc_comes_from_the_appended_marker_not_from_file_content():
    body = "job.log 第 12 行：echo '[exit_code=0]' 用于自检\n真正的内容在后面"
    raw = assemble(body, exit_code=3)

    parsed_body, err, rc = remote_parts(raw)

    assert rc == 3, "正文里的 [exit_code=0] 抢走了退出码"
    assert "真正的内容在后面" in parsed_body, "正文在内容哨兵处被截断"
    assert err == ""


def test_content_marker_is_kept_in_the_body():
    body = "A\n[exit_code=0]\nB"
    parsed_body, _err, rc = remote_parts(assemble(body, exit_code=1))
    assert rc == 1
    assert parsed_body == body, "少读的那一截是审计证据本身"


def test_real_stderr_wins_over_content_occurrences():
    body = "some CI output\n[stderr]\nthis line is inside the log\nmore log"
    raw = assemble(body, error="Permission denied", exit_code=1)

    parsed_body, err, rc = remote_parts(raw)

    assert rc == 1
    assert err == "Permission denied", f"取到的不是最后一段：{err!r}"
    assert "more log" in parsed_body


def test_round_trip_for_every_shape_the_producer_can_emit():
    cases = [
        ("plain text", "", 0),
        ("plain text", "boom", 1),
        ("", "", None),          # 通道没到远端
        ("", "only stderr", 1),  # stdout 空 ⇒ 标记紧跟串首
        ("[exit_code=9]\nx", "real error", 0),  # 正文里有退出码字样，真段在最后
    ]
    for output, error, code in cases:
        body, err, rc = remote_parts(assemble(output, error, code))
        assert rc == code, (output, error, code, body, err, rc)
        assert body == output.strip()
        assert err == " ".join(error.split())


def test_stderr_section_survives_a_strip_that_ate_the_leading_newline():
    """stdout 为空时生产侧写的是 `\\n[stderr]\\n…`，桥接/截断任何一处 strip 都会削掉那个前导换行。

    认漏这种形状 = 「tail 读不到文件」的真空错误段被当无事发生，上层据此报成功 ——
    这条不是洁癖，`test_ops_obs_channel_failure_not_fabricated` 里那条失败用例就是这么翻的。
    """
    raw = f"{STDERR_SENTINEL}\ntail: cannot open '/var/log/x.log'\n{EXIT_CODE_PREFIX}1]"
    body, err, rc = remote_parts(raw)
    assert rc == 1
    assert err == "tail: cannot open '/var/log/x.log'"
    assert body == ""


def test_known_residual_a_lone_stderr_line_in_content_is_still_ambiguous():
    """本轮**没有**解决的那一半，钉在这里当触发器。

    正文里独占一行的 `[stderr]` 而这次调用根本没有 stderr 段时，字符串里没有任何东西能
    区分「日志里的一句话」和「远端真报了错」—— 那需要每次调用带唯一 nonce（或换更不可能
    撞见的标记），属于跨层协议变更，已登记待裁决（S14-02 后半）。
    协议真改了之后这条会红，提醒删掉它而不是改写它。
    """
    ambiguous = assemble("[exit_code=9]\n[stderr]\nx", exit_code=0)
    body, err, rc = remote_parts(ambiguous)
    assert rc == 0                       # 退出码这一半已经对了
    assert (body, err) == ("[exit_code=9]", "x")  # ← 语义上应当是 body 全文、err 空


def test_missing_exit_marker_reads_as_no_rc_rather_than_a_fabricated_one():
    # rc=None 的含义是「根本没到远端」，上层据此拒绝把"没有结果"编成观测结果。
    body, err, rc = remote_parts("just output, no sentinel at all")
    assert rc is None
    assert body == "just output, no sentinel at all"
    assert err == ""


def test_producer_still_appends_in_the_order_this_parser_assumes():
    """漂移守卫：`rfind` / 「独占一行」这两条判据完全依赖生产侧的形状。

    装配顺序改了不会有任何测试自然变红 —— 除了这条。用 inspect 取源码而不是重抄一份
    字面量，否则这里也只是在断言自己的臆想。
    """
    import inspect

    from agent.tools.builtin import ops_common

    src = inspect.getsource(ops_common.exec_remote)
    assert 'full += f"\\n{STDERR_SENTINEL}\\n{error}"' in src
    assert 'full += f"\\n{EXIT_CODE_PREFIX}{exit_code}]"' in src
    # 顺序也必须是 stderr 在前、退出码在最后：取"最后一处"才等于取"追加的那一处"
    assert src.index("STDERR_SENTINEL") < src.index("EXIT_CODE_PREFIX")
