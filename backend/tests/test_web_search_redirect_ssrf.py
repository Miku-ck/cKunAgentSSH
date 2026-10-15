"""搜索腿的重定向必须逐跳复核 —— 不能把「跟到哪儿」交给对端说了算。

`netconfig.new_client` 的文档写着 follow_redirects 刻意沿用 httpx 默认（False），
理由是「web_fetch 的 SSRF 守卫就依赖不跟随」。两条 HTML 搜索腿却直接传
`follow_redirects=True`：一旦跟随，Location 完全由响应决定，`302 → http://127.0.0.1:8080/`
`302 → http://169.254.169.254/latest/meta-data/` 走的都是同一条请求链，而 `_read_capped`
只把 (body, encoding) 交回，落地地址根本没过任何校验。

修法不是关掉重定向（so.com / DDG 都会 302 到带来源参数的同站路径，关掉等于把两条
降级源刮成空页），而是**自己跟、每一跳先校验目标地址再发请求** —— 和 web_fetch 同一
条公网规则、同一个跳数上限。这里钉住四件事：

1. 指向内网/保留地址的 Location 被拦下，且**那条 URL 从未被请求过**（先校验后连接）；
   前提由 `test_httpx_would_follow_straight_into_the_internal_host` 用真 httpx 钉住；
2. 同站相对重定向照常跟随（保住降级源可用性，别用「关掉」换安全）；
3. 两条腿不再要求 netconfig 盲目跟随；Tavily 那条仍然不跟随（api_key 在 body 里，
   307/308 会把 body 原样重投给 Location 指定的主机）；
4. web_fetch 与搜索腿对同一个目标给同一个结论，跳数上限共用一个常量。
"""
import asyncio
import ipaddress
import socket
from pathlib import Path

import pytest

import agent.tools.builtin.web_tools as wt
import netconfig
from agent.tools import ToolContext
from agent.tools.builtin.web_tools import _MAX_REDIRECTS, _MAX_RESPONSE_BYTES

PUB = "8.8.8.8"  # 真的公网可路由地址；203.0.113.x 是 TEST-NET 保留段，_is_safe_ip 会拦

# host → 解析结果。既是 DNS 桩，也是「哪些地址算内网」的用例表
DNS = {
    "www.so.com": PUB,
    "html.duckduckgo.com": PUB,
    "api.tavily.com": PUB,
    "redirect.so.com": "8.8.4.4",
    "loopback.test": "127.0.0.1",
    "metadata.test": "169.254.169.254",
    "cgnat.test": "100.64.0.1",
    "rfc1918.test": "10.0.0.5",
    "v6mapped.test": "::ffff:127.0.0.1",
    "dead.test": None,          # None ⇒ 抛 gaierror
}


def _install_dns(monkeypatch):
    """替换 web_tools 里的 socket 模块本身：DNS 走桩，其余逻辑全是真的。"""

    class _FakeSocket:
        gaierror = socket.gaierror

        @staticmethod
        def gethostbyname(host):
            ip = DNS.get(host)
            if ip is None:
                try:
                    ipaddress.ip_address(host)     # URL 直接写 IP 时按字面量回
                    return host
                except ValueError:
                    raise socket.gaierror(f"no such host: {host}") from None
            return ip

    monkeypatch.setattr(wt, "socket", _FakeSocket())


class _Resp:
    def __init__(self, status=200, headers=None, body=b"", encoding="utf-8"):
        self.status_code = status
        self.headers = headers or {}
        self.encoding = encoding
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"Server error {self.status_code}")

    async def aiter_bytes(self, _chunk):
        yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _ChainClient:
    """按 URL 路由响应，并记录**实际被请求**的 URL —— 「先校验后连接」就看这张表。"""

    def __init__(self, routes):
        self.routes = routes
        self.requested = []
        self.client_kwargs = []

    def stream(self, method, url, headers=None, json=None):
        self.requested.append(url)
        if url not in self.routes:
            raise AssertionError(f"用例没准备 {url} 的响应（已请求 {self.requested}）")
        return _Resp(**self.routes[url])

    def new_client(self, **kwargs):
        self.client_kwargs.append(kwargs)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _run(coro):
    return asyncio.run(coro)


def _install(monkeypatch, routes):
    client = _ChainClient(routes)
    monkeypatch.setattr(netconfig, "new_client", client.new_client)
    return client


_360_URL = "https://www.so.com/s?q=x"
_DDG_URL = "https://html.duckduckgo.com/html/?q=x"
_TAVILY_URL = "https://api.tavily.com/search"

_360_HIT = {"body": (b'<li class="res-list"><h3 class="res-title">'
                     b'<a href="https://example.com/a">\xe5\x91\xbd\xe4\xb8\xad</a>'
                     b'</h3></li>')}
_DDG_HIT = {"body": b'<a class="result__a" href="https://example.com/b">hit</a>'}

