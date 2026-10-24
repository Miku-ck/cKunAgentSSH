"""自由文本凭据脱敏 — 日志出口与审计落库共用的单一实现。

为什么放独立模块：netconfig.redact_urls_in_text 只管 URL userinfo 一种形态；
日志与审计要防的是「凭据以任意形态混进自由文本」—— key=value、Bearer 头、
sk- 形态 API key、mysql/sshpass 的 -p 内联口令。两类出口共用一份模式库，
避免「日志脱了、审计没脱」的口径漂移。

精度取舍：只收录高置信模式。误报的代价（排障时看到 ***）远小于漏报的代价
（口令进日志/审计库），但大面积误报会让人把整个脱敏器当噪音绕掉 —— 所以
`-p` 内联口令只在 mysql/mariadb/mysqldump/sshpass 上下文里生效（裸匹配会
误吃 `tar -pxzf`）；键值对只认凭据语义的键名（prompt_tokens/max_tokens 这类
带下划线的复合词天然不命中 \\btoken\\b）。掩码统一用 `***`，与
netconfig.redact_urls_in_text 的 userinfo 掩码同款，见到 *** 即知被脱敏。
"""
from __future__ import annotations

import re

from netconfig import redact_urls_in_text

# key=value / key: value。键白名单只收凭据语义的词；
# 引号包裹的值吃整段，裸值吃到第一个空白/引号/常见分隔符。
_KEY_VALUE_RE = re.compile(
    r"(?P<key>\b(?:password|passwd|pwd|pgpassword|secret|token|"
    r"api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret)"
    r"\b)\s*[:=]\s*"
    r"(?P<val>\"[^\"]{0,256}\"|'[^']{0,256}'|[^\s;,|&\"'>)]{1,256})",
    re.IGNORECASE,
)

# Authorization: Bearer <credentials>
_BEARER_RE = re.compile(
    r"(?P<pre>\bBearer\s+)(?P<val>[A-Za-z0-9._\-+/=~]{8,})",
    re.IGNORECASE,
)

# OpenAI 风格 API key（sk- 前缀 + 足够长的 key 体）
_SK_KEY_RE = re.compile(r"\bsk-(?P<val>[A-Za-z0-9_\-]{16,})")

# mysql/sshpass 的 -p 内联口令：-p 后紧跟非空白串（无空格形态）。
# 预检工具名是为了精度：裸 `-p[A-Za-z0-9]+` 会误吃 `tar -pxzf`。
# 命令体内最多向前看 80 字符，且不得跨 ; | & 分隔符（那是另一条命令）；
# 中间段不得含分隔符本身就保证了「-p 属于这个工具的这条命令」。
_INLINE_P_FLAG_RE = re.compile(
    r"(?P<pre>\b(?:mysql|mariadb|mysqldump|mysqladmin|mysqlimport|sshpass)\b"
    r"[^|;&\n]{0,80}?\s)-p(?P<val>[^\s\"']{3,})",
)


def redact_secrets(text: str | None) -> str:
    """把自由文本中的高置信凭据形态替换为 ***，无凭据时原样返回。

    调用点可以无条件套上它（同 netconfig.redact_urls_in_text 的约定），
    不必先判断文本里是否真的有口令。None 按 "" 处理，与 netconfig 同口径。
    """
    if not text:
        return ""
    # URL userinfo 先走：先把 user:pass@ 收敛掉，后续模式不会再碰到这段
    text = redact_urls_in_text(text)
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group('key')}=***", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group('pre')}***", text)
    text = _SK_KEY_RE.sub("sk-***", text)
    text = _INLINE_P_FLAG_RE.sub(lambda m: f"{m.group('pre')}-p***", text)
    return text
