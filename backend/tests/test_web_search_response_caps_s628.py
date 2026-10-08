# -*- coding: utf-8 -*-
"""S6-28 回归 — 三条搜索腿必须有响应体上限，Tavily 的返回列表要在本地截断。

web_fetch 早在审查 M-4 就配了封顶流式读（`_get_with_limit`），但搜索这三条腿直接
`resp.text` / `resp.json()`：数 MB 的结果页整个进内存，紧接着的正则
（`re.finditer(..., DOTALL)`）还是在**事件循环线程**上对这几 MB 做 —— 一次搜索
就能把后端的所有请求卡住。同一条防护既然给 fetch 加了，搜索腿没道理漏着。
"""
import asyncio
import json

import pytest

from agent.tools.builtin import web_tools
from agent.tools.builtin.web_tools import ResponseTooLarge, _read_capped


class _StreamResp:
    def __init__(self, status=200, headers=None, chunks=(b"",), encoding="utf-8",
                 raise_on_status=True):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.encoding = encoding
        self._raise_on_status = raise_on_status
        self.pulled = 0

    def raise_for_status(self):
        if self._raise_on_status and self.status_code >= 400:
            raise RuntimeError(f"Server error {self.status_code}")

    async def aiter_bytes(self, n):
        for c in self._chunks:
            self.pulled += len(c)
            yield c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def stream(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url, "json": json})
        return self.resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _run(coro):
    return asyncio.run(coro)


# ── _read_capped：封顶读取 ────────────────────────────────────────────────────

def test_content_length_over_cap_aborts_without_downloading():
    resp = _StreamResp(headers={"content-length": "99999999"}, chunks=[b"x" * 10])
    client = _FakeClient(resp)
    with pytest.raises(ResponseTooLarge):
        _run(_read_capped(client, "GET", "https://x.test", cap=1024))
    assert resp.pulled == 0, "Content-Length 已经超限时不该再下载任何字节"


def test_chunked_overflow_aborts_mid_stream():
    """无 Content-Length 的服务器（分块）也得拦住 —— 逐块累计是第二道闸。"""
    resp = _StreamResp(chunks=[b"a" * 4096] * 10)
    with pytest.raises(ResponseTooLarge):
        _run(_read_capped(_FakeClient(resp), "GET", "https://x.test", cap=1024))


def test_body_and_encoding_returned_intact_under_cap():
    resp = _StreamResp(headers={"content-length": "6"}, chunks=[b"abc", b"def"],
                       encoding="gbk")
    body, enc = _run(_read_capped(_FakeClient(resp), "GET", "https://x.test", cap=1024))
    assert body == b"abcdef"
    # 编码必须原样带出：360 这类页面是 gbk，写死 utf-8 会把结果刮成乱码
    assert enc == "gbk"


def test_http_error_still_raises_before_reading_body():
    """行为不变：4xx/5xx 仍旧走各腿的 except，而不是静默返回空 body。"""
    resp = _StreamResp(status=503, chunks=[b"nope"])
    with pytest.raises(RuntimeError):
        _run(_read_capped(_FakeClient(resp), "GET", "https://x.test", cap=1024))
    assert resp.pulled == 0


def test_junk_content_length_does_not_bypass_the_cap():
    """脏 Content-Length（分块编码下随便填）解析不了时，只能靠逐块累计那道闸。"""
    resp = _StreamResp(headers={"content-length": "abc"}, chunks=[b"z" * 4096])
    with pytest.raises(ResponseTooLarge):
        _run(_read_capped(_FakeClient(resp), "GET", "https://x.test", cap=1024))


# ── Tavily 腿：上限 + 本地截断 ───────────────────────────────────────────────

def _tavily_payload(n: int) -> bytes:
    return json.dumps({
        "answer": "A",
        "results": [{"title": f"t{i}", "url": f"http://s/{i}", "content": "c" * 400}
                    for i in range(n)],
    }).encode()


def test_tavily_truncates_results_to_max_results(monkeypatch):
    """此前 max_results 只当请求参数发出去，返回列表没在本地截过 ⇒ 上限看服务端心情。"""
    import netconfig

    payload = _tavily_payload(10)
    client = _FakeClient(_StreamResp(headers={"content-length": str(len(payload))},
                                     chunks=[payload]))
    monkeypatch.setattr(netconfig, "new_client", lambda **kw: client)

    tool = web_tools.WebSearchTool()
    result = _run(tool._search_tavily("q", 3, "k"))
    assert result.success is True
    body = result.output
    assert "3. t2" in body
    assert "4. t3" not in body, f"没按 max_results 截断：\n{body}"
    # 请求侧仍然带上 max_results
    assert client.calls[0]["json"]["max_results"] == 3


def test_tavily_honors_the_response_cap(monkeypatch):
    import netconfig

    huge = _tavily_payload(2000)
    client = _FakeClient(_StreamResp(chunks=[huge[:200_000]] * 20))
    monkeypatch.setattr(netconfig, "new_client", lambda **kw: client)
    result = _run(web_tools.WebSearchTool()._search_tavily("q", 5, "k"))
    assert result.success is False
    assert "上限" in (result.error or ""), result.error


@pytest.mark.parametrize("method", ["_search_360", "_search_duckduckgo"])
def test_html_legs_honor_the_response_cap(monkeypatch, method):
    """两条 HTML 腿同样封顶，且失败是 ToolResult(False) 而不是抛穿。"""
    import netconfig

    client = _FakeClient(_StreamResp(chunks=[b"<html>" + b"x" * 100_000] * 30))
    monkeypatch.setattr(netconfig, "new_client", lambda **kw: client)
    result = _run(getattr(web_tools.WebSearchTool(), method)("q", 5))
    assert result.success is False, method
    assert "上限" in (result.error or ""), (method, result.error)
