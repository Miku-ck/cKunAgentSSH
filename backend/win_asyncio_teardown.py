"""补齐 Windows proactor 被跳过的 socket 拆除（修 10054 噪音的真正代价）。

失效模式：CPython 3.13 的 `asyncio/proactor_events.py:154-172`
`_ProactorBasePipeTransport._call_connection_lost` 长这样：

    try:
        self._protocol.connection_lost(exc)
    finally:
        if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
            self._sock.shutdown(socket.SHUT_RDWR)   # <- 对端已 RST 时抛 10054
        self._sock.close()
        self._sock = None
        server = self._server
        if server is not None:
            server._detach(self)
        self._called_connection_lost = True

`shutdown()` 没有 try 包裹。对端强制重置连接（浏览器取消 SSE、主进程
`req.destroy()` 探活、WS 被替换后客户端重拨）时它抛 ConnectionResetError，
异常穿出 `Handle._run` 报到 asyncio logger，于是 stderr 上那段没人认领的
traceback；更要紧的是同一个 finally 里后面三步全部没跑：

- `sock.close()` 没跑 -> fd 要等 GC 才放；
- `server._detach(self)` 没跑 -> `Server.close()` 只在 `len(_clients)==0` 时才
  `_wakeup()`，uvicorn `server.py` 收尾的 `await server.wait_closed()` 可能永挂
  （即 CONTEXT.md 里的「孤儿后端」）；
- `_called_connection_lost` 没置 -> `_force_close` 的重入守卫失效，
  `protocol.connection_lost` 会被通知第二次，而 uvicorn 的
  `websockets_impl.py` 用的是 `connections.remove(self)`，第二次直接 ValueError。

修法只在 original 抛出且**拆除状态确实没走完**时补那三步，不复制 stdlib 的
函数体：CPython 以后改了这个函数，本补丁要么继续只在失效时补尾，要么被
`_stdlib_shape_ok` 认不出来而放弃打补丁，不会静默打错。

只在 win32 生效（`Selector` 路径没有 `shutdown()` 这一手），且必须早于任何
transport 创建，所以由 `main.py` 的 `__main__` 在 `asyncio.run()` 之前调用。
"""
import logging
import sys

logger = logging.getLogger(__name__)

#: stdlib 那段 finally 里的内部名。认不出来就不打补丁。
_REQUIRED_NAMES = ("shutdown", "_detach", "_called_connection_lost")
_PATCH_MARK = "_ckun_patched"
_installed = False

_MISSING = object()


def _stdlib_shape_ok(fn) -> bool:
    code = getattr(fn, "__code__", None)
    names = getattr(code, "co_names", None)
    if not isinstance(names, tuple):
        return False
    return all(name in names for name in _REQUIRED_NAMES)


def repair_teardown(transport) -> bool:
    """补跑被跳过的 close/_detach/守卫位。幂等；返回是否真的补过。"""
    if getattr(transport, "_called_connection_lost", True):
        return False
    sock = getattr(transport, "_sock", None)
    if sock is not None:
        try:
            sock.close()
        except OSError as e:
            logger.debug("proactor 拆除：sock.close() 失败 %s", e)
        transport._sock = None
    server = getattr(transport, "_server", None)
    if server is not None:
        try:
            server._detach(transport)
        except Exception as e:  # noqa: BLE001 - 补齐路径不得再抛
            logger.debug("proactor 拆除：server._detach() 失败 %s", e)
        transport._server = None
    transport._called_connection_lost = True
    return True


def wrap_call_connection_lost(original):
    def safe(self, exc):
        try:
            original(self, exc)
        except OSError:
            # 判据是拆除状态而非异常类型：守卫位已置 = stdlib 的 finally 跑完了
            # = 这个 OSError 来自协议自己，与本失效模式无关，照常外抛。
            if getattr(self, "_called_connection_lost", True):
                raise
            repair_teardown(self)
            logger.debug(
                "proactor 拆除已补齐（原 shutdown() 抛 10054）：protocol=%s",
                type(getattr(self, "_protocol", None)).__name__,
            )

    safe.__name__ = getattr(original, "__name__", "_call_connection_lost")
    safe.__qualname__ = getattr(original, "__qualname__", safe.__name__)
    return safe


def _resolve_target_class():
    from asyncio.proactor_events import _ProactorBasePipeTransport

    return _ProactorBasePipeTransport


def _apply_patch(cls) -> bool:
    current = cls.__dict__.get("_call_connection_lost", _MISSING)
    if current is _MISSING:
        logger.warning("asyncio 上没有 _call_connection_lost，跳过 proactor 拆除补丁")
        return False
    if getattr(current, _PATCH_MARK, False):
        return True
    if not _stdlib_shape_ok(current):
        logger.warning("asyncio 内部形状与预期不符，跳过 proactor 拆除补丁（沿用原生行为）")
        return False
    wrapped = wrap_call_connection_lost(current)
    setattr(wrapped, _PATCH_MARK, True)
    cls._call_connection_lost = wrapped
    return True


def install() -> bool:
    """打补丁；非 win32 / 形状不符时返回 False 且不改任何东西。"""
    global _installed
    if _installed:
        return True
    if sys.platform != "win32":
        return False
    try:
        cls = _resolve_target_class()
    except Exception as e:  # noqa: BLE001 - 补丁装不上不该拖垮启动
        logger.warning("proactor 拆除补丁无法定位 asyncio 内部类: %s", e)
        return False
    _installed = _apply_patch(cls)
    return _installed
