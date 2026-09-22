"""向量通路：embedding BLOB 编解码 + sqlite-vec 可选加速。

约定：
- memories.embedding 恒为 float32 LE BLOB（唯一事实源）；
- sqlite-vec 只是加速器：扩展不可用/维度漂移/任何异常 → 全量以 BLOB 建表内
  Python 余弦扫描兜底（单用户千级规模够用，spec §11）。
"""
import logging
import struct

logger = logging.getLogger(__name__)

_VEC_OK: bool | None = None      # 三态：None 未探测
_VEC_DIM: int | None = None      # 当前 vec 表维度


def pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vec(blob) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def vec_available(conn) -> bool:
    global _VEC_OK
    if _VEC_OK is None:
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            _VEC_OK = True
        except Exception as e:
            logger.debug("sqlite-vec 不可用，向量检索走 Python 扫描兜底: %s", e)
            _VEC_OK = False
    return _VEC_OK


def reset_probe() -> None:
    """测试用：重置探测缓存。"""
    global _VEC_OK, _VEC_DIM
    _VEC_OK = None
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
        globals()["_VEC_OK"] = False
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
