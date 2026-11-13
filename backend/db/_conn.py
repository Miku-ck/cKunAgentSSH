"""连接池与库初始化 — 从 database.py 原样复制的连接层（阶段 1 骨架）。

与 database.py 各自持有线程本地连接池是迁移期的已知过渡态：
最终 database.py 会收敛为本包的再导出壳，届时只保留一份。
"""
import atexit
import logging
import re
import sqlite3
import threading

logger = logging.getLogger(__name__)

_db_path: str = ""

# SQL 标识符守卫（CWE-89 加固）：凡是拼进 f-string SQL 的表名/列名，必须是
# 裸标识符。这些标识符全部来自本模块的字面量常量（不接受任何外部入参），
# 校验是防御性的 —— 字面量手滑改出空格/引号/分号时当场抛 RuntimeError，
# 而不是静默生成一条语义被改写的 SQL。
_SQL_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _assert_sql_ident(name: str) -> None:
    if not _SQL_IDENT_RE.fullmatch(name):
        raise RuntimeError(f"拒绝拼接非法 SQL 标识符: {name!r}")


def _assert_sql_ident_list(names: str) -> None:
    """逗号分隔的列名串（如 SELECT 投影列列表）逐个过标识符守卫。"""
    for n in names.split(","):
        _assert_sql_ident(n.strip())

# ── 线程本地连接池 ──
_thread_local = threading.local()
# 跟踪所有线程的连接，用于进程退出时统一清理。
# 注意：不能用 WeakSet —— sqlite3.Connection 在部分 Python 构建（如 3.13）下
# 不支持弱引用，WeakSet.add() 会抛 "cannot create weak reference"，导致后端起不来。
# 由于 _thread_local 已保证每线程仅一条连接，这里改用普通 set，数量受线程数约束，不会无界增长。
_all_connections: set = set()
_conn_lock = threading.Lock()


class _PooledConnection:
    """sqlite3.Connection 的包装类，用于线程本地连接池。

    - 所有属性/方法委托到底层 _conn（通过 __getattr__）
    - close() 不真正关闭连接，而是执行 rollback 清理未提交事务，
      连接保留在线程本地存储中供下次复用
    - 这样现有代码中 51 处 `finally: conn.close()` 无需改动
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        """No-op close：回滚未提交事务，但不关闭底层连接。"""
        try:
            # 修复：记录未提交事务的回滚，便于排查数据丢失问题
            if self._conn.in_transaction:
                logger.debug("回滚未提交的事务（连接保持复用）")
            self._conn.rollback()
        except Exception as e:
            # 连接可能已损坏，下次 _get_conn() 会重建。此处不能静默吞掉：
            # 这是"有事务没能回滚"的唯一可见信号，pass 会让数据丢失无从排查。
            logger.warning("回滚未提交事务失败（连接将重建）: %s", e)


def _create_connection() -> sqlite3.Connection:
    """创建一个新的底层连接并应用 PRAGMA 设置。"""
    conn = sqlite3.connect(_db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# 连接空闲超过该秒数才做 SELECT 1 探活（审查 NIT：本地文件 SQLite 连接不会
# 像网络连接那样断开，原实现每次 _get_conn() 都跑一遍探活，给每条 SQL 白加
# 一次往返；30s 空闲阈值兼顾"长期空闲后校验"与热路径零开销）
_CONN_IDLE_PROBE_SECONDS = 30.0


def _get_conn() -> _PooledConnection:
    """获取当前线程的数据库连接（复用线程本地连接）。

    首次调用时创建底层连接并缓存到 threading.local()；
    后续调用直接返回包装后的连接。
    close() 被包装为 no-op，所以 `finally: conn.close()` 不会真正关闭连接。
    """
    # fail-fast 守卫：_db_path 为空说明没人初始化过本包连接池，此时
    # sqlite3.connect("") 会打开一个临时空库 —— 查什么都是 no such table，
    # 报错完全指向不了真实原因。初始化入口有两条：db.init_db()，或生产
    # 入口 database.init_db()（其末尾会把路径同步给本包）。
    if not _db_path:
        raise RuntimeError(
            "db 包连接池未初始化（_db_path 为空）：请先调用 db.init_db(db_path)，"
            "或经生产入口 database.init_db() 启动（它会同步路径到本包）。"
        )
    import time as _time
    now = _time.monotonic()
    conn = getattr(_thread_local, "conn", None)
    # 切库（init_db 指向另一个路径）必须换连接：池化连接连着上一个 _db_path，
    # 直接复用会让 init_db 的结构版本探测读到旧库 —— 把新库当成「已是最新」
    # 短路掉，建表/迁移永远不执行。
    if conn is not None and getattr(_thread_local, "conn_path", None) != _db_path:
        with _conn_lock:
            _all_connections.discard(conn)
        try:
            conn.close()
        except Exception as e:
            logger.warning("关闭切库前的旧连接失败（句柄可能泄漏）: %s", e)
        conn = None
        _thread_local.conn = None
    if conn is not None:
        last_used = getattr(_thread_local, "conn_last_used", 0.0)
        # 仅当连接空闲超过阈值时验证其有效性（热路径零探活开销）
        if now - last_used > _CONN_IDLE_PROBE_SECONDS:
            try:
                conn.execute("SELECT 1")
            except Exception:
                # 连接已损坏，从跟踪列表移除并重建
                with _conn_lock:
                    if conn in _all_connections:
                        _all_connections.remove(conn)
                    try:
                        conn.close()
                    except Exception as e:
                        # 探活失败已经证明这条连接坏掉了，close 再失败只是同一故障的
                        # 第二次回响；但泄漏的文件句柄只有这里能看到，留一条 warning
                        logger.warning("关闭失效连接失败（句柄可能泄漏）: %s", e)
                conn = None
                _thread_local.conn = None
        _thread_local.conn_last_used = now

    if conn is None:
        conn = _create_connection()
        with _conn_lock:
            _all_connections.add(conn)
        _thread_local.conn = conn
        _thread_local.conn_path = _db_path
        _thread_local.conn_last_used = now

    return _PooledConnection(conn)


def _close_all_connections() -> None:
    """关闭所有线程的连接。进程退出时由 atexit 钩子调用。"""
    with _conn_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()

    # 清除当前线程的引用
    if hasattr(_thread_local, "conn"):
        _thread_local.conn = None
        _thread_local.conn_last_used = 0.0


atexit.register(_close_all_connections)


def init_db(db_path: str) -> None:
    """初始化 db 包自己的连接池并确保库结构就绪。

    建表/迁移逻辑仍在 database.init_db（重构方案阶段 3 才迁入本包）；
    这里延迟导入调用它，既复用同一份 DDL，又避免模块级循环依赖。
    """
    global _db_path
    _db_path = db_path
    from database import init_db as _init_schema

    _init_schema(db_path)
