# -*- coding: utf-8 -*-
"""S14-04 / S14-08：本地 grep 不得把「没读到」说成「确认没有」，也不得悄悄截断。

原形状：逐个文件的 `except (IsADirectoryError, PermissionError, OSError): continue` 没有
任何记账，末尾 `if not results: return ToolResult(True, "未匹配到内容：…")`。于是目录里只要
全部文件都读不了，回给模型的是「没有匹配」—— 一次权限问题被翻译成一条否定结论。
而**远程那条腿**会把 Permission denied 带回来（grep 退出码 2 与 1 是两件事，ops_obs 那族
测试正钉着这条），同一个问题两条腿给相反答案，属"同族防护漏一处"。

另一半（S14-08）：`max_lines` 命中上限时直接 break，输出里没有任何痕迹，
「正好 200 条」与「被截到 200 条」读起来一模一样，模型会当全量结论用。
"""
import asyncio
import builtins

import pytest

from agent.tools.builtin.ops_file import FileGrepTool, _grep_coverage_note


class _Ctx:
    ssh_session_id = ""  # 本地分支


def _grep(path, pattern, max_lines=200):
    return asyncio.run(FileGrepTool().execute(
        _Ctx(), path=str(path), pattern=pattern, max_lines=max_lines
    ))


def _make(dirpath, name, text):
    p = dirpath / name
    p.write_text(text, encoding="utf-8")
    return p


def _block_open(monkeypatch, blocked_path):
    """把某一个文件的 open 换成 PermissionError（Windows 上 chmod 不拦读，只能这么造）。"""
    real_open = builtins.open

    def fake_open(file, *a, **kw):
        if str(file) == str(blocked_path):
            raise PermissionError(13, "Permission denied")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)


def test_all_unreadable_is_not_reported_as_no_match(tmp_path, monkeypatch):
    a = _make(tmp_path, "a.log", "alpha\n")
    b = _make(tmp_path, "b.log", "beta\n")
    _block_open(monkeypatch, a)
    _block_open(monkeypatch, b)

    r = _grep(tmp_path, "alpha")

    assert r.success is False, "全部读不了却回「未匹配到内容」= 把权限问题说成否定结论"
    assert "未匹配到内容" not in (r.error or "")
    assert "2 个文件读不了" in r.error
    assert "Permission denied" in r.error, "现场线索必须带出去"


def test_partial_unreadable_still_answers_but_says_so(tmp_path, monkeypatch):
    blocked = _make(tmp_path, "secret.log", "alpha hidden\n")
    _make(tmp_path, "app.log", "alpha ok\n")
    _block_open(monkeypatch, blocked)

    r = _grep(tmp_path, "alpha")

    assert r.success is True
    assert "app.log:1:alpha ok" in r.output
    assert "1 个文件读不了" in r.output, "命中了也要说明覆盖不全"


def test_cap_is_visible_in_the_output(tmp_path):
    for i in range(5):
        _make(tmp_path, f"f{i}.log", "hit\n")

    r = _grep(tmp_path, "hit", max_lines=2)

    assert r.success is True
    assert "已达 max_lines=2 上限" in r.output
    assert "可能仍有匹配未列出" in r.output


def test_complete_coverage_carries_no_note(tmp_path):
    _make(tmp_path, "app.log", "alpha one\n")

    r = _grep(tmp_path, "alpha")

    assert r.output == "app.log:1:alpha one", "全量命中还缀一句废话，会把注脚稀释成噪声"


@pytest.mark.parametrize("capped,unreadable,expect", [
    (False, [], ""),
    (True, [], "\n\n（已达 max_lines=200 上限，可能仍有匹配未列出）"),
])
def test_coverage_note_shape(capped, unreadable, expect):
    assert _grep_coverage_note(capped=capped, max_lines=200, unreadable=unreadable) == expect


def test_coverage_note_truncates_the_example_list():
    note = _grep_coverage_note(capped=False, max_lines=200,
                               unreadable=[f"/p/{i}: denied" for i in range(5)])
    assert "5 个文件读不了" in note
    assert "另有 2 个未列出" in note
    assert "/p/3" not in note
