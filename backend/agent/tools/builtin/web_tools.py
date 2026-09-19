"""
联网搜索工具 — Work / IDE 模式

  - WebSearchTool: 调用搜索引擎 API（Tavily / 360 / DuckDuckGo），返回结构化结果
  - WebFetchTool:  抓取指定 URL 内容，转为文本
"""
import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from agent.tools import BaseTool, ToolResult, ToolMeta

logger = logging.getLogger(__name__)

# 默认搜索结果数量
_DEFAULT_MAX_RESULTS = 5

# web_fetch 响应体上限（审查 M-4）：超过即拒绝，防止恶意页面打爆后端内存
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# C-review MINOR-10：max_chars 上限 — 原实现无上限，LLM 可传 10^9，
# 10MB 响应全量进上下文（约 2.5M token，必然超窗口且烧钱）
_MAX_FETCH_CHARS = 100_000


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
        # 参数校验：max_results 必须是 1~20 的整数
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            return ToolResult(False, "", "max_results 必须是整数")
        if max_results < 1:
            max_results = 1
        elif max_results > 20:
            max_results = 20

        # 尝试 Tavily API（用户配置 key 时优先）
        tavily_key = self._get_tavily_key()
        if tavily_key:
            result = await self._search_tavily(query, max_results, tavily_key)
            if result.success:
                return result
            logger.warning("Tavily 搜索失败，降级到 360: %s", result.error)

        # 降级 1：360 搜索 HTML（国内可直接访问，无需 API key）
        result = await self._search_360(query, max_results)
        if result.success:
            return result

        # 降级 2：DuckDuckGo 免费 HTML 搜索
        return await self._search_duckduckgo(query, max_results)

    def _get_tavily_key(self) -> str:
        """从环境变量或数据库获取 Tavily API Key"""
        import os
        return os.environ.get("TAVILY_API_KEY", "")

    async def _search_tavily(self, query: str, max_results: int, api_key: str) -> ToolResult:
        """调用 Tavily Search API"""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": api_key,
                        "query": query,
                        "max_results": max_results,
                        "include_answer": True,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            # 格式化输出
            parts = []
            if data.get("answer"):
                parts.append(f"摘要: {data['answer']}\n")

            for i, result in enumerate(data.get("results", []), 1):
                parts.append(
                    f"{i}. {result.get('title', '无标题')}\n"
                    f"   URL: {result.get('url', '')}\n"
                    f"   {result.get('content', '')[:300]}"
                )

            # 搜索结果：常规优先级
            return ToolResult(True, "\n\n".join(parts) if parts else "无搜索结果", priority="normal")

        except Exception as e:
            logger.exception("Tavily 搜索失败")
            return ToolResult(False, "", f"搜索失败: {e}")

    async def _search_360(self, query: str, max_results: int) -> ToolResult:
        """360 搜索 HTML（无需 API key，国内可直接访问）"""
        import httpx
        from urllib.parse import quote_plus

        try:
            search_url = f"https://www.so.com/s?q={quote_plus(query)}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.0"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }

            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(search_url, headers=headers)
                resp.raise_for_status()
                html = resp.text

            results = self._parse_360_html(html, max_results)
            if not results:
                return ToolResult(True, "无搜索结果", priority="normal")

            parts = []
            for i, result in enumerate(results, 1):
                parts.append(
                    f"{i}. {result.get('title', '无标题')}\n"
                    f"   URL: {result.get('url', '')}\n"
                    f"   {result.get('snippet', '')[:300]}"
                )
            return ToolResult(True, "\n\n".join(parts), priority="normal")

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
        import httpx
        from urllib.parse import quote_plus

        try:
            search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.0"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }

            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(search_url, headers=headers)
                resp.raise_for_status()
                html = resp.text

            results = self._parse_duckduckgo_html(html, max_results)
            if not results:
                return ToolResult(True, "无搜索结果", priority="normal")

            parts = []
            for i, result in enumerate(results, 1):
                parts.append(
                    f"{i}. {result.get('title', '无标题')}\n"
                    f"   URL: {result.get('url', '')}\n"
                    f"   {result.get('snippet', '')[:300]}"
                )
            return ToolResult(True, "\n\n".join(parts), priority="normal")

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


def _strip_html(html: str) -> str:
    """简易 HTML 标签去除（无 trafilatura 时的降级方案）"""
    import html as html_module
    import re

    # 去 script/style
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # 去标签
    text = re.sub(r"<[^>]+>", "", html)
    # 解码 HTML 实体（&amp; &lt; &gt; &nbsp; &#39; &quot; 等）
    text = html_module.unescape(text)
    # 压缩空白
    text = re.sub(r"\s+", " ", text).strip()
    return text


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

        # 参数校验：max_chars 必须是正整数
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            return ToolResult(False, "", "max_chars 必须是整数")
        if max_chars < 1:
            max_chars = 1
        # C-review MINOR-10：钳制上限（防止整页 10MB 灌入上下文）
        if max_chars > _MAX_FETCH_CHARS:
            max_chars = _MAX_FETCH_CHARS

        # 解析 URL
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult(False, "", f"不允许的协议: {parsed.scheme}")
        hostname = parsed.hostname
        if not hostname:
            return ToolResult(False, "", "无效的 URL")

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
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                direct_url = self._build_direct_url(parsed, ip_or_reason)
                status, location, html = await self._get_with_limit(
                    client, direct_url, headers, hostname,
                )
                current_url = url

                # 手动处理重定向（最多 5 次），每次重新解析并校验目标 IP
                redirect_count = 0
                while status in (301, 302, 303, 307, 308) and redirect_count < 5:
                    redirect_count += 1
                    next_url = location or ""
                    if not next_url:
                        break
                    # 处理相对路径重定向
                    if not next_url.startswith("http"):
                        next_url = httpx.URL(current_url).join(next_url).str()

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
                # 审查修复（P2）：重定向达到 5 次上限后 status 仍是 3xx，
                # 原实现直接落入下方成功分支 — 302 响应体通常是空的，
                # Agent 拿到"成功"的空正文继续推理
                if status in (301, 302, 303, 307, 308):
                    return ToolResult(
                        False, "", f"重定向次数超过 5 次上限，放弃抓取: {current_url}",
                    )
                if status >= 400:
                    return ToolResult(False, "", f"HTTP 错误 {status}")

            # 尝试用 trafilatura 提取正文（可选依赖）
            try:
                import trafilatura
                text = trafilatura.extract(html) or _strip_html(html)
            except ImportError:
                text = _strip_html(html)

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
        """
        async with client.stream(
            "GET", direct_url, headers=headers, extensions={"sni_hostname": sni},
        ) as resp:
            if 300 <= resp.status_code < 400:
                return resp.status_code, resp.headers.get("location", ""), ""
            if resp.status_code >= 400:
                return resp.status_code, "", ""
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

        E 修复：socket.gethostbyname 是同步 DNS 查询，放线程池避免卡事件循环
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"不允许的协议: {parsed.scheme}"
        hostname = parsed.hostname
        if not hostname:
            return False, "无效的 URL"
        try:
            ip = await asyncio.to_thread(socket.gethostbyname, hostname)
        except socket.gaierror:
            return False, f"DNS 解析失败: {hostname}"
        safe, reason = _is_safe_ip(ip)
        if not safe:
            return False, reason
        return True, ip

