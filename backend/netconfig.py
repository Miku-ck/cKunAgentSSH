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
from typing import Optional, TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import httpx

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

# 下限 0 = 「不覆盖」哨兵（各调用点沿用自己的原默认超时），不是最小可用值。
# 此前这里写着 5，而两处夹紧都硬编码 0 作下限、面板注释又自称与本常量同口径 ——
# 一个没人读的常量只会诱导出下一次「按 5 修好」的改动，把那层哨兵语义弄丢。
TIMEOUT_MIN, TIMEOUT_MAX = 0, 600
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


# S10-03：URL 带凭据的不止代理 —— base_url 同样允许
# `https://user:token@ai-gateway.example/v1`（企业 OpenAI 兼容网关的常见文档形态，
# httpx/openai 都照此认证）。redact_proxy 只能处理「整串就是一个 URL」，而实际外泄
# 面是**自由文本**：httpx 的异常 str() 会把请求 URL 原样嵌进消息，上游 4xx 响应体
# 也常回显请求地址，两者都被 routes/settings.py 直接写进日志和返回给渲染层的
# detail。所以这里做的是文本级替换，只吃 userinfo 段、其余原样保留。
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)"
    r"(?P<user>[^\s/@:]*)(?::(?P<pass>[^\s/@]*))?@"
)


def redact_urls_in_text(text: Optional[str]) -> str:
    """把任意文本中内嵌 URL 的 userinfo 打成 `scheme://***@host`。

    只用于「日志 / 回传渲染端」出口，不改写真正要发出的请求。无凭据的文本原样返回，
    因此调用点可以无条件套上它，不必先判断这条 URL 是否带口令。
    """
    if not text:
        return ""

    def _sub(m: re.Match) -> str:
        if not m.group("user") and not m.group("pass"):
            # `https://@host` 这种空 userinfo 不必打码
            return m.group(0)
        return f"{m.group('scheme')}***@"

    return _URL_USERINFO_RE.sub(_sub, text)


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
    timeout = _env_int("CKUN_HTTP_TIMEOUT", 0, TIMEOUT_MIN, TIMEOUT_MAX)
    retries = _env_int("CKUN_HTTP_RETRIES", 1, RETRY_MIN, RETRY_MAX)
    proxy = os.environ.get("CKUN_HTTP_PROXY", "").strip()
    try:
        import database as db
        stored = db.get_http_policy()
        # 代理口径**与 timeout/retry 不同，且是刻意的**：库里非空值才压过 env，
        # 空行（面板点「清除代理」）与缺行一样轮到 env 兜底。
        # ⚠️ 这不是漏改：切片审查把这里判成 S0-01「清除是一次性操作，重启后 env
        # 复活」并要求裁决，而 round 19 登记的口径相反（「空串 = 清除该行、清完
        # 才轮到 env 兜底」），round 22 用两条测试把它钉住，本仓另一会话在同一轮
        # 里看过 S0-01 后明确保留旧口径。两种读法都自洽（env 到底是「运维默认」
        # 还是「已被用户否决」），所以留待产品裁决，不要在裁决前单方面改 ——
        # 我改过一次并撞掉那两条测试，回退了。
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
        "timeout_seconds": _clamp_int(timeout_seconds, 0, TIMEOUT_MIN, TIMEOUT_MAX),
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


def effective_timeout_bounds(default=None) -> tuple[float, float]:
    """这次请求**真正**会生效的 (connect, read) 上限 —— 供报错/诊断文案使用。

    S10-21：原先「连接超时 (Ns)」里的 N 取的是 `timeout_seconds(10.0)`，也就是用户
    设定的那个标量。但 http_timeout 把两个相位算成不同值（connect=min(10,t)、
    read=max(60,t)）：用户设 5s、端点连上却慢回时，界面上写着「连接超时 (5s)」，
    而真正到点的是 60s —— 一句比现状更误导的诊断，用户会按 5s 去查网络。
    上限规则归 netconfig 单管，调用点不许再自己抄公式（抄一份就等于给下一次调整
    留一个会静默失效的副本）。connect/read 分开返回，让调用方能按异常类型说准
    到底是「连不上」还是「连上了没回应」。
    """
    to = http_timeout(default)
    if to is None:
        return 0.0, 0.0
    read = getattr(to, "read", None)
    if read is None:
        # 调用点传的是标量默认（用户没设超时）→ 各相位同为该值
        return float(to), float(to)
    return float(to.connect), float(read)


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
    确实需要跟随的调用点自己逐跳校验目标地址（web_search 的两条 HTML 腿见
    web_tools._read_search_html）—— 别在这里打开开关：跟随一旦交给 httpx 内部，
    复核就只能发生在「已经连上 Location 指的那个地址之后」。
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
