"""出站 HTTP 策略（G4 代理 / G5 超时与重试）— 全后端唯一读取点。

设计约束：
- 策略落在 app_settings，进程内缓存一份模块值，PUT 时同步推送（与 E3 的
  allowlist TTL 同一个范式）。每次请求都查库没必要，缓存不同步才有害。
- 默认值必须等于引入本设置之前的行为：超时留 None（各调用点用自己的旧硬编码），
  重试次数 1（openai SDK 本来就是 max_retries=1），代理空（直连）。
  默认值一旦选成"30 秒 + 3 次重试"，升级当天所有慢端点都会开始超时失败。
- 代理 URL 可以带 userinfo（http://user:pass@host:port 是常见形态），
  所以任何日志/诊断输出必须走 redact_proxy()，绝不回显原始值。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def _httpx():
    """惰性导入 httpx（启动性能优化，与 llm_adapter._openai 同源）。

    httpx 及其 h11/certifi/ssl 依赖链实测 ~72ms，只在真正发出站请求时
    才需要构造客户端/超时对象；放在模块顶层会把这段开销压进启动关键路径
    （main/routes/database 都 import 本模块）。类型注解一律靠
    __future__.annotations 延迟求值，运行时不再触碰 httpx 名称。
    """
    import httpx
    return httpx

TIMEOUT_MIN, TIMEOUT_MAX = 5, 600
RETRY_MIN, RETRY_MAX = 0, 3
_ALLOWED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")
# 只到 scheme + host[:port] 为止；userinfo 一律不取
_PROXY_RE = re.compile(r"^(?P<scheme>[a-zA-Z][\w+\-.]*)://(?P<rest>.+)$")

_policy: dict = {"proxy_url": "", "timeout_seconds": 0, "retry_count": 1}
_loaded = False


def _env_int(name: str, default: int, low: int, high: int) -> int:
    """带区间的 env 读取，口径与 db._int_app_setting 一致：非法/越界回默认，
    **不钳到边界** —— env 里写 99 不代表运维想要 3 次重试，回默认才是诚实读数。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < low or value > high:
        logger.warning("环境变量 %s=%s 超出 %d..%d，按默认 %d 处理", name, raw, low, high, default)
        return default
    return value


def normalize_proxy_url(raw: Optional[str]) -> str:
    """校验并归一化代理地址；非法则抛 ValueError（路由层转 422）。"""
    value = (raw or "").strip()
    if not value:
        return ""
    m = _PROXY_RE.match(value)
    if not m:
        raise ValueError("代理地址需形如 http://host:port 或 socks5://host:port")
    scheme = m.group("scheme").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"不支持的代理协议 {scheme}（可用：{', '.join(_ALLOWED_SCHEMES)}）")
    # netloc 为空 = "http:///x" 这种缺主机写法；urlsplit 同时把 userinfo 与 host 分清
    if not urlsplit(value).netloc:
        raise ValueError("代理地址缺少主机名")
    return f"{scheme}://{m.group('rest').strip()}"


def redact_proxy(url: Optional[str]) -> str:
    """去掉 userinfo，只保留 scheme://host:port —— 诊断面板/日志用这个。"""
    if not url:
        return ""
    sp = urlsplit(url)
    host = sp.hostname or ""
    if not host:
        return ""
    if sp.port:
        host = f"{host}:{sp.port}"
    return f"{(sp.scheme or '').lower()}://{host}"


