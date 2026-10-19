"""
联网搜索工具 — Work / IDE 模式

  - WebSearchTool: 调用搜索引擎 API（Tavily / 360 / DuckDuckGo），返回结构化结果
  - WebFetchTool:  抓取指定 URL 内容，转为文本
"""
import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse, urljoin

from agent.tools import BaseTool, ToolResult, ToolMeta

logger = logging.getLogger(__name__)

# 默认搜索结果数量
_DEFAULT_MAX_RESULTS = 5

# web_fetch 响应体上限（审查 M-4）：超过即拒绝，防止恶意页面打爆后端内存
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# C-review MINOR-10：max_chars 上限 — 原实现无上限，LLM 可传 10^9，
# 10MB 响应全量进上下文（约 2.5M token，必然超窗口且烧钱）
_MAX_FETCH_CHARS = 100_000

# S6-28：三条搜索腿的响应体上限。web_fetch 早在审查 M-4 就配了封顶流式读
# （_get_with_limit），搜索这三条腿却直接 `resp.text` / `resp.json()` —— 数 MB 的
# 搜索结果页会整个进内存，而随后的 `re.finditer(..., DOTALL)` 是在事件循环线程上
# 对这几 MB 做正则。固定站点 ⇒ 多数是「大页面」而不是攻击，但同一条防护既然给
# fetch 加了，搜索腿没道理漏着。
_MAX_SEARCH_BYTES = 2 * 1024 * 1024

# S6-28 的另一半：封顶读只保证「不会把几 MB 拉进内存」，不保证「进上下文的字数量
# 可控」。单条结果的 title/url/answer 长度同样由对端决定（300 字只截了 snippet），
# 所以逐字段截 + 整段再给一个总上限，喂给模型的搜索文本才有可预测的预算。
_MAX_SEARCH_FIELD_CHARS = 300
_MAX_SEARCH_OUTPUT_CHARS = 12_000

# 重定向跳数上限。web_fetch 的逐跳校验链里原本写死三处 5（两处判断 + 一处错误文案），
# 搜索腿再接一条链就必须共用同一个数 —— 否则「上限」会随调用点分家。
_MAX_REDIRECTS = 5

# 视为「要跟随」的 3xx。304 不在内：它没有 Location，也不是重定向。
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class ResponseTooLarge(RuntimeError):
    """响应体超过 _MAX_SEARCH_BYTES —— 与各腿既有的 `except Exception` 文案兼容。"""


class SsrfBlocked(RuntimeError):
    """重定向目标不是公网可路由地址 / 跳数超上限 —— 同样被各腿的 except 兜住。"""


