"""web_fetch 的正文解析必须有上限：它是模型选的 URL，而且跑在事件循环上。

三处实测（均为修复前）：

1. `_strip_html` 去 script/style 用的是 `<(script|style)[^>]*>.*?</\\1>` + DOTALL。
   「有开标签、没有闭标签」时非贪婪组会从该位置一路扩到全文末尾才发现匹配失败 ⇒
   每个未闭合标签一次 O(len)。4MB 正文实测：

       未闭合 <script> 数 =     0 → 0.04s
                          =    50 → 1.20s
                          =   200 → 6.27s
                          =   800 → 28.66s

   标签个数由 URL 说了算（一个开标签 8 字节，10MB 上限内能塞几十万个）。更要命的是这段
   同步 CPU 原本写在 `async with asyncio.timeout(...)` **之外**：既完全脱窗，又直接跑在
   事件循环线程上 ⇒ 解析期间本进程所有 run/SSE 一起停摆。`web_fetch` 的
   `risk_level="safe"` 且在 `PLAN_READ_ONLY_TOOLS` 白名单里，这条路径不需要用户审批就能
   被模型走到。
2. `_get_with_limit` 没有内容类型判据：任何类型的 200 都收满 10MB 再
   `decode("utf-8", "replace")` ⇒ 10MB 二进制解码后仍占约 60MB 峰值，然后交给正则。
3. 解析吃的是全量正文，而输出本来就被 `max_chars`（上限 100k）截掉。

修法对应三道上限：线性扫描（未闭合时按 HTML 语义整段丢弃，也是浏览器看到的结果）、
文本型 Content-Type 才解析（缺失仍放行，大量静态站不给这个头）、解析窗口 1M 字符，
并把解析放进总时限内 + 挪到线程里（线程不可强杀，所以窗口才是真兜底）。

运行: python -m pytest backend/tests/test_web_fetch_body_parse_bounds.py -v
"""
import time
from pathlib import Path

import pytest

from agent.tools.builtin import web_tools
from agent.tools.builtin.web_tools import (
    _PARSE_WINDOW_CHARS,
    _extract_body,
    _is_parsable_content,
    _strip_html,
)

_BODY = "x" * (4 * 1024 * 1024)


@pytest.mark.parametrize("tags", [50, 800, 8000])
def test_未闭合标签不再放大解析耗时(tags):
    """成本必须随正文长度线性，而不是随「未闭合标签数 × 长度」。"""
    html = f"<p>{_BODY[:1000]}</p>" + "<script>" * tags + _BODY
    t0 = time.perf_counter()
    _strip_html(html)
    dt = time.perf_counter() - t0
    assert dt < 1.5, f"{tags} 个未闭合 <script> 让解析花了 {dt:.2f}s（修复前 800 个就 28.66s）"


def test_闭合的脚本样式仍被整段去掉():
    assert _strip_html("<p>a</p><script>var x=1;</script><p>b</p>") == "ab"
    assert _strip_html("<style type='text/css'>p{color:red}</style><p>hi</p>") == "hi"


def test_未闭合标签不把脚本源码当正文():
    """原正则在这种情况下匹配失败 ⇒ 整段原文留给模型；HTML/浏览器语义是「都在标签里」。"""
    assert "alert" not in _strip_html("<p>keep</p><script>alert(document.cookie)")
    assert "color" not in _strip_html("<p>keep</p><style>p{color:red}")


def test_相似标签名不被误认():
    """`<scripting>` 不是 `<script>`，边界判断不能只按前缀。"""
    out = _strip_html("<p>x</p><scripting>y</scripting>")
    assert "x" in out and "y" in out
    assert _strip_html("<div><scripted>a</scripted></div>") == "a"


def test_实体解码与空白压缩未回退():
    assert _strip_html("<p>a&amp;b&nbsp;<script>s</script>c</p>") == "a&b c"


@pytest.mark.parametrize("ctype,ok", [
    ("text/html; charset=utf-8", True),
    ("text/plain", True),
    ("application/json", True),
    ("application/problem+json", True),
    ("application/rss+xml", True),
    ("application/xhtml+xml", True),
    ("", True),                        # 没有这个头 ⇒ 放行（长度与窗口仍受约束）
    ("image/png", False),
    ("application/pdf", False),
    ("application/octet-stream", False),
    ("video/mp4", False),
])
def test_只解析文本型响应(ctype, ok):
    assert _is_parsable_content(ctype) is ok, ctype


def test_解析窗口不超过网络上限():
    assert 0 < _PARSE_WINDOW_CHARS <= web_tools._MAX_RESPONSE_BYTES


def test_解析落在总时限之内():
    """`asyncio.timeout` 管不到它之后的同步 CPU —— 位置本身就是契约。"""
    src = Path(web_tools.__file__).read_text(encoding="utf-8")
    deadline = src.index("async with asyncio.timeout")
    parse = src.index("_extract_body,")
    handler = src.index("except httpx.TimeoutException")
    assert deadline < parse < handler, (
        "正文解析又跑到总时限之外了：解析期间没有任何窗口在管它")
    assert "await asyncio.to_thread(" in src[deadline:parse], (
        "解析必须离开事件循环线程，否则所有 run/SSE 一起停摆")


def test_extract_body_仍走_trafilatura_降级链():
    """换函数不能把「有 trafilatura 用它、没有就正则」这层语义弄丢。"""
    html = "<html><head><style>p{color:red}</style></head><body><p>正文 here</p></body></html>"
    text = _extract_body(html)
    assert "正文 here" in text
    assert "color:red" not in text
