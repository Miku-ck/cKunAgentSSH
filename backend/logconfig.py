"""后端日志级别（G9）。

为什么默认 warning 而不是 info：uvicorn 只配它自己的 'uvicorn*' logger，本项目的
模块 logger（`logging.getLogger(__name__)`）一路冒泡到 root，而 root 在此之前
**没有 handler** —— 由 `logging.lastResort` 兜底，只放 WARNING 及以上。也就是说
今天所有 `logger.info(...)` 根本没落地。装上 handler 后把默认级别定成 warning，
可观测输出与之前一致（只是多了时间戳），用户显式选 info/debug 才开始变多。
"""
import logging
import sys
from typing import Iterable, Optional

LEVELS: tuple[str, ...] = ("debug", "info", "warning", "error", "critical")
DEFAULT_LEVEL = "warning"

_current = DEFAULT_LEVEL
_handler: Optional[logging.StreamHandler] = None


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


def effective_levels() -> Iterable[str]:
    return LEVELS
