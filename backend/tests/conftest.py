import shutil
import sys
from pathlib import Path

import pytest

# 让测试可以直接 import backend/ 下的模块（safety.py 等）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session", autouse=True)
def _sweep_tmp_workspace():
    """每轮测试前后把 _tmp/ 整个清干净（沙箱禁止系统 tmp，测试产物只能落在 _tmp/）。

    只靠 tmp_workspace 的逐目录 rmtree 会留孤儿：database 的连接池是
    keep-alive（close() = rollback 后保留底层句柄），进程退出前 _tmp 里的
    .db/.db-shm/.db-wal 一直开着，Windows 下 rmtree(ignore_errors=True)
    静默失败，目录就永久残留。这里每轮测试前后整目录清扫：上一轮进程
    退出后句柄已释放，可全部删掉；仍被占用的（如挂死进程持锁）由
    ignore_errors 兜底跳过，不阻塞测试。
    """
    base = Path(__file__).resolve().parent / "_tmp"

    def sweep() -> None:
        if not base.is_dir():
            return
        for child in base.iterdir():
            shutil.rmtree(child, ignore_errors=True)

    sweep()
    yield
    sweep()


@pytest.fixture(scope="session", autouse=True)
def _harden_socketpair():
    """Windows + py3.13 的 socket.socketpair 回退实现（AF_INET bind/listen/connect/accept）
    偶发在 accept 上死等（CPython gh-118761 类竞态，listen 与 connect 之间有窗口期），
    asyncio ProactorEventLoop 建 self-pipe 时命中即挂死整个测试会话——实测非确定性
    卡在套件 24% / 67% 两处（pytest-timeout dump 栈确认链路为
    asyncio.run → new_event_loop → ProactorEventLoop.__init__ → _make_self_pipe）。
    这里对回退路径加超时与重试：非 Windows 平台零影响，原生 socketpair 平台不触达。
    """
    import os
    import socket

    if os.name != "nt":
        return

    _orig = socket.socketpair

    def _socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        for _ in range(3):
            lsock = socket.socket(family, type, proto)
            csock = ssock = None
            try:
                lsock.bind(("127.0.0.1", 0))
                lsock.listen(1)
                lsock.settimeout(5)
                csock = socket.socket(family, type, proto)
                # connect 同样要超时：本机实测回环 connect 的 SYN 会被静默丢弃，
                # 阻塞模式死等数分钟（py-spy 栈确认），只给 accept 加超时挡不住。
                csock.settimeout(5)
                csock.connect(lsock.getsockname())
                ssock, _ = lsock.accept()
                csock.settimeout(None)
                ssock.settimeout(None)
                return ssock, csock
            except OSError:
                for s in (ssock, csock):
                    if s is not None:
                        try:
                            s.close()
                        except OSError:
                            pass
            finally:
                lsock.close()
        raise OSError("socketpair retry exhausted on Windows")

    socket.socketpair = _socketpair
    try:
        yield
    finally:
        socket.socketpair = _orig


@pytest.fixture
def tmp_workspace():
    """工作区内的临时目录（沙箱禁止系统 tmp，pytest 默认 tmp_path 不可用）

    注意：不能用 tempfile.mkdtemp — Windows 下它给目录打限制性 ACL，
    沙箱进程随后读写该目录会被拒绝。普通 os.mkdir 继承默认 ACL，可用。
    """
    import uuid
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    d = base / f"wstest-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    yield str(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def base_db(tmp_workspace):
    """真实 SQLite，返回库文件路径；进出各收一次连接池。

    每个测试拿独立的 tmp_workspace 目录，所以库文件名不需要再按用例区分。
    """
    import os

    import database as db
    db_path = os.path.join(tmp_workspace, "test.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


@pytest.fixture
def runbook_db(base_db):
    """base_db + runbook_store 的建表 memo 复位（切库后必须重走 DDL）。"""
    import runbook_store as rstore
    rstore._reset_memo_for_tests()
    yield base_db
