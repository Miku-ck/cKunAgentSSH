"""向量通路：embedding BLOB 编解码 + sqlite-vec 可选加速。

约定：
- memories.embedding 恒为 float32 LE BLOB（唯一事实源）；
- sqlite-vec 只是加速器：扩展不可用/维度漂移/任何异常 → 全量以 BLOB 建表内
  Python 余弦扫描兜底（单用户千级规模够用，spec §11）。
"""
import logging
import struct

logger = logging.getLogger(__name__)

_VEC_OK_CONNS: dict = {}         # 底层连接对象 -> 该连接上 sqlite-vec 是否可用
_VEC_DIM: int | None = None      # 当前 vec 表维度


def pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vec(blob) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _raw_conn(conn):
    """剥掉 _PooledConnection 薄壳，取长寿命的底层 sqlite3.Connection。

    包装对象每次 _get_conn() 都新建，按它缓存等于永远不命中；底层连接按
    对象身份可哈希（但不支持弱引用，见 database.py 注释），这里以对象本身
    为 dict 键 —— dict 强引用让键不被回收，杜绝"连接关闭后 id 复用"造成
    的假命中（代价是已废弃连接的探测标记留到进程退出，量级为线程数，可接受）。
    """
    return getattr(conn, "_conn", conn)


def vec_available(conn) -> bool:
    # 审查R2修复：sqlite-vec 扩展按连接生效，而 database 层是线程本地连接池。
    # 原实现在首条连接加载扩展后进程级缓存 True：其他线程的连接根本没加载，
    # KNN/虚表查询照样去试然后报错降级。改为按底层连接对象缓存，逐连接加载。
    key = _raw_conn(conn)
    cached = _VEC_OK_CONNS.get(key)
    if cached is not None:
        return cached
    ok = False
    try:
        import sqlite_vec
        key.enable_load_extension(True)
        sqlite_vec.load(key)
        key.enable_load_extension(False)
        ok = True
    except Exception as e:
        logger.debug("sqlite-vec 不可用，向量检索走 Python 扫描兜底: %s", e)
    _VEC_OK_CONNS[key] = ok
    return ok


def reset_probe() -> None:
    """测试用：重置探测缓存（含按连接缓存的可用性标记）。"""
    global _VEC_DIM
    _VEC_OK_CONNS.clear()
    _VEC_DIM = None


def ensure_vec_table(conn, dim: int) -> bool:
    """建/重建 memories_vec（维度漂移时全量重建）。返回是否可用。"""
    global _VEC_DIM
    if not vec_available(conn):
        return False
    try:
        if _VEC_DIM is None:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key='memory_v2.vec_dim'"
            ).fetchone()
            _VEC_DIM = int(row[0]) if row else None
        if _VEC_DIM is not None and _VEC_DIM != dim:
            conn.execute("DROP TABLE IF EXISTS memories_vec")
            _VEC_DIM = None
        if _VEC_DIM is None:
            conn.execute("DROP TABLE IF EXISTS memories_vec")
            conn.execute(
                f"CREATE VIRTUAL TABLE memories_vec USING vec0("
                f"memory_id TEXT PRIMARY KEY, "
                f"embedding float[{dim}] distance_metric=cosine)"
            )
            _VEC_DIM = dim
            conn.execute(
                "INSERT OR REPLACE INTO app_settings(key,value) "
                "VALUES('memory_v2.vec_dim', ?)", (str(dim),),
            )
        return True
    except Exception as e:
        logger.debug("sqlite-vec 表初始化失败，退回 Python 扫描: %s", e)
        # 审查R2修复：降级只标记当前连接（原为进程级全局，会误伤其他线程）
        _VEC_OK_CONNS[_raw_conn(conn)] = False
        return False


def vec_upsert(conn, memory_id: str, blob: bytes, dim: int) -> None:
    if not ensure_vec_table(conn, dim):
        return
    try:
        conn.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))
        conn.execute(
            "INSERT INTO memories_vec(memory_id, embedding) VALUES(?, ?)",
            (memory_id, blob),
        )
    except Exception as e:
        logger.debug("vec upsert 失败（不影响扫描兜底）: %s", e)


def vec_delete(conn, memory_id: str) -> None:
    try:
        conn.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))
    except Exception:
        pass  # 表不存在/未加载：扫描路径无残留概念


def vec_knn(conn, qblob: bytes, k: int) -> "list[tuple[str, float]] | None":
    """返回 [(memory_id, cosine_distance)]；不可用返回 None（调用方走扫描）。"""
    if not vec_available(conn):
        return None
    try:
        rows = conn.execute(
            "SELECT memory_id, distance FROM memories_vec "
            "WHERE embedding MATCH ? AND k = ?",
            (qblob, k),
        ).fetchall()
        return [(r["memory_id"], r["distance"]) for r in rows]
    except Exception as e:
        logger.debug("vec KNN 失败，退回扫描: %s", e)
        return None