def _clamp_int(raw, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def load_policy_from_db() -> dict:
    """从 app_settings 读取策略并缓存；数据库不可读时退回 env/内置默认。

    区间夹紧在 db.get_http_policy()（与 memory.max_items / allowlist TTL 同一个
    读取范式）：这里只负责"读不到时怎么办"，以及 env 与库里的行谁优先。

    优先级口径（与 main.py 对 approval_fallback_s 的启动回填同款）：
    **库里没有这一行 → 用 env 注入；用户写过 → 以 DB 为准**，包括显式写的 0
    （重试 0 = 不重试、超时 0 = 各调用点沿用原默认）。此前这里是无条件覆盖，
    运维注入的 CKUN_HTTP_RETRIES 实际上永远读不到，等于死代码。
    """
    global _policy, _loaded
    _loaded = True
    timeout = _env_int("CKUN_HTTP_TIMEOUT", 0, 0, TIMEOUT_MAX)
    retries = _env_int("CKUN_HTTP_RETRIES", 1, RETRY_MIN, RETRY_MAX)
    proxy = os.environ.get("CKUN_HTTP_PROXY", "").strip()
    try:
        import database as db
        stored = db.get_http_policy()
        # 代理口径本轮未变（旧代码在 try 之后有等价的 if not proxy 兜底）：库里
        # 非空值才压过 env，空行/缺行时 env 说话。
        if stored["proxy_url"]:
            proxy = stored["proxy_url"]
        # 缺行才让 env 说话；有行（含显式 0）就是用户的权威值
        if stored["timeout_set"]:
            timeout = stored["timeout_seconds"]
        if stored["retry_set"]:
            retries = stored["retry_count"]
    except Exception as e:
        logger.warning("HTTP 策略读取失败，沿用直连默认: %s", e)
    # 区间夹紧只用于 PUT 写路径；这里的 env 值已由 _env_int 校验，DB 值已由
    # db.is_int_app_setting_set 校验，越界一律回默认而不是钳到边界。
    try:
        proxy = normalize_proxy_url(proxy)
    except ValueError:
        logger.warning("非法的 http.proxy_url，按直连处理")
        proxy = ""
    _policy = {"proxy_url": proxy, "timeout_seconds": timeout, "retry_count": retries}
    return dict(_policy)


def get_policy() -> dict:
    if not _loaded:
        load_policy_from_db()
    return dict(_policy)


def set_policy(proxy_url: str, timeout_seconds: int, retry_count: int) -> dict:
    """PUT 路径写入：校验 + 夹紧 + 立刻生效（缓存替换，不重启）。"""
    global _policy, _loaded
    proxy = normalize_proxy_url(proxy_url)
    _policy = {
        "proxy_url": proxy,
        "timeout_seconds": _clamp_int(timeout_seconds, 0, 0, TIMEOUT_MAX),
        "retry_count": _clamp_int(retry_count, 1, RETRY_MIN, RETRY_MAX),
    }
    _loaded = True
    return dict(_policy)


def proxy_url() -> str:
    return get_policy()["proxy_url"] or None


def retry_count() -> int:
    return int(get_policy()["retry_count"])


def timeout_seconds(fallback: Optional[float] = None) -> Optional[float]:
    """用户设了超时就用它，否则用调用点原本的硬编码（= 引入本设置前的行为）。"""
    t = int(get_policy()["timeout_seconds"])
    return float(t) if t > 0 else fallback


def http_timeout(default=None):
    """用户设了超时就用它，否则原样返回调用点自己的默认值。

    LLM 路径要分开配置 connect/read：read 必须远大于一次生成的间隔，给一个标量
    会把 read 也压到同一个值、长回答被中途掐断（旧代码固定 read=300s 正是为此）。
    所以用户值只覆盖「设定值 + 60s 下限」的 read。
    """
    t = int(get_policy()["timeout_seconds"])
    if t <= 0:
        return default
    return _httpx().Timeout(connect=min(10.0, float(t)), read=max(60.0, float(t)),
                            write=float(t), pool=float(t))


# openai SDK 的流式默认（引入 G5 之前 llm_adapter 用的就是这组值）。
# 经模块级 __getattr__（PEP 562）惰性构造并缓存：httpx.Timeout 是导入期求值
# 的表达式，直写会把整个 httpx 链拉回启动关键路径。
_LLM_TIMEOUT_SPEC = dict(connect=10.0, read=300.0, write=10.0, pool=10.0)


def __getattr__(name: str):
    if name == "LLM_TIMEOUT_DEFAULT":
        value = _httpx().Timeout(**_LLM_TIMEOUT_SPEC)
        globals()["LLM_TIMEOUT_DEFAULT"] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def proxy_http_client(**kwargs) -> httpx.AsyncClient:
    """只带代理的 AsyncClient，供把超时作为 SDK 参数传的场合（openai SDK 会按
    每请求覆盖 httpx 客户端默认超时，所以超时必须由调用方另外传）。"""
    proxy = proxy_url()
    if proxy:
        kwargs["proxy"] = proxy
    return _httpx().AsyncClient(**kwargs)


def new_client(default_timeout: Optional[float] = None, **kwargs) -> httpx.AsyncClient:
    """统一出口的 AsyncClient：代理 + 超时（用户值优先于调用点默认）。

    follow_redirects 刻意沿用 httpx 默认（False）：调用点原来没开重定向的，
    接上这里也不该突然开始跟随（web_fetch 的 SSRF 守卫就依赖不跟随）。
    """
    to = http_timeout(default_timeout)
    if to is not None:
        kwargs["timeout"] = to
    proxy = proxy_url()
    if proxy:
        kwargs["proxy"] = proxy
    return _httpx().AsyncClient(**kwargs)


def new_sync_client(default_timeout: Optional[float] = None, **kwargs) -> httpx.Client:
    to = http_timeout(default_timeout)
    if to is not None:
        kwargs["timeout"] = to
    proxy = proxy_url()
    if proxy:
        kwargs["proxy"] = proxy
    return _httpx().Client(**kwargs)
