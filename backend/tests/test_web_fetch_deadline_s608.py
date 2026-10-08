"""
S6-08 回归：web_fetch 的整请求总时限。

httpx 的标量 timeout 管的是**每次套接字操作**，不是整请求 deadline：慢滴流服务端
每 <15s 吐一个 64KB 块，就能在 10MB 上限内把一次调用钉住几分钟，重定向链最多再乘
6 次 ⇒ 模型选一个 URL 就能占死整个 run。修法是把工具自己声明的 meta.timeout 变成
真判据（asyncio.timeout 罩住整个网络段），而不是新加一个没人读的常量。
"""
import asyncio

import pytest

import agent.tools.builtin.web_tools as wt
from agent.tools import ToolContext, ToolMeta


def _tool(timeout: float) -> wt.WebFetchTool:
    tool = wt.WebFetchTool()
    # 实例属性遮蔽类属性，别改动共享的 meta
    tool.meta = ToolMeta(risk_level="safe", reversible=True, cacheable=True,
                         idempotent=True, timeout=timeout)
    return tool


def _stub_fetch(monkeypatch, tool, *, delay: float, status: int = 200,
                body: str = "<p>ok</p>", location: str = ""):
    async def _safe(_url):
        return (True, "203.0.113.10")

    monkeypatch.setattr(tool, "_check_url_safe", _safe)

    async def fake_get(self, client, direct_url, headers, sni, cap=wt._MAX_RESPONSE_BYTES):
        if delay:
            await asyncio.sleep(delay)
        return status, location, body

    monkeypatch.setattr(wt.WebFetchTool, "_get_with_limit", fake_get)
    monkeypatch.setattr(wt, "_strip_html", lambda h: h)
    monkeypatch.setitem(__import__("sys").modules, "trafilatura", None)


@pytest.mark.asyncio
async def test_slow_drip_hit_total_deadline_instead_of_running_forever(monkeypatch):
    """每块都在单次操作超时内，但整请求早就超过总预算 —— 必须被掐断并如实报错。

    302 + Location 让重定向链真的转起来（最多 5 跳），每次 0.05s 的「套接字操作」
    都远低于 httpx 的 15s 标量超时；总时限 0.12s 在第三跳之前把它打掉。
    """
    tool = _tool(0.12)
    _stub_fetch(monkeypatch, tool, delay=0.05, status=302,
                location="http://example.com/next")

    result = await tool.execute(ToolContext(), "http://example.com/slow")

    assert result.success is False, "慢滴流不得无限占住 run"
    assert "总时限" in result.error, f"应报总时限截断，实际：{result.error}"


@pytest.mark.asyncio
async def test_prompt_fetch_still_succeeds_under_the_deadline(monkeypatch):
    tool = _tool(30.0)
    _stub_fetch(monkeypatch, tool, delay=0.01, body="<p>hello</p>")

    result = await tool.execute(ToolContext(), "http://example.com/fast")

    assert result.success is True, f"正常响应不该被总时限打掉：{result.error}"
    assert "hello" in result.output
