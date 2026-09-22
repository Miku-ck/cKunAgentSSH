"""web_search 降级链回归 —— HTML 型搜索源「刮不到结果」不能被当成「搜不到结果」。

360 / DuckDuckGo 两级都靠正则刮页面，页面改版或反爬拦截时解析结果为空。
原实现在这一分支返回 success + "无搜索结果"，execute 的 `if result.success`
据此直接返回，下一级降级源永远不会再试：360 改版等于 web_search 整体失效。
"""
import asyncio

import pytest

import agent.tools.builtin.web_tools as wt
import netconfig
from agent.tools import ToolResult, ToolContext


def _res(ok: bool, out: str = "", err: str = "") -> ToolResult:
    return ToolResult(ok, out, err)


class _FakeResp:
    def __init__(self, html: str):
        self.text = html

    def raise_for_status(self) -> None:
        return None


def _install_fake_httpx(monkeypatch, html: str) -> None:
    """HTML 源走 netconfig.new_client（统一代理+超时出口），替换它即可离网"""
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, _url, headers=None):
            return _FakeResp(html)

    monkeypatch.setattr(netconfig, "new_client", lambda **_kw: _FakeClient())


@pytest.mark.parametrize("method", ["_search_360", "_search_duckduckgo"])
def test_empty_parse_returns_failure(monkeypatch, method):
    """刮不到条目 = 该源失败（success=False），而不是成功的「无搜索结果」"""
    _install_fake_httpx(monkeypatch, "<html><body>nothing matched</body></html>")
    tool = wt.WebSearchTool()
    res = asyncio.run(getattr(tool, method)("任意关键词", 5))
    assert res.success is False
    assert res.output == ""
    assert "未解析出结果" in res.error


def test_chain_continues_to_duckduckgo_when_360_parses_nothing(monkeypatch):
    tool = wt.WebSearchTool()
    monkeypatch.setattr(tool, "_get_tavily_key", lambda: "")
    called = {}

    async def no_results(query, max_results):
        return _res(False, "", "360 页面未解析出结果")

    async def ddg_ok(query, max_results):
        called["q"] = query
        return _res(True, "1. 标题\n   URL: https://e.com\n   摘要")

    monkeypatch.setattr(tool, "_search_360", no_results)
    monkeypatch.setattr(tool, "_search_duckduckgo", ddg_ok)

    res = asyncio.run(tool.execute(ToolContext(), query="k"))
    assert res.success is True
    assert called.get("q") == "k"
    assert "标题" in res.output


def test_all_html_sources_failed_yields_aggregate_error(monkeypatch):
    tool = wt.WebSearchTool()
    monkeypatch.setattr(tool, "_get_tavily_key", lambda: "")

    async def empty360(query, max_results):
        return _res(False, "", "360 解析空")

    async def emptyDdg(query, max_results):
        return _res(False, "", "DDG 解析空")

    monkeypatch.setattr(tool, "_search_360", empty360)
    monkeypatch.setattr(tool, "_search_duckduckgo", emptyDdg)

    res = asyncio.run(tool.execute(ToolContext(), query="k"))
    assert res.success is False
    assert "均失败" in res.error
    assert "DDG 解析空" in res.error


def test_max_results_is_clamped_not_rejected(monkeypatch):
    """越界值钳到 1-20 后继续搜索；非数字才报错"""
    tool = wt.WebSearchTool()
    monkeypatch.setattr(tool, "_get_tavily_key", lambda: "")
    seen = {}

    async def empty360(query, max_results):
        return _res(False, "", "empty")

    async def ddg_ok(query, max_results):
        seen["n"] = max_results
        return _res(True, "ok")

    monkeypatch.setattr(tool, "_search_360", empty360)
    monkeypatch.setattr(tool, "_search_duckduckgo", ddg_ok)

    asyncio.run(tool.execute(ToolContext(), query="k", max_results="999"))
    assert seen["n"] == 20
    asyncio.run(tool.execute(ToolContext(), query="k", max_results=0))
    assert seen["n"] == 1

    bad = asyncio.run(tool.execute(ToolContext(), query="k", max_results="abc"))
    assert bad.success is False


def test_format_results_numbers_and_truncates_snippets():
    out = wt._format_results([
        {"title": "A", "url": "https://a", "snippet": "x" * 400},
        {"title": "", "url": "https://b", "snippet": None},
    ])
    blocks = out.split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].startswith("1. A")
    assert "x" * 300 in blocks[0] and "x" * 301 not in blocks[0]
    assert blocks[1].startswith("2. 无标题")