async def _resolve_public_destination(url: str) -> tuple[bool, str, str]:
    """URL 的 hostname 是否解析到公网可路由地址，返回 (放行, IP, 拒绝原因)。

    web_fetch 与搜索腿共用这一条规则（两处各自写一遍「什么算内网」必然漂移）。
    E 修复：socket.gethostbyname 是同步 DNS 查询，放线程池避免卡事件循环。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "", f"不允许的协议: {parsed.scheme}"
    hostname = parsed.hostname
    if not hostname:
        return False, "", "无效的 URL"
    try:
        ip = await asyncio.to_thread(socket.gethostbyname, hostname)
    except socket.gaierror:
        return False, "", f"DNS 解析失败: {hostname}"
    safe, reason = _is_safe_ip(ip)
    if not safe:
        return False, "", reason
    return True, ip, ""


def _join_redirect(current: str, location: str) -> str:
    """Location 可能是相对的（360 给 `/s?...`），按当前 URL 折成绝对地址再校验。

    必须先折叠后校验：拿 `/s?q=x` 去查协议只会得到一条与真实目标无关的「协议不允许」
    原因；而请求实际打到的 Host 是折叠出来的那台 —— 校验对象和请求对象必须是同一个
    URL，否则复核形同虚设。
    """
    return urljoin(current, location.strip())


async def _read_capped(client, method: str, url: str, *,
                       headers: dict | None = None, json_body: dict | None = None,
                       cap: int = _MAX_SEARCH_BYTES) -> tuple[bytes, str, str]:
    """流式读取并封顶，返回 (响应体, 编码名, Location)。

    与 `_get_with_limit` 同一手法，但那条绑着 SSRF 的 IP 固定与 sni 扩展，
    搜索腿用真实域名，故另开这一条共用的。`raise_for_status` 留在读体之前，
    保持各腿原先「4xx/5xx 走 except、报 搜索失败: …」的行为不变。

    编码取自 `resp.encoding`（httpx 按 Content-Type 的 charset 推，缺省 utf-8），
    HTML 型搜索页（360 常见 gbk）必须用它解，写死 utf-8 会把结果刮成乱码。

    Location 只在 3xx 时带出，且 3xx 不下载正文：跟随与否由调用方决定
    （见 `_read_search_html`），跟随时旧正文必然被丢弃，白占一次下载额度。
    不跟随的 Tavily 腿拿到空 body —— 它原本也是 JSON 解析失败报错，只是那段正文
    本来就用不上。
    """
    async with client.stream(method, url, headers=headers, json=json_body) as resp:
        resp.raise_for_status()
        encoding = resp.encoding or "utf-8"
        if resp.status_code in _REDIRECT_STATUSES:
            return b"", encoding, resp.headers.get("location", "")
        clen = resp.headers.get("content-length")
        if clen:
            # Content-Length 可能是脏值（分块编码下服务器随便填）：解析不了就
            # 只靠逐块累计那一道闸，不能因为读不出数字而放行超大响应
            try:
                over = int(clen) > cap
            except ValueError:
                over = False
            if over:
                raise ResponseTooLarge(
                    f"搜索结果响应 {clen} 字节，超过 {cap // 1024}KB 上限")
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes(65536):
            total += len(chunk)
            if total > cap:
                raise ResponseTooLarge(
                    f"搜索结果响应超过 {cap // 1024}KB 上限（已读到 {total} 字节）")
            chunks.append(chunk)
        return b"".join(chunks), encoding, ""


async def _read_search_html(client, url: str, headers: dict) -> str:
    """两条 HTML 腿共用的取页：封顶读 + 逐跳公网复核，返回解码后的页面文本。

    httpx 侧一律 follow_redirects=False（netconfig 出口的那条不变量），跟随由这里手动
    做 —— 这样每一跳都是「先校验目标地址、再发请求」，而不是 httpx 内部跟完之后再验
    落地地址。搜索腿必须能跟：so.com / DDG 都会 302 到带来源参数的同站路径，直接关掉
    等于把两条降级源刮成空页。
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        body, enc, location = await _read_capped(client, "GET", current, headers=headers)
        if not location:
            return body.decode(enc, "replace")
        next_url = _join_redirect(current, location)
        safe, _ip, reason = await _resolve_public_destination(next_url)
        if not safe:
            raise SsrfBlocked(f"重定向被拦截: {reason}")
        current = next_url
    raise SsrfBlocked(f"重定向次数超过 {_MAX_REDIRECTS} 次上限，放弃搜索")


# HTML 型搜索源（360 / DuckDuckGo）共用的浏览器 UA：两者都靠正则刮页面，
# 缺一致的 Accept-Language/UA 容易被判定为爬虫返回验证页
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 刮不到结果 ≠ 搜不到结果：返回失败才会走下一级降级源，
# 否则页面改版/反爬的那一级会把「无搜索结果」当成功直接回给模型。
_EMPTY_PARSE_HINT = "页面未解析出结果（可能已改版或被反爬拦截）"


# 审查 M-4：显式封锁 CPython `is_global` 表未覆盖、但可路由到内网的地址段。
# - 100.64.0.0/10 (CGNAT)：运营商级 NAT，可达内网设备，is_private 恒为 False
# - 192.0.0.0/29 / 192.0.0.170/31：IETF 协议分配 / AS112-VS
# - 198.18.0.0/15：网络基准测试（部分云内网使用）
# - 2001::/32 (Teredo) / 2002::/16 (6to4) / 64:ff9b::/96 (NAT64)：
#   已废弃的 IPv6 过渡机制，6to4/NAT64 可内嵌任意 IPv4（含内网地址）
#   （64:ff9b:1::/48 已包含在 64:ff9b::/96 内，无需重复列出）
_EXTRA_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n) for n in (
        "100.64.0.0/10",
        "192.0.0.0/29",
        "192.0.0.170/31",
        "198.18.0.0/15",
        "2001::/32",
        "2002::/16",
        "64:ff9b::/96",
    )
]


