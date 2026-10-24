"""后端日志级别（G9）。

为什么默认 warning 而不是 info：uvicorn 只配它自己的 'uvicorn*' logger，本项目的
模块 logger（`logging.getLogger(__name__)`）一路冒泡到 root，而 root 在此之前
**没有 handler** —— 由 `logging.lastResort` 兜底，只放 WARNING 及以上。也就是说
今天所有 `logger.info(...)` 根本没落地。装上 handler 后把默认级别定成 warning，
可观测输出与之前一致（只是多了时间戳），用户显式选 info/debug 才开始变多。
"""
import logging
import sys
from typing import Optional

from secret_redact import redact_secrets

LEVELS: tuple[str, ...] = ("debug", "info", "warning", "error", "critical")
DEFAULT_LEVEL = "warning"

_current = DEFAULT_LEVEL
_handler: Optional[logging.StreamHandler] = None


class SecretRedactionFilter(logging.Filter):
    """出口脱敏：所有经 root handler 的日志文本先过 secret_redact。

    只改写 message 文本（含 %s 参数格式化后的结果）；exc_info 回溯栈不经
    这里 —— 回溯里几乎不嵌口令，且改写回溯会破坏排障，不做。脱敏自身异常
    不得让日志丢失，吞掉按原文放行。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            masked = redact_secrets(msg)
            if masked != msg:
                record.msg = masked
                record.args = ()
        except Exception:  # noqa: BLE001, S110 — 脱敏失败不得让日志本身丢失
            pass
        return True


def normalize_level(raw, default: str = DEFAULT_LEVEL) -> str:
    value = str(raw or "").strip().lower()
    return value if value in LEVELS else default


def apply_level(level, *, persist_note: bool = True) -> str:
    """安装 root handler（一次）并设定级别；返回生效级别。

    只改 root 与本项目自有 logger 的级别，不动 uvicorn.error/uvicorn.access：
    那两个由 uvicorn.Config(log_level=...) 管，访问日志级别不该被这个开关改掉。
    """
    global _current, _handler
    resolved = normalize_level(level)
    root = logging.getLogger()
    if _handler is None:
        _handler = logging.StreamHandler(sys.stderr)
        _handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(_handler)
    # 出口脱敏挂在 handler 上（幂等）：uvicorn 自有 handler 不经此过滤器，
    # 那是访问日志，不含业务凭据。
    if not any(isinstance(f, SecretRedactionFilter) for f in _handler.filters):
        _handler.addFilter(SecretRedactionFilter())
    root.setLevel(getattr(logging, resolved.upper(), logging.WARNING))
    _current = resolved
    if persist_note:
        logging.getLogger(__name__).debug("日志级别设为 %s", resolved)
    return resolved


def get_level() -> str:
    return _current


def load_from_db() -> str:
    """启动回填：读 app_settings('log.level')，脏值/缺行回默认级别。"""
    try:
        import database as db
        raw = db.get_app_setting("log.level")
    except Exception as e:
        logging.getLogger(__name__).warning("日志级别读取失败，沿用 %s: %s", _current, e)
        return _current
    return apply_level(raw if raw is not None else DEFAULT_LEVEL, persist_note=False)
