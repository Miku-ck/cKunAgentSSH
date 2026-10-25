"""后端日志级别（G9）。

为什么默认 warning 而不是 info：uvicorn 只配它自己的 'uvicorn*' logger，本项目的
模块 logger（`logging.getLogger(__name__)`）一路冒泡到 root。root 的 handler 由
main.py 入口最早经 install_early() 安装（S-B1①：在那之前 lastResort 只放
WARNING+ 且裸打无格式）—— 用户显式选 info/debug 才开始变多，运行期默认可观测
输出与装 handler 之前一致（只是多了时间戳与级别）。
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


def _install_handler_locked() -> None:
    """装 root handler + 出口脱敏（幂等），apply_level / install_early 共用。"""
    global _handler
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


def install_early() -> str:
    """进程入口最早安装 root handler（S-B1①，main.py 模块级调用）。

    目标：token 警告、启动横幅这类「handler 安装之前」发生的日志也走统一的
    asctime/level/name 格式与出口脱敏 —— 之前它们要么经 lastResort 裸打
    （无时间戳无级别），要么因 root 默认 WARNING 把 INFO 丢成死日志。

    启动窗口先用 info 级：启动横幅与初始化进度（logger.info）必须落地。
    用户配置级别由 lifespan 的 load_from_db 回填接管（缺行/脏值回 DEFAULT
    warning）—— 所以 info 只在启动窗口多出来，运行期可观测面不变。

    脱敏随 handler 同步生效：提前安装让早期日志出现的同时，early INFO 里若
    混有凭据形态同样过 SecretRedactionFilter，不存在脱敏真空期。
    """
    global _current
    _install_handler_locked()
    logging.getLogger().setLevel(logging.INFO)
    _current = "info"
    return _current


def apply_level(level, *, persist_note: bool = True) -> str:
    """安装 root handler（一次）并设定级别；返回生效级别。

    只改 root 与本项目自有 logger 的级别，不动 uvicorn.error/uvicorn.access：
    那两个由 uvicorn.Config(log_level=...) 管，访问日志级别不该被这个开关改掉。
    """
    global _current
    resolved = normalize_level(level)
    _install_handler_locked()
    logging.getLogger().setLevel(getattr(logging, resolved.upper(), logging.WARNING))
    _current = resolved
    if persist_note:
        logging.getLogger(__name__).debug("日志级别设为 %s", resolved)
    return resolved


def get_level() -> str:
    return _current


def load_from_db() -> str:
    """启动回填：读 app_settings('log.level')，脏值/缺行回默认级别。

    读取本身失败也回 DEFAULT：install_early 把启动窗口抬到 info，若这里因
    DB 故障沿用 _current，info 就会泄漏成运行期级别 —— 运行期默认必须是
    warning，宁可牺牲一次读失败时的个性化级别。
    """
    try:
        import database as db
        raw = db.get_app_setting("log.level")
    except Exception as e:
        logging.getLogger(__name__).warning("日志级别读取失败，沿用 %s: %s", DEFAULT_LEVEL, e)
        return apply_level(DEFAULT_LEVEL, persist_note=False)
    return apply_level(raw if raw is not None else DEFAULT_LEVEL, persist_note=False)