def _is_safe_ip(ip: str) -> tuple[bool, str]:
    """检查 IP 是否安全（防 SSRF：只允许公网可路由地址）

    审查 M-4：基础检查改用 `is_global`（CPython 3.13 下同时覆盖
    private/loopback/link-local/multicast/reserved/unspecified 全部语义），
    并叠加显式封锁表（CGNAT/Teredo/6to4/NAT64 等 is_global 漏掉的段）。

    额外防护：检测 IPv6-mapped IPv4 地址（如 ::ffff:127.0.0.1），
    提取内嵌 IPv4 重新校验，防止通过 mapped 地址绕过。
    """
    try:
        ip_obj = ipaddress.ip_address(ip)

        # IPv6-mapped IPv4 地址检测（如 ::ffff:127.0.0.1）
        if isinstance(ip_obj, ipaddress.IPv6Address):
            mapped = ip_obj.ipv4_mapped
            if mapped is not None:
                # 是 mapped IPv4，递归检查内嵌的 IPv4
                return _is_safe_ip(str(mapped))

        # 只放行公网可路由地址（is_global 为 False 即 private/loopback/
        # link-local/multicast/reserved/unspecified 等全部内网语义）
        if not ip_obj.is_global:
            return False, f"拒绝访问内网/保留/多播地址: {ip}"
        # 显式封锁 CPython 表未覆盖的内网可达段（CGNAT/Teredo/6to4/NAT64 等）
        for net in _EXTRA_BLOCKED_NETWORKS:
            if ip_obj.version == net.version and ip_obj in net:
                return False, f"拒绝访问特殊用途地址: {ip}"
        return True, ""
    except ValueError:
        return False, f"无效的 IP 地址: {ip}"


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "联网搜索，返回相关结果的标题、摘要和 URL。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "最大结果数（默认 5）"},
        },
        "required": ["query"],
    }
    meta = ToolMeta(risk_level="safe", reversible=True, cacheable=True, idempotent=True)

    async def execute(self, context, query, max_results=_DEFAULT_MAX_RESULTS):
        # 参数校验：max_results 必须是整数，区间越界按边界钳制
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            return ToolResult(False, "", "max_results 必须是整数")
        max_results = max(1, min(max_results, 20))

        # 尝试 Tavily API（用户配置 key 时优先）
        # 取 key 要读 SQLite（get_app_setting），而这里是 async def：直接调用等于把
        # 一次可能的 busy_timeout（PRAGMA busy_timeout=5000）压在事件循环上 —— 审计或
        # 调度线程持写锁时，所有 run 与 SSE 流一起停摆。与本仓其余 DB 读同口径走线程。
        tavily_key = await asyncio.to_thread(self._get_tavily_key)
        if tavily_key:
            result = await self._search_tavily(query, max_results, tavily_key)
            if result.success:
                return result
            logger.warning("Tavily 搜索失败，降级到 360: %s", result.error)

        # 降级 1：360 搜索 HTML（国内可直接访问，无需 API key）
        result = await self._search_360(query, max_results)
        if result.success:
            return result
        logger.warning("360 搜索失败，降级到 DuckDuckGo: %s", result.error)

        # 降级 2：DuckDuckGo 免费 HTML 搜索。两级都空手时回聚合原因 ——
        # 单看最后一条错误会让人以为只是 DDG 挂了。
        result = await self._search_duckduckgo(query, max_results)
        if result.success:
            return result
        return ToolResult(False, "", f"联网搜索未取到结果（360 与 DuckDuckGo 均失败）: {result.error}")

    def _get_tavily_key(self) -> str:
        """获取 Tavily API Key：环境变量优先，其次数据库（设置面板写入）"""
        import os
        key = (os.environ.get("TAVILY_API_KEY") or "").strip()
        if key:
            return key
        try:
            import database as db
            return (db.get_app_setting("web.tavily_api_key") or "").strip()
        except Exception:
            return ""

    async def _search_tavily(self, query: str, max_results: int, api_key: str) -> ToolResult:
        """调用 Tavily Search API"""
        try:
            import json

            import netconfig
            # 这一条腿**故意**不跟重定向（区别于两条 HTML 腿的 _read_search_html）：
            # api_key 在 JSON body 里，307/308 会把 body 原样重投给 Location 指定的
            # 主机 ⇒ 等于把密钥交给对端说了算的地址。要跟随必须先改成 Header 鉴权。
            async with netconfig.new_client(default_timeout=15.0) as client:
                raw, _enc, _loc = await _read_capped(
                    client, "POST", "https://api.tavily.com/search",
                    json_body={
                        "api_key": api_key,
                        "query": query,
                        "max_results": max_results,
                        "include_answer": True,
                    },
                )
                data = json.loads(raw)

            # 格式化输出
            parts = []
            if data.get("answer"):
                parts.append(
                    f"摘要: {str(data['answer'])[:_MAX_SEARCH_OUTPUT_CHARS]}\n")

            # S6-28：`max_results` 此前只当请求参数发出去，返回列表本身没在本地
            # 截过 —— 服务端（或中间层）多给就全量进上下文，上限完全看对方心情。
            results = [{
                "title": r.get("title"),
                "url": r.get("url"),
                "snippet": r.get("content"),
            } for r in (data.get("results") or [])[:max_results]]
            # 排版与 360 / DDG 两条腿本就是同一个形状，故共用格式化函数：
            # 字段截断规则改一处三条腿同时生效，不会再出现「只给两级加了上限」。
            body = _format_results(results)
            if body:
                parts.append(body)

            # 搜索结果：常规优先级
            return ToolResult(True, "\n\n".join(parts) if parts else "无搜索结果", priority="normal")

        except Exception as e:
            logger.exception("Tavily 搜索失败")
            return ToolResult(False, "", f"搜索失败: {e}")

    async def _search_360(self, query: str, max_results: int) -> ToolResult:
        """360 搜索 HTML（无需 API key，国内可直接访问）"""
        from urllib.parse import quote_plus

        try:
            search_url = f"https://www.so.com/s?q={quote_plus(query)}"
            headers = {**_BROWSER_HEADERS, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

            import netconfig
            async with netconfig.new_client(default_timeout=15.0) as client:
                html = await _read_search_html(client, search_url, headers)

            results = self._parse_360_html(html, max_results)
            if not results:
                return ToolResult(False, "", f"360 {_EMPTY_PARSE_HINT}")

            return ToolResult(True, _format_results(results), priority="normal")

        except Exception as e:
            logger.exception("360 搜索失败")
            return ToolResult(False, "", f"360 搜索失败: {e}")

    def _parse_360_html(self, html: str, max_results: int) -> list[dict]:
        """解析 360 搜索 HTML 结果（不依赖 bs4）"""
        import re

        results = []
        # 匹配每个结果块 <li class="res-list ...">...</li>
        for m in re.finditer(
            r'<li[^>]*class="[^"]*\bres-list\b[^"]*"[^>]*>(.*?)</li>',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            if len(results) >= max_results:
                break
            block = m.group(1)

            # 标题和 URL：<h3 class="res-title "><a href="...">title</a></h3>
            title_match = re.search(
                r'<h3[^>]*class="[^"]*\bres-title\b[^"]*"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?</h3>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if not title_match:
                continue
            url = title_match.group(1).strip()
            title = _strip_html(title_match.group(2))
            if not title:
                continue

            # 摘要：<p class="res-desc">...</p>
            snippet_match = re.search(
                r'<p[^>]*class="res-desc"[^>]*>(.*?)</p>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            snippet = _strip_html(snippet_match.group(1)) if snippet_match else ""

            # 过滤掉 360 自己的中转链接页和无效协议
            if url.startswith("javascript:") or url.startswith("/s?"):
                continue
            if url.startswith("//"):
                url = "https:" + url

            results.append({"title": title, "url": url, "snippet": snippet})
        return results

    async def _search_duckduckgo(self, query: str, max_results: int) -> ToolResult:
        """DuckDuckGo HTML 搜索（无需 API key）"""
        from urllib.parse import quote_plus

        try:
            search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            headers = {**_BROWSER_HEADERS, "Accept-Language": "en-US,en;q=0.9"}

            import netconfig
            async with netconfig.new_client(default_timeout=15.0) as client:
                html = await _read_search_html(client, search_url, headers)

            results = self._parse_duckduckgo_html(html, max_results)
            if not results:
                return ToolResult(False, "", f"DuckDuckGo {_EMPTY_PARSE_HINT}")

            return ToolResult(True, _format_results(results), priority="normal")

        except Exception as e:
            logger.exception("DuckDuckGo 搜索失败")
            return ToolResult(False, "", f"搜索失败: {e}")

    def _parse_duckduckgo_html(self, html: str, max_results: int) -> list[dict]:
        """解析 DuckDuckGo HTML 搜索结果（不依赖 bs4）"""
        import re

        results = []
        # 匹配每个结果标题：class="result__a"
        for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            if len(results) >= max_results:
                break
            href = m.group(1).strip()
            title = _strip_html(m.group(2))
            if not title:
                continue

            # 在该标题后面一小段 HTML 中找摘要和显示 URL
            after = html[m.end() : m.end() + 2500]
            snippet_match = re.search(
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                after,
                flags=re.DOTALL | re.IGNORECASE,
            )
            snippet = _strip_html(snippet_match.group(1)) if snippet_match else ""

            url_match = re.search(
                r'<a[^>]*class="result__url"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                after,
                flags=re.DOTALL | re.IGNORECASE,
            )
            display_url = _strip_html(url_match.group(2)) if url_match else ""

            # DuckDuckGo 的 href 可能是重定向链接，优先用显示 URL
            url = display_url or href
            if url.startswith("//"):
                url = "https:" + url

            results.append({"title": title, "url": url, "snippet": snippet})
        return results


def _format_results(results: list[dict]) -> str:
    """把 {title,url,snippet} 列表排成喂给模型的编号文本（三条腿共用）"""
    text = "\n\n".join(
        f"{i}. {(r.get('title') or '无标题')[:_MAX_SEARCH_FIELD_CHARS]}\n"
        f"   URL: {(r.get('url') or '')[:_MAX_SEARCH_FIELD_CHARS]}\n"
        f"   {(r.get('snippet') or '')[:_MAX_SEARCH_FIELD_CHARS]}"
        for i, r in enumerate(results, 1)
    )
    return text[:_MAX_SEARCH_OUTPUT_CHARS]


def _skip_script_style(html: str) -> str:
    """线性扫掉 `<script>` / `<style>` 段，替代 `.*?</\\1>` 的 DOTALL 回溯。

    非贪婪 DOTALL 遇到「有开标签、没有闭标签」时，会把 `.*?` 从该位置一路扩到全文末尾
    才发现匹配失败 —— 代价是每个未闭合标签一次 O(len)。实测 4MB 正文：0 个未闭合
    `<script>` 0.04s，50 个 1.20s，200 个 6.27s，800 个 **28.66s**，而标签个数由模型选的
    URL 说了算（一个开标签 8 字节，10MB 上限内能塞几十万个）。

    未闭合时按 HTML 自己的语义办：开标签之后的内容全在这个标签里，整段丢弃 —— 这也是
    浏览器看到的结果，原来那种「匹配失败就整段留原文」反而是把脚本源码当正文喂给模型。
    """
    low = html.lower()
    n = len(html)
    out: list[str] = []
    i = 0
    while i < n:
        script_at = _find_tag_start(low, "<script", i)
        style_at = _find_tag_start(low, "<style", i)
        hits = [p for p in (script_at, style_at) if p != -1]
        if not hits:
            out.append(html[i:])
            break
        j = min(hits)
        tag = "script" if j == script_at else "style"
        open_end = low.find(">", j)
        if open_end == -1:
            out.append(html[i:j])
            break
        close = low.find(f"</{tag}", open_end)
        if close == -1:
            out.append(html[i:j])
            break
        gt = low.find(">", close)
        out.append(html[i:j])
        i = n if gt == -1 else gt + 1
    return "".join(out)


def _find_tag_start(low: str, tag: str, start: int) -> int:
    """找 `<script` / `<style`，要求后面是空白或 `>` —— `<scripting>` 不算。"""
    pos = start
    while True:
        p = low.find(tag, pos)
        if p == -1:
            return -1
        nxt = low[p + len(tag): p + len(tag) + 1]
        if nxt in ("", " ", "\t", "\n", "\r", ">", "/"):
            return p
        pos = p + 1


def _extract_body(html: str) -> str:
    """正文提取（同步 CPU，调用方要放到线程里并纳入总时限）"""
    try:
        import trafilatura
        text = trafilatura.extract(html) or _strip_html(html)
    except ImportError:
        text = _strip_html(html)
    return text


def _strip_html(html: str) -> str:
    """简易 HTML 标签去除（无 trafilatura 时的降级方案）"""
    import html as html_module
    import re

    # 去 script/style
    html = _skip_script_style(html)
    # 去标签
    text = re.sub(r"<[^>]+>", "", html)
    # 解码 HTML 实体（&amp; &lt; &gt; &nbsp; &#39; &quot; 等）
    text = html_module.unescape(text)
    # 压缩空白
    text = re.sub(r"\s+", " ", text).strip()
    return text


_TEXTUAL_TYPES = (
    "text/", "application/json", "application/xml", "application/xhtml",
    "application/javascript", "application/x-javascript", "application/rss",
    "application/atom", "application/ld",
)

# 只把正文的前这么多字符交给解析器。输出本来就被 `max_chars`（上限 100k）截掉，
# 拿 10MB 全量去解析只是为了得到一个 100k 的结果。
_PARSE_WINDOW_CHARS = 1_000_000


def _is_parsable_content(content_type: str) -> bool:
    """只解析文本型响应。

    原来这条路上没有内容类型判据：`_get_with_limit` 收满 10MB 任何类型的字节就
    `decode("utf-8", "replace")` 成一个同尺寸的 Python 字符串，再交给正则/trafilatura。
    实测 10MB 二进制解码后仍要占约 60MB 峰值，且解析纯耗 CPU。
    Content-Type 缺失时**放行**：大量静态站与旧服务器不给这个头，一票否决会让
    web_fetch 在真实环境下不可用；长度与解析窗口仍受上面两道约束。
    """
    if not content_type:
        return True
    base = content_type.split(";", 1)[0].strip().lower()
    if not base:
        return True
    return any(base.startswith(t) for t in _TEXTUAL_TYPES) or (
        "html" in base or "+json" in base or "+xml" in base)


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "抓取指定 URL 的网页内容，返回文本（自动去除 HTML 标签）。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的 URL"},
            "max_chars": {"type": "integer", "description": "最大返回字符数（默认 8000，上限 100000）"},
        },
        "required": ["url"],
    }
    meta = ToolMeta(risk_level="safe", reversible=True, cacheable=True, idempotent=True)

    async def execute(self, context, url, max_chars=8000):
        import httpx

        # 参数校验：max_chars 必须是整数；下限 1，上限 _MAX_FETCH_CHARS
        # （C-review MINOR-10：钳制上限，防止整页 10MB 灌入上下文）
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            return ToolResult(False, "", "max_chars 必须是整数")
        max_chars = max(1, min(max_chars, _MAX_FETCH_CHARS))

        # 解析 URL：parsed/hostname 后面拼直连地址要用。协议与空 host 的拒绝文案
        # 只在 `_resolve_public_destination` 一处 —— 这里再抄一遍就会和搜索腿漂移，
        # 而两处给出的原因本来必须一致。
        parsed = urlparse(url)
        hostname = parsed.hostname

        # 预先解析并校验目标 IP，防止 DNS rebinding / SSRF
        # E 修复：内部同步 DNS 查询已放线程池，需 await
        safe, ip_or_reason = await self._check_url_safe(url)
        if not safe:
            return ToolResult(False, "", ip_or_reason)

        headers = {"User-Agent": "cKunAgent/1.0", "Host": hostname}

        try:
            # 使用解析后的 IP 直连，同时通过 sni_hostname 保留原始域名做 TLS/SNI，
            # 既防止 DNS rebinding，又兼容 CDN / 虚拟主机。
            # 审查 M-4：流式读取 + _MAX_RESPONSE_BYTES 上限，原 `resp.text` 无界
            # （100MB+ 页面直接打爆内存 → 后端 OOM 影响所有请求）。
            # G4 有意**不**接 netconfig 代理：这里是「用解析出的 IP 直连 + sni_hostname
            # 保留原域名」的 DNS rebinding 守卫。一旦交给代理，代理服务器自己重新解析
            # 域名，这层守卫整体失效；宁可让纯代理出网环境下的 web_fetch 不可用。
            # S6-08：整请求**总**时限。httpx 的标量 timeout 是「每次套接字操作」的
            # 超时，不是整请求 deadline —— 慢滴流服务端每 <15s 吐一个 64KB 块，就能在
            # 10MB 上限内把这次调用钉住几分钟，重定向链最多再乘 6 ⇒ 模型选一个 URL
            # 就能占死整个 run。这里用工具自己声明的 meta.timeout（30s）作总预算，
            # 顺带让那个字段不再是装饰 —— 它过去只有路由里那份建议性清单读它。
            async with asyncio.timeout(self.meta.timeout), \
                    httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                direct_url = self._build_direct_url(parsed, ip_or_reason)
                status, location, html = await self._get_with_limit(
                    client, direct_url, headers, hostname,
                )
                current_url = url

                # 手动处理重定向（最多 _MAX_REDIRECTS 次），每次重新解析并校验目标 IP
                redirect_count = 0
                while (status in _REDIRECT_STATUSES and location
                       and redirect_count < _MAX_REDIRECTS):
                    redirect_count += 1
                    # 相对 Location 先按当前 URL 折成绝对地址，再校验 —— 否则校验的和
                    # 请求的不是同一个 URL（折叠后的 Host 才是真正要连的那一台）
                    next_url = _join_redirect(current_url, location)

                    safe, next_ip_or_reason = await self._check_url_safe(next_url)
                    if not safe:
                        return ToolResult(False, "", f"重定向被拦截: {next_ip_or_reason}")

                    next_parsed = urlparse(next_url)
                    current_url = next_url
                    headers["Host"] = next_parsed.hostname
                    direct_url = self._build_direct_url(next_parsed, next_ip_or_reason)
                    status, location, html = await self._get_with_limit(
                        client, direct_url, headers, next_parsed.hostname,
                    )

                if status == 413:
                    return ToolResult(
                        False, "", f"响应超过 {_MAX_RESPONSE_BYTES // (1024 * 1024)}MB 上限，拒绝抓取: {current_url}",
                    )
                if status == 415:
                    return ToolResult(
                        False, "", f"响应不是可解析的文本类型，放弃抓取: {current_url}")
                if status in _REDIRECT_STATUSES:
                    # 审查修复（P2）：重定向达到上限后 status 仍是 3xx，原实现直接落入
                    # 下方成功分支 —— 302 响应体通常是空的，Agent 拿到"成功"的空正文继续
                    # 推理。这里按原因分开报：跳数用完和「3xx 却不给 Location」是两回事，
                    # 后者的文案说「次数超过 5 次上限」会让人去查一个不存在的环。
                    if not location:
                        return ToolResult(
                            False, "", f"重定向响应没有 Location，放弃抓取: {current_url}")
                    return ToolResult(
                        False, "", f"重定向次数超过 {_MAX_REDIRECTS} 次上限，放弃抓取: {current_url}",
                    )
                if status >= 400:
                    return ToolResult(False, "", f"HTTP 错误 {status}")

                # 解析必须在总时限之内，而且不能占住事件循环。原实现把 trafilatura /
                # `_strip_html` 放在 `async with asyncio.timeout(...)` **之外**：
                # 一是完全脱窗（S6-08 那道 30s 总预算只管到网络），二是同步 CPU 直接跑在
                # 事件循环线程上 —— 解析期间本进程所有 run/SSE 一起停摆。放线程里只是
                # 归还事件循环（线程不可强杀），真正的兜底是「解析窗口 + 线性扫描 +
                # 内容类型」三道上限。
                text = await asyncio.to_thread(
                    _extract_body, html[:_PARSE_WINDOW_CHARS])

            # 截断
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n... [已截断，原始长度 {len(text)} 字符]"

            # 网页内容：常规优先级
            return ToolResult(True, text, priority="normal")

        except httpx.TimeoutException as e:
            logger.warning("Web fetch 超时: %s", url)
            return ToolResult(False, "", f"抓取超时: {e}")
        except httpx.ConnectError as e:
            logger.warning("Web fetch 连接失败: %s", url)
            return ToolResult(False, "", f"连接失败: {e}")
        except TimeoutError:
            # asyncio.timeout 到点抛的是内置 TimeoutError（3.11 起与
            # asyncio.TimeoutError 同一个类），与上面的 httpx 逐操作超时不是一回事
            logger.warning("Web fetch 超过总时限 %ss: %s", self.meta.timeout, url)
            return ToolResult(
                False, "",
                f"抓取超过总时限 {self.meta.timeout}s 已放弃（慢速或分块拖延的响应）: {url}",
            )
        except Exception as e:
            logger.exception("Web fetch 失败: %s", url)
            return ToolResult(False, "", f"抓取失败: {e}")

    async def _get_with_limit(
        self, client, direct_url: str, headers: dict, sni: str,
        cap: int = _MAX_RESPONSE_BYTES,
    ) -> tuple[int, str, str]:
        """流式 GET 并在响应体超过 cap 时中止（审查 M-4）。

        返回 (status_code, location, body_text)。
        - 重定向状态：location 为 Location 头，body 为空
        - 4xx/5xx：body 为空，调用方按 status 处理
        - 超限：返回 (413, "", "") 作为内部标记
        - 非文本 Content-Type：返回 (415, "", "") 作为内部标记（在下载之前判，
          二进制不必收满 10MB 再解码成同样尺寸的 Python 字符串）
        """
        async with client.stream(
            "GET", direct_url, headers=headers, extensions={"sni_hostname": sni},
        ) as resp:
            if 300 <= resp.status_code < 400:
                return resp.status_code, resp.headers.get("location", ""), ""
            if resp.status_code >= 400:
                return resp.status_code, "", ""
            # 内容类型判据（放在下载之前，二进制就没必要收满 10MB 了）
            if not _is_parsable_content(resp.headers.get("content-type", "")):
                return 415, "", ""
            # 先查 Content-Length（若服务器提供），避免白白下载超大响应
            clen = resp.headers.get("content-length")
            if clen:
                try:
                    if int(clen) > cap:
                        return 413, "", ""
                except ValueError:
                    pass
            chunks = []
            total = 0
            async for chunk in resp.aiter_bytes(65536):
                total += len(chunk)
                if total > cap:
                    return 413, "", ""
                chunks.append(chunk)
            return resp.status_code, "", b"".join(chunks).decode("utf-8", "replace")

    def _build_direct_url(self, parsed, ip: str) -> str:
        """用解析后的 IP 替换 URL 中的 hostname，保留端口和路径"""
        port = parsed.port
        if parsed.scheme == "https" and port == 443:
            port = None
        if parsed.scheme == "http" and port == 80:
            port = None
        netloc = ip if port is None else f"{ip}:{port}"
        # IPv6 地址在 URL 中需要括号
        if ":" in ip and not ip.startswith("["):
            netloc = f"[{ip}]" if port is None else f"[{ip}]:{port}"
        return parsed._replace(netloc=netloc).geturl()

    async def _check_url_safe(self, url: str) -> tuple[bool, str]:
        """校验 URL 解析后的 IP 是否安全，并返回解析到的 IP（防 SSRF / DNS rebinding）

        规则本体在模块级 `_resolve_public_destination`（搜索腿的重定向复核共用）。
        """
        safe, ip_or_reason, reason = await _resolve_public_destination(url)
        return (True, ip_or_reason) if safe else (False, reason)

