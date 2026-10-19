"""Windows proactor 拆除修复（win_asyncio_teardown）单元测试。

全部用鸭子类型的假 transport：本文件必须在非 win32 上也能跑（后端 pytest 只在
ubuntu 跑，由 test_ci_matrix_round22 钉住），所以测试里绝不 import
asyncio.proactor_events —— 那在 Linux 上直接 ImportError。

被测契约：stdlib 的
_ProactorBasePipeTransport._call_connection_lost 在 finally 里调
self._sock.shutdown(SHUT_RDWR)。对端已强制重置该 socket 时它抛
ConnectionResetError(10054)，于是同一个 finally 里后续三步全被跳过：
sock.close()（fd 不释放）、server._detach(self)（wait_closed 可能永挂）、
_called_connection_lost=True（重入守卫失效 -> 二次 connection_lost）。
"""
import types

import pytest

import win_asyncio_teardown as wat

MISSING = object()
_SHUT_RDWR = 2


def stdlib_shaped_original(self, exc):
    """复刻 D:/ProgramData/miniconda3/Lib/asyncio/proactor_events.py:154-172。"""
    if self._called_connection_lost:
        return
    try:
        self._protocol.connection_lost(exc)
    finally:
        if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
            self._sock.shutdown(_SHUT_RDWR)
        self._sock.close()
        self._sock = None
        server = self._server
        if server is not None:
            server._detach(self)
            self._server = None
        self._called_connection_lost = True



class BoomSock:
    """对端已 RST 的 socket：shutdown() 抛 10054。"""

    def __init__(self, fail_shutdown=True, fail_close=False):
        self.closed = False
        self.fail_shutdown = fail_shutdown
        self.fail_close = fail_close

    def fileno(self):
        return 42

    def shutdown(self, how):
        if self.fail_shutdown:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。", None, 10054, None)

    def close(self):
        if self.fail_close:
            raise OSError(22, "拒绝关闭")
        self.closed = True


class FakeServer:
    def __init__(self):
        self.clients = set()
        self.detach_count = 0

    def _attach(self, transport):
        self.clients.add(transport)

    def _detach(self, transport):
        self.detach_count += 1
        self.clients.discard(transport)


class FakeProtocol:
    def __init__(self):
        self.lost_count = 0

    def connection_lost(self, exc):
        self.lost_count += 1


class FakeTransport:
    """只带那段 finally 真正读写的属性。"""

    def __init__(self, sock=None, server=MISSING):
        self._sock = BoomSock() if sock is None else sock
        self._server = FakeServer() if server is MISSING else server
        self._protocol = FakeProtocol()
        self._called_connection_lost = False
        self._closing = False
        if self._server is not None:
            self._server._attach(self)



def make_safe(orig=stdlib_shaped_original):
    return wat.wrap_call_connection_lost(orig)


# ── 先把回归钉住：原始形状确实漏掉拆除 ─────────────────────────────────────
def test_original_shape_leaks_when_shutdown_raises():
    t = FakeTransport()
    with pytest.raises(ConnectionResetError):
        stdlib_shaped_original(t, None)
    assert t._sock.closed is False
    assert t._server.detach_count == 0
    assert t._called_connection_lost is False


# ── 修复后的行为 ────────────────────────────────────────────────────────────
def test_socket_is_closed_after_shutdown_fails():
    sock = BoomSock()
    t = FakeTransport(sock=sock)
    make_safe()(t, None)  # 不得外抛
    assert t._sock is None, "引用应释放（与 stdlib 正常路径一致）"
    assert sock.closed is True, "fd 不释放就得等 GC 才闭"


def test_detach_actually_ran():
    # Server.close() 只在 len(_clients)==0 时 _wakeup()；漏 _detach 会让
    # uvicorn server.py 的 await server.wait_closed() 挂住 -> 孤儿后端。
    server = FakeServer()
    t = FakeTransport(server=server)
    make_safe()(t, None)
    assert server.detach_count == 1
    assert len(server.clients) == 0, "transport 不该残留在 server._clients"
    assert t._server is None


def test_reentry_guard_is_restored():
    # 漏置 _called_connection_lost 时，第二次 _force_close 会再通知一次
    # connection_lost —— uvicorn websockets_impl 的 connections.remove(self)
    # 第二次直接 ValueError。
    t = FakeTransport()
    safe = make_safe()
    safe(t, None)
    assert t._called_connection_lost is True
    assert t._sock is None
    safe(t, None)
    safe(t, None)
    assert t._protocol.lost_count == 1, f"connection_lost 应只通知一次，实际 {t._protocol.lost_count}"