# 每条腿：起始 URL、刮到结果的响应
_LEGS = [
    pytest.param(_360_URL, _360_HIT, id="360"),
    pytest.param(_DDG_URL, _DDG_HIT, id="duckduckgo"),
]
_METHODS = {_360_URL: "_search_360", _DDG_URL: "_search_duckduckgo"}


# ── 1. 内网目标被拦，且从未被请求 ────────────────────────────────────────────

def test_httpx_would_follow_straight_into_the_internal_host():
    """前提证明：`follow_redirects=True` 交给 httpx 内部，跟随不受我们约束。

    用真 httpx + MockTransport（离网），看它自己把请求打到 Location 指定的内网地址。
    上面那批用例里的假客户端不会自行跟随，所以修复前在它们面前表现为「压根没有校验」
    —— 这条把「会跟进去」这个前提钉死，而不是写在注释里让人信。
    """
    import httpx

    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "www.so.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        return httpx.Response(200, text="内网返回的内容")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     follow_redirects=True) as client:
            resp = await client.get("https://www.so.com/s?q=x")
            return resp.text

    assert _run(go()) == "内网返回的内容"
    assert seen == ["https://www.so.com/s?q=x", "http://127.0.0.1/admin"], seen


@pytest.mark.parametrize("start,hit", _LEGS)
@pytest.mark.parametrize("target", [
    "http://loopback.test/admin",
    "http://metadata.test/latest/meta-data/",
    "http://cgnat.test/",
    "http://rfc1918.test/",
    "http://v6mapped.test/",
])
def test_redirect_to_internal_destination_is_blocked_before_connecting(
        monkeypatch, start, hit, target):
    """这些 URL 是搜索站给的（Location 由对端决定），不是模型给的 —— 但请求确实从
    本进程发出，打到的是本机/云元数据/内网。修复前 follow_redirects=True 会一路跟到
    目的地并读回正文，落地地址从未被校验。"""
    client = _install(monkeypatch, {
        start: {"status": 302, "headers": {"location": target}},
        target: hit,
    })
    _install_dns(monkeypatch)

    result = _run(getattr(wt.WebSearchTool(), _METHODS[start])("x", 5))

    assert result.success is False, f"内网重定向被当成功放行: {result.output}"
    assert "重定向被拦截" in (result.error or ""), result.error
    assert client.requested == [start], (
        f"校验必须先于连接，实际请求序列: {client.requested}")


@pytest.mark.parametrize("start", [_360_URL, _DDG_URL], ids=["360", "duckduckgo"])
def test_relative_redirect_is_validated_after_joining(monkeypatch, start):
    """Location 写成相对形式时，真正要连的 Host 是折叠出来的那台。

    拿原始 `//loopback.test/x` 去校验会因「没有协议」得到一条与真实目标无关的原因；
    折叠后它就是 loopback —— 必须先折叠再校验。
    """
    client = _install(monkeypatch, {
        start: {"status": 302, "headers": {"location": "//loopback.test/x"}},
        "http://loopback.test/x": {"body": b""},
    })
    _install_dns(monkeypatch)

    result = _run(getattr(wt.WebSearchTool(), _METHODS[start])("x", 5))

    assert "重定向被拦截" in (result.error or ""), result.error
    assert "127.0.0.1" in result.error, "原因要写折叠后的真实地址，而不是字面 Location"
    assert client.requested == [start]


# ── 2. 同站重定向照常跟随：安全不能靠关掉功能换 ─────────────────────────────

@pytest.mark.parametrize("start,hit", _LEGS)
def test_same_site_relative_redirect_is_followed_and_parsed(monkeypatch, start, hit):
    """直接关掉重定向也能「安全」，但 so.com / DDG 都会 302 到带来源参数的同站路径，
    那等于把两条降级源刮成空页 —— 这条锁住「跟得上」。"""
    path = start.split("/", 3)[3]
    final = start + "&fr=src"
    client = _install(monkeypatch, {
        start: {"status": 302, "headers": {"location": "/" + path + "&fr=src"}},
        final: hit,
    })
    _install_dns(monkeypatch)

    result = _run(getattr(wt.WebSearchTool(), _METHODS[start])("x", 5))

    assert result.success is True, result.error
    assert client.requested == [start, final], client.requested
    assert "example.com" in result.output


def test_hop_limit_stops_a_redirect_loop_with_a_real_reason(monkeypatch):
    """每一跳都合法（公网），但链子没完 —— 必须报「跳数上限」，不能空正文当成功。"""
    start = _360_URL
    routes = {start: {"status": 302, "headers": {"location": "/s?q=x"}}}
    for i in range(_MAX_REDIRECTS + 3):
        routes[f"https://redirect.so.com/{i}"] = {
            "status": 302, "headers": {"location": f"https://redirect.so.com/{i + 1}"}}
    client = _install(monkeypatch, routes)
    _install_dns(monkeypatch)

    result = _run(wt.WebSearchTool()._search_360("x", 5))

    assert result.success is False
    assert "上限" in (result.error or ""), result.error
    assert len(client.requested) == _MAX_REDIRECTS + 1, client.requested


