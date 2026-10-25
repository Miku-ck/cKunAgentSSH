"""自由文本凭据脱敏 — 日志出口与审计落库共用的单一实现。

为什么放独立模块：netconfig.redact_urls_in_text 只管 URL userinfo 一种形态；
日志与审计要防的是「凭据以任意形态混进自由文本」—— key=value、Bearer 头、
sk- 形态 API key、mysql/sshpass 的 -p 内联口令。两类出口共用一份模式库，
避免「日志脱了、审计没脱」的口径漂移。

精度取舍：只收录高置信模式。误报的代价（排障时看到 ***）远小于漏报的代价
（口令进日志/审计库），但大面积误报会让人把整个脱敏器当噪音绕掉 —— 所以
`-p` 内联口令只在 mysql/mariadb/mysqldump/sshpass 上下文里生效（裸匹配会
误吃 `tar -pxzf`）；redis-cli 的 `-a`、curl 的 `-u` 同理锚在工具名上；裸的
`--password` 长选项名本身自带凭据语义，不限工具但排除 --ask* 提示型 flag；
键值对只认凭据语义的键名（prompt_tokens/max_tokens 这类带下划线的复合词
天然不命中 \\btoken\\b）。掩码统一用 `***`，与
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

# Authorization: Basic 的两种形态：base64 编码串（dXNlcjpwYXNzMTIz）与
# 解码后的 user:pass 明文。锚在 Authorization/Proxy-Authorization 头名上保精度
# （裸 "Basic <词>" 会误吃英文句子）；base64 与 user:pass 按「含不含冒号」天然
# 分流，user:pass 形态放前，避免 base64 分支把 "administrator:pass" 吃成半截。
_BASIC_AUTH_RE = re.compile(
    r"(?P<pre>\b(?:Proxy-)?Authorization\s*:\s*Basic\s+)"
    r"(?P<val>[^\s:;|&\"']{0,256}:[^\s;|&\"']{1,256}|[A-Za-z0-9+/=]{6,})",
    re.IGNORECASE,
)

# 工具内联口令的通用骨架：先匹配到「工具名 + 同一条命令体内的 flag」，值支持
# 粘连（-aS3cret）、空格（-a S3cret）、等号（--pass=S3cret）三种形态。
# 值不得以 - 开头：防止把下一个选项（redis-cli -a --json、psql --password -c）
# 当口令吃掉；引号包裹的整段值也吃。
_INLINE_TOOL_PWD_TMPL = (
    r"(?P<pre>\b{tool}\b[^|;&\n]{{0,80}}?\s)"
    r"(?P<flag>{flags})"
    r"(?P<sep>\s*=?\s*)"
    r"(?P<val>\"[^\"]{{0,256}}\"|'[^']{{0,256}}'|(?![\s=-])[^\s\"'|;&]{{3,256}})"
)

# redis-cli 的口令 flag：-a（粘连/空格）、--pass/--password。裸 `-a` 会误吃
# grep -a / ls -la 这类无凭据语义的 flag，必须锚在 redis-cli 上下文里；
# -a 不加 \b 是为了吃粘连形态 -aS3cret。
_REDIS_PWD_RE = re.compile(
    _INLINE_TOOL_PWD_TMPL.format(tool="redis-cli", flags=r"-a|--pass(?:word)?\b"),
    re.IGNORECASE,
)

# curl 的 -u/--user user:pass。curl 语义上 -u 后面那一个参数就是凭据本体，
# 无论带不带冒号都按口令处理（残缺命令更该掩）；组合短选项里 u 必须在末位
# 才会吃值（-su admin:pass），故只收 `-u` 与「-若干短flag+u 结尾」。
_CURL_USER_RE = re.compile(
    _INLINE_TOOL_PWD_TMPL.format(
        tool="curl", flags=r"--user\b|--proxy-user\b|-u|-[a-z]+u|-U|-[a-z]+U"),
    re.IGNORECASE,
)

# docker login 的 -p/--password：docker 的 `-p` 平时是端口映射（docker run
# -p 8080:80），但 login 子命令里就是口令，且是极常见内联形态，单独锚
# 「docker + login」子命令。
_DOCKER_LOGIN_PWD_RE = re.compile(
    _INLINE_TOOL_PWD_TMPL.format(
        tool=r"docker\s+login", flags=r"-p\b|--password\b"),
    re.IGNORECASE,
)

# 任意工具的 --password/--passwd/--pass 长选项（含 --http-password 这类带前缀
# 的变体）。选项名本身就是凭据语义的锚，不再限定工具白名单；--askpass /
# --ask-password 是「提示输入」而非「传入口令」，显式排除防止把后面的
# 位置参数（如 wget --ask-password <url> 的 URL）当口令吃掉。
# pre 是可选的工具名前缀（仅用于替换时原样保留）。
_LONG_PWD_FLAG_RE = re.compile(
    r"(?P<pre>\b[A-Za-z0-9_.@/-]{1,64}[^|;&\n]{0,80}?\s)?"
    r"(?P<flag>--(?!ask-?p)(?:[a-z0-9_-]{0,16}-)?(?:password|passwd|pass)\b)"
    r"(?P<sep>\s*=?\s*)"
    r"(?P<val>\"[^\"]{0,256}\"|'[^']{0,256}'|(?![\s=-])[^\s\"'|;&]{3,256})",
    re.IGNORECASE,
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
    text = _BASIC_AUTH_RE.sub(lambda m: f"{m.group('pre')}***", text)
    text = _SK_KEY_RE.sub("sk-***", text)
    text = _INLINE_P_FLAG_RE.sub(lambda m: f"{m.group('pre')}-p***", text)
    for pattern in (_REDIS_PWD_RE, _CURL_USER_RE, _DOCKER_LOGIN_PWD_RE,
                    _LONG_PWD_FLAG_RE):
        text = pattern.sub(
            lambda m: f"{m.group('pre') or ''}{m.group('flag')}"
            f"{m.group('sep')}***",
            text,
        )
    return text