def test_clean_teardown_left_untouched():
    """对端正常 FIN、shutdown() 不抛时，包装层不得多做任何动作。"""
    t = FakeTransport(sock=BoomSock(fail_shutdown=False))
    server = t._server
    make_safe()(t, None)
    assert t._sock is None
    assert server.detach_count == 1
    assert t._protocol.lost_count == 1


def test_non_oserror_still_propagates():
    """协议自身的 bug 必须照常外抛，不能被这套拆除逻辑吃掉。

    这里 socket 是正常的（shutdown 不抛），所以 stdlib 的 finally 会跑完：
    拆除已完成 + 抛的是 RuntimeError -> 与我们的失效模式无关，必须外抛。
    """
    class Exploding:
        def connection_lost(self, exc):
            raise RuntimeError("protocol bug")

    t = FakeTransport(sock=BoomSock(fail_shutdown=False))
    t._protocol = Exploding()
    with pytest.raises(RuntimeError, match="protocol bug"):
        make_safe()(t, None)


def test_protocol_oserror_propagates_when_teardown_completed():
    """判据是拆除状态而不是异常类型：stdlib 的 finally 跑完了（守卫位已置）
    说明 OSError 来自协议本身，不能当成 proactor 失效模式吞掉。"""
    class ResettingProtocol:
        def connection_lost(self, exc):
            raise ConnectionResetError(10054, "协议内部抛 OSError", None, 10054, None)

    t = FakeTransport(sock=BoomSock(fail_shutdown=False))
    t._protocol = ResettingProtocol()
    with pytest.raises(ConnectionResetError):
        make_safe()(t, None)
    assert t._called_connection_lost is True


def test_repair_completes_even_if_close_also_raises():
    """close() 也抛时仍要置位守卫，否则拆除会被无限重排。"""
    t = FakeTransport(sock=BoomSock(fail_close=True))
    server = t._server
    make_safe()(t, None)
    assert t._called_connection_lost is True
    assert server.detach_count == 1


def test_no_server_attached_is_fine():
    # 客户端侧 transport（httpx/anyio 出站）没有 server
    t = FakeTransport(server=None)
    make_safe()(t, None)
    assert t._called_connection_lost is True
    assert t._sock is None


# ── install() / _apply_patch() 的门控与自检 ─────────────────────────────────
def test_install_noop_off_windows(monkeypatch):
    def boom():
        raise AssertionError("非 win32 不该去 import proactor_events")

    monkeypatch.setattr(wat.sys, "platform", "linux")
    monkeypatch.setattr(wat, "_installed", False)
    monkeypatch.setattr(wat, "_resolve_target_class", boom)
    assert wat.install() is False


def test_install_returns_true_without_reapplying(monkeypatch):
    calls = []

    class Cls:
        pass

    monkeypatch.setattr(wat.sys, "platform", "win32")
    monkeypatch.setattr(wat, "_installed", False)
    monkeypatch.setattr(wat, "_resolve_target_class", lambda: Cls)
    monkeypatch.setattr(wat, "_apply_patch", lambda cls: calls.append(cls) or True)
    assert wat.install() is True
    assert wat.install() is True
    assert len(calls) == 1, f"_apply_patch 应只跑一次，实际 {len(calls)}"


_GOOD = ("shutdown", "fileno", "close", "_detach", "_called_connection_lost", "_sock", "_server")


def _fn(names=_GOOD):
    return types.SimpleNamespace(__code__=types.SimpleNamespace(co_names=tuple(names)))


def _cls(fn=None):
    return type("FakeProactorCls", (), {"_call_connection_lost": fn if fn is not None else _fn()})


def test_apply_patch_wraps_and_marks():
    cls = _cls()
    assert wat._apply_patch(cls) is True
    assert getattr(cls.__dict__["_call_connection_lost"], "_ckun_patched", False) is True


def test_apply_patch_is_idempotent():
    cls = _cls()
    assert wat._apply_patch(cls) is True
    first = cls.__dict__["_call_connection_lost"]
    assert wat._apply_patch(cls) is True
    assert cls.__dict__["_call_connection_lost"] is first, "重复 patch 会把拆除逻辑叠两层"


def test_apply_patch_refuses_unknown_shape():
    # 内部改名（改名 shutdown/_detach/守卫位）后必须放弃打补丁，而不是静默打错
    cls = _cls(_fn(("shutdown", "close", "_teardown_done")))
    before = cls.__dict__["_call_connection_lost"]
    assert wat._apply_patch(cls) is False
    assert cls.__dict__["_call_connection_lost"] is before


def test_apply_patch_refuses_missing_method():
    assert wat._apply_patch(type("Bare", (), {})) is False


def test_shape_check_rejects_missing_code():
    assert wat._stdlib_shape_ok(object()) is False
    assert wat._stdlib_shape_ok(_fn(_GOOD)) is True