# ── 3. 客户端不再被要求盲目跟随；Tavily 仍然不跟随 ──────────────────────────

@pytest.mark.parametrize("method", ["_search_360", "_search_duckduckgo",
                                    "_search_tavily"])
def test_legs_do_not_ask_netconfig_to_follow_redirects(monkeypatch, method):
    """`follow_redirects=True` 一旦交给 httpx 内部，复核就只能发生在「已经连上
    Location 指的那个地址之后」。跟随必须由 _read_search_html 逐跳做。"""
    url = _TAVILY_URL if method == "_search_tavily" else {
        "_search_360": _360_URL,
        "_search_duckduckgo": _DDG_URL,
    }[method]
    body = b'{"results": []}' if method == "_search_tavily" else b"<html></html>"
    client = _install(monkeypatch, {url: {"body": body}})
    _install_dns(monkeypatch)

    tool = wt.WebSearchTool()
    if method == "_search_tavily":
        _run(tool._search_tavily("x", 5, "secret-key"))
    else:
        _run(getattr(tool, method)("x", 5))

    assert client.client_kwargs, "没走 netconfig 出口"
    for kw in client.client_kwargs:
        assert "follow_redirects" not in kw, kw


def test_tavily_never_reposts_the_api_key_to_a_redirect_target(monkeypatch):
    """api_key 在 JSON body 里；307/308 会把 body 原样重投给 Location 指定的主机。

    这条腿不跟随是刻意的（代码里也写了原因），所以这里钉住「给了 Location 也不跟」。
    """
    client = _install(monkeypatch, {
        "https://api.tavily.com/search": {
            "status": 307, "headers": {"location": "http://loopback.test/collect"}},
        "http://loopback.test/collect": {"body": b"{}"},
    })
    _install_dns(monkeypatch)

    result = _run(wt.WebSearchTool()._search_tavily("x", 5, "secret-key"))

    assert result.success is False
    assert client.requested == ["https://api.tavily.com/search"], client.requested


# ── 4. 两条通道同规则、同上限 ───────────────────────────────────────────────

@pytest.mark.parametrize("url,should_pass", [
    ("https://www.so.com/s?q=x", True),
    ("http://loopback.test/", False),
    ("http://metadata.test/", False),
    ("http://cgnat.test/", False),
    ("http://rfc1918.test/", False),
    ("http://v6mapped.test/", False),
    ("http://dead.test/", False),
    ("ftp://www.so.com/x", False),
])
def test_search_guard_and_web_fetch_share_one_verdict(monkeypatch, url, should_pass):
    """「同一个地址，换个写法不换结论」—— web_fetch 的守卫与搜索腿的复核必须同判。

    规则本体只有一处（`_resolve_public_destination`）；两条通道各自实现一遍「什么算
    内网」就会漂移，而漂移的方向通常是搜索这条更松。
    """
    _install_dns(monkeypatch)
    safe_fetch, info_fetch = _run(wt.WebFetchTool()._check_url_safe(url))
    safe_search, ip_search, reason_search = _run(wt._resolve_public_destination(url))

    assert safe_fetch is should_pass, (url, info_fetch)
    assert safe_search is should_pass, (url, reason_search)
    if should_pass:
        assert info_fetch == ip_search == PUB
    else:
        assert info_fetch == reason_search


def test_public_rule_text_lives_in_one_place():
    """协议/空 host 的拒绝文案只允许出现一次（web_fetch 里那份重复已合并）。"""
    src = Path(wt.__file__).read_text(encoding="utf-8")
    assert src.count('不允许的协议: ') == 1, "两处各写一遍就会漂移"
    assert src.count('"无效的 URL"') == 1
    # 同步 DNS 只有一次，而且必须在线程池里（文档字符串里提一次名字不算）
    assert src.count("to_thread(socket.gethostbyname") == 1


def test_fetch_hop_count_uses_the_shared_constant(monkeypatch):
    """上限是常量而不是文案里的数字：搜索腿改上限时 fetch 必须一起动。"""
    calls = []

    async def _safe(_url):
        return (True, PUB)

    async def _get(self, client, direct_url, headers, sni, cap=_MAX_RESPONSE_BYTES):
        calls.append(direct_url)
        return 302, "http://203.0.113.11/next", ""

    tool = wt.WebFetchTool()
    monkeypatch.setattr(tool, "_check_url_safe", _safe)
    monkeypatch.setattr(wt.WebFetchTool, "_get_with_limit", _get)
    monkeypatch.setattr(wt, "_extract_body", lambda h: h)

    result = _run(tool.execute(ToolContext(), "https://example.com/loop"))

    assert len(calls) == _MAX_REDIRECTS + 1, calls
    assert f"{_MAX_REDIRECTS} 次上限" in result.error, result.error
