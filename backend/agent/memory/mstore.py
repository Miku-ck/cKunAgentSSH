"""v2 记忆数据访问（同步，沿用 database.py 直连风格；全部 SQL 参数绑定）。

主表 memories 是唯一事实源：FTS 侧表由本模块用 text.tokenize 重分词维护，
vec 侧表失败不影响正确性（检索自动退回 BLOB 扫描），两路都可随时丢弃重建。
"""
import json
import math
import sqlite3

import database as db

from . import vec as _vec
from .text import tokenize

_ACTIVE_STATUSES = ("active", "pending_reconcile")

_fts_flag: "bool | None" = None


def _conn():
    return db._get_conn()


def reset_flags() -> None:
    """测试用：重置进程内可用性缓存。"""
    global _fts_flag
    _fts_flag = None
    _vec.reset_probe()


def fts_enabled(conn) -> bool:
    global _fts_flag
    if _fts_flag is None:
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key='memory_v2.fts'"
            ).fetchone()
            _fts_flag = bool(row and row[0] == "1")
        except sqlite3.Error:
            _fts_flag = False
    return _fts_flag


# ── 写入 ──


def add_memory(
    *,
    kind: str,
    content: str,
    scope: str = "global",
    title: str = "",
    source: str,
    importance: float = 0.5,
    status: str = "active",
    pinned: int = 0,
    embedding=None,
    embedding_src: str = "",
    source_conv_id=None,
    agent_run_id=None,
) -> str:
    mid = db._new_id()
    now = db._now()
    blob = _vec.pack_vec(embedding) if embedding else None
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO memories (id,kind,scope,title,content,source,status,importance,"
            "pinned,use_count,valid_at,embedding,embedding_src,source_conv_id,"
            "agent_run_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
            (
                mid, kind, scope, title, content, source, status, importance,
                pinned, now, blob, embedding_src if blob else None,
                source_conv_id, agent_run_id, now, now,
            ),
        )
        _fts_sync(conn, mid, title, content)
        if blob:
            _vec.vec_upsert(conn, mid, blob, len(embedding))
        conn.commit()
    finally:
        conn.close()
    return mid


def _fts_sync(conn, mid: str, title: str, content: str) -> None:
    if not fts_enabled(conn):
        return
    conn.execute("DELETE FROM memory_fts WHERE mid=?", (mid,))
    toks = " ".join(tokenize(f"{title} {content}"))
    if toks:
        conn.execute("INSERT INTO memory_fts(mid, tok) VALUES(?,?)", (mid, toks))


def user_edit(memory_id: str, *, content: str, title: str) -> bool:
    """用户在 UI 编辑：改内容后旧向量作废（不重嵌入，宁缺毋滥），FTS 重建。"""
    conn = _conn()
    try:
        # 审查R2修复：审计要能还原"用户把什么改成了什么"，UPDATE 前先取旧内容；
        # 原实现 add_audit 恒传 old=None，审计链丢失修改前原值。
        old_row = conn.execute(
            "SELECT content FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        cur = conn.execute(
            "UPDATE memories SET content=?, title=?, embedding=NULL, "
            "embedding_src=NULL, updated_at=? WHERE id=?",
            (content, title, db._now(), memory_id),
        )
        if cur.rowcount:
            row = conn.execute(
                "SELECT content, title FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            _fts_sync(conn, memory_id, row["title"] or "", row["content"])
            _vec.vec_delete(conn, memory_id)
            add_audit(memory_id, "UPDATE", old=old_row["content"] if old_row else None,
                      new=content, reason="user_edit")
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 状态流转 ──


def get_memory(memory_id: str):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        return _public(row) if row else None
    finally:
        conn.close()


def get_many(ids) -> list:
    ids = list(ids)
    if not ids:
        return []
    conn = _conn()
    try:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM memories WHERE id IN ({marks})", ids
        ).fetchall()
        return [_public(r) for r in rows]
    finally:
        conn.close()


def retire(memory_id: str, *, reason: str, job_id: str = "", status: str = "invalidated",
           superseded_by: str = None) -> bool:
    """失效不物理删（spec §2.4）。返回是否命中行。"""
    conn = _conn()
    try:
        old = conn.execute(
            "SELECT content FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not old:
            return False
        now = db._now()
        cur = conn.execute(
            "UPDATE memories SET status=?, invalid_at=?, superseded_by=?, updated_at=? "
            "WHERE id=? AND status IN ('active','pending_reconcile')",
            (status, now, superseded_by, now, memory_id),
        )
        # 行存在但已非 active/pending_reconcile（已被 retire/supersede）时 UPDATE 命中 0 行，
        # 原实现仍写审计并返回 True，与"返回是否命中行"契约相悖：会重复落 DELETE 审计、
        # 并让 reconcile DELETE 分支误以为作废成功而把候选转正。按 rowcount 收敛。
        if cur.rowcount == 0:
            return False
        add_audit(memory_id, "DELETE" if status == "invalidated" else "UPDATE",
                  old=old["content"], new=None, reason=reason, job_id=job_id)
        conn.commit()
        return True
    finally:
        conn.close()


def supersede(old_id: str, new_id: str) -> None:
    """旧行让位给新行并继承热度（spec §5 UPDATE）。"""
    conn = _conn()
    try:
        # 审查R2修复：old_id 不存在时 fetchone() 返回 None，原下标访问抛 TypeError；
        # 提前取热度并显式 raise ValueError，与本模块其他数据访问错误契约一致。
        old_row = conn.execute(
            "SELECT use_count FROM memories WHERE id=?", (old_id,)
        ).fetchone()
        if old_row is None:
            raise ValueError(f"被让位的记忆行不存在: {old_id}")
        conn.execute(
            "UPDATE memories SET status='superseded', invalid_at=?, superseded_by=?, "
            "updated_at=? WHERE id=?",
            (db._now(), new_id, db._now(), old_id),
        )
        conn.execute(
            "UPDATE memories SET use_count = use_count + ? WHERE id=?",
            (old_row["use_count"], new_id),
        )
        conn.commit()
    finally:
        conn.close()


def adopt(memory_id: str, *, content: str, title: str = "", embedding=None,
          embedding_src: str = "") -> None:
    """迁移行通过消化转正（backfill ADD/UPDATE 复用原 id）。"""
    blob = _vec.pack_vec(embedding) if embedding else None
    conn = _conn()
    try:
        conn.execute(
            "UPDATE memories SET status='active', content=?, title=?, embedding=?, "
            "embedding_src=?, valid_at=?, updated_at=? WHERE id=?",
            (content, title, blob, embedding_src if blob else None, db._now(), db._now(), memory_id),
        )
        _fts_sync(conn, memory_id, title, content)
        if blob:
            _vec.vec_upsert(conn, memory_id, blob, len(embedding))
        else:
            _vec.vec_delete(conn, memory_id)
        conn.commit()
    finally:
        conn.close()


def mark_injected(memory_ids) -> None:
    """只有真正进入 prompt 才计热度（v1 检索即 touch 的污染点）。"""
    ids = list(memory_ids)
    if not ids:
        return
    conn = _conn()
    try:
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE memories SET use_count = use_count + 1, last_used_at=? "
            f"WHERE id IN ({marks})",
            (db._now(), *ids),
        )
        conn.commit()
    finally:
        conn.close()


def set_pinned(memory_id: str, pinned: bool) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE memories SET pinned=? WHERE id=?", (1 if pinned else 0, memory_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 检索侧（同步原语；融合逻辑在 retriever） ──


def _filters(scopes, kinds, statuses, alias="memories"):
    conds, params = [], []
    if scopes:
        conds.append(f"{alias}.scope IN ({','.join('?' * len(scopes))})")
        params.extend(scopes)
    if kinds:
        conds.append(f"{alias}.kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    if statuses:
        conds.append(f"{alias}.status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    where = "".join(f" AND {c}" for c in conds)
    return where, params


def _row_pass(row, scopes, kinds, statuses) -> bool:
    return (
        (not scopes or row["scope"] in scopes)
        and (not kinds or row["kind"] in kinds)
        and (not statuses or row["status"] in statuses)
    )


def fts_hits(query: str, limit: int = 20, scopes=None, kinds=None,
             statuses=_ACTIVE_STATUSES):
    """FTS5 BM25；返回 [(id, score)]，score=-bm25（越大越好）。

    FTS5 不可用时退回 Python 词重叠打分（LIKE 级兜底，保混合检索管线形状不变）。
    """
    toks = tokenize(query)[:24]
    if not toks:
        return []
    conn = _conn()
    try:
        if not fts_enabled(conn):
            return _overlap_hits(conn, toks, limit, scopes, kinds, statuses)
        m = " OR ".join(toks)
        where, params = _filters(scopes, kinds, statuses, alias="m")
        rows = conn.execute(
            "SELECT f.mid AS mid, bm25(memory_fts) AS rank FROM memory_fts f "
            "JOIN memories m ON m.id = f.mid "
            f"WHERE f MATCH ?{where} ORDER BY rank LIMIT ?",
            (m, *params, limit),
        ).fetchall()
        return [(r["mid"], -float(r["rank"])) for r in rows]
    except sqlite3.Error:
        return _overlap_hits(conn, toks, limit, scopes, kinds, statuses)
    finally:
        conn.close()


def _overlap_hits(conn, toks, limit, scopes, kinds, statuses):
    where, params = _filters(scopes, kinds, statuses, alias="memories")
    rows = conn.execute(
        f"SELECT id, content, title FROM memories WHERE 1=1{where} "
        "ORDER BY updated_at DESC LIMIT 500",
        params,
    ).fetchall()
    tset = set(toks)
    scored = []
    for r in rows:
        docs = set(tokenize(f"{r['title'] or ''} {r['content']}"))
        n = len(tset & docs)
        if n:
            scored.append((r["id"], n / len(tset)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def vec_search(query_vec, limit: int = 20, scopes=None, kinds=None,
               statuses=_ACTIVE_STATUSES):
    """向量路：sqlite-vec 可用则 KNN，否则 BLOB 全扫余弦。返回 [(id, cosine)]。"""
    if not query_vec:
        return []
    blob = _vec.pack_vec(query_vec)
    dim = len(query_vec)
    conn = _conn()
    try:
        knn = _vec.vec_knn(conn, blob, limit * 3)
        if knn is not None:
            hits = []
            for mid, dist in knn:
                row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
                if row and _row_pass(row, scopes, kinds, statuses):
                    hits.append((mid, max(0.0, 1.0 - dist)))
                if len(hits) >= limit:
                    break
            return hits
        where, params = _filters(scopes, kinds, statuses, alias="memories")
        rows = conn.execute(
            f"SELECT id, embedding FROM memories WHERE embedding IS NOT NULL{where} "
            "ORDER BY updated_at DESC LIMIT 1000",
            params,
        ).fetchall()
        scored = []
        for r in rows:
            v = _vec.unpack_vec(r["embedding"])
            if len(v) == dim:
                scored.append((r["id"], _cosine(query_vec, v)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]
    finally:
        conn.close()


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def render_line(row) -> str:
    title = row.get("title") or (row.get("content") or "")[:24]
    prefix = "[待确认] " if row.get("status") == "pending_reconcile" else ""
    return (
        f"{prefix}[mem:{row['id']}] ({row['kind']}/{row['scope']}) "
        f"{title}: {row['content']}"
    )


def recent_fallback_lines(limit: int = 5) -> list:
    """检索故障兜底：仅按新近，不做相关性（仅限故障路径，v1 P1 教训保留）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM memories WHERE status='active' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [render_line(dict(r)) for r in rows]
    finally:
        conn.close()


# ── 列表 / 管理 ──


def _public(row) -> dict:
    d = dict(row)
    d["has_embedding"] = d.pop("embedding", None) is not None
    return d


def list_memories(*, kind=None, scope=None, status=None, source=None, q=None,
                  limit: int = 50, offset: int = 0) -> list:
    conds, params = [], []
    for col, val in (("kind", kind), ("scope", scope), ("status", status), ("source", source)):
        if val:
            conds.append(f"{col} = ?")
            params.append(val)
    if q:
        conds.append("(content LIKE ? OR title LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [_public(r) for r in rows]
    finally:
        conn.close()


def stats() -> dict:
    conn = _conn()
    try:
        out = {"memories": {}, "jobs": {}}
        for r in conn.execute("SELECT kind, status, COUNT(*) n FROM memories GROUP BY kind, status"):
            out["memories"].setdefault(r["kind"], {})[r["status"]] = r["n"]
        for r in conn.execute("SELECT status, COUNT(*) n FROM memory_jobs GROUP BY status"):
            out["jobs"][r["status"]] = r["n"]
        return out
    finally:
        conn.close()


# ── jobs ──


def enqueue_job(kind: str, payload: dict, priority: int = 5) -> str:
    jid = f"mj-{db._new_id()}"
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO memory_jobs (id, kind, payload, priority) VALUES (?,?,?,?)",
            (jid, kind, json.dumps(payload, ensure_ascii=False), priority),
        )
        conn.commit()
        return jid
    finally:
        conn.close()


def next_jobs(limit: int = 20) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM memory_jobs WHERE status='pending' AND attempts < 3 "
            "ORDER BY priority, created_at LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def finish_job(job_id: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE memory_jobs SET status='done' WHERE id=?", (job_id,))
        conn.commit()
    finally:
        conn.close()


def fail_job(job_id: str, error: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE memory_jobs SET attempts=attempts+1, last_error=?, "
            "status=CASE WHEN attempts+1 >= 3 THEN 'failed' ELSE 'pending' END WHERE id=?",
            (error[:300], job_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_jobs(status=None, limit: int = 50) -> list:
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT id,kind,status,attempts,last_error,priority,created_at "
                "FROM memory_jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,kind,status,attempts,last_error,priority,created_at "
                "FROM memory_jobs ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def retry_job(job_id: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE memory_jobs SET status='pending', attempts=0 WHERE id=? AND status='failed'",
            (job_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── audit ──


def add_audit(memory_id: str, action: str, *, old=None, new=None, reason: str = "",
              job_id: str = "") -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO memory_audit (memory_id, job_id, action, old_content, "
            "new_content, reason) VALUES (?,?,?,?,?,?)",
            (memory_id, job_id, action, old, new, reason[:300]),
        )
        conn.commit()
    finally:
        conn.close()


def list_audit(memory_id=None, limit: int = 50) -> list:
    conn = _conn()
    try:
        if memory_id:
            rows = conn.execute(
                "SELECT * FROM memory_audit WHERE memory_id=? ORDER BY id DESC LIMIT ?",
                (memory_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── blocks ──


def get_block(key: str):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM memory_blocks WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_blocks() -> list:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memory_blocks ORDER BY key"
        ).fetchall()]
    finally:
        conn.close()


def put_block(key: str, value: str, updated_by: str) -> int:
    """整块写入（用户 UI 用）；agent 走 replace_in_block。超限抛 ValueError。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT value, version, limit_chars FROM memory_blocks WHERE key=?", (key,)
        ).fetchone()
        limit_chars = row["limit_chars"] if row else 2000
        if len(value) > limit_chars:
            raise ValueError(f"块内容超限：{len(value)} > {limit_chars} 字符")
        new_version = (row["version"] + 1) if row else 1
        conn.execute(
            "INSERT OR REPLACE INTO memory_blocks (key, value, limit_chars, version, "
            "updated_by, updated_at) VALUES (?,?,?,?,?,?)",
            (key, value, limit_chars, new_version, updated_by, db._now()),
        )
        conn.execute(
            "INSERT INTO memory_block_history (key, version, value, updated_by) "
            "VALUES (?,?,?,?)",
            (key, new_version, value, updated_by),
        )
        conn.execute(
            "DELETE FROM memory_block_history WHERE key=? AND version NOT IN "
            "(SELECT version FROM memory_block_history WHERE key=? "
            "ORDER BY version DESC LIMIT 20)",
            (key, key),
        )
        conn.commit()
        return new_version
    finally:
        conn.close()


def replace_in_block(key: str, old: str, new: str, updated_by: str) -> int:
    """Letta 式精确替换：old 必须恰好出现一次；拒绝整块清空。"""
    row = get_block(key)
    base = row["value"] if row else ""
    if not old and not base:
        return put_block(key, new, updated_by)
    if not old:
        # 审查R2修复：block_update 工具描述承诺「old_string 传空=末尾追加」，
        # 原实现在非空块上直接抛"不能为空"，承诺与实现背离；补齐追加语义。
        return put_block(key, base + "\n" + new, updated_by)
    n = base.count(old)
    if n != 1:
        raise ValueError(f"old_string 出现 {n} 次，需恰好 1 次")
    merged = base.replace(old, new)
    if not merged.strip():
        raise ValueError("拒绝清空画像块")
    return put_block(key, merged, updated_by)


def list_block_history(key: str, limit: int = 20) -> list:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT key, version, updated_by, created_at, value FROM memory_block_history "
            "WHERE key=? ORDER BY version DESC LIMIT ?", (key, limit)
        ).fetchall()]
    finally:
        conn.close()


# ── 维护（tick 调用；失效不物理删） ──


def trim_pools(max_items: int) -> int:
    """每 kind 保留 importance/use_count/新近 最优的 max_items 条，其余 retire。

    pinned 与 pending_reconcile（job 未消化）不参与（spec §5/§11）。
    """
    conn = _conn()
    total = 0
    try:
        for kind in ("semantic", "episodic", "procedural"):
            cur = conn.execute(
                "UPDATE memories SET status='invalidated', invalid_at=?, updated_at=? "
                "WHERE id IN (SELECT id FROM memories WHERE kind=? AND status='active' "
                "AND pinned=0 ORDER BY importance DESC, use_count DESC, created_at DESC "
                "LIMIT -1 OFFSET ?)",
                (db._now(), db._now(), kind, max_items),
            )
            total += cur.rowcount
        conn.commit()
        return total
    finally:
        conn.close()


def decay_memories(days: int = 90, delta: float = 0.1, floor: float = 0.2) -> dict:
    """长期无人用且零命中的降权，跌破地板 retire（自增强奖励回路已整体删除）。

    降权不碰 updated_at：它是账面记账不是内容编辑（mark_injected 计热度、set_pinned
    改旗标同理）。updated_at 是本模块三处「新近」口径的唯一依据 —— _overlap_hits 取
    最近 500 行做词法候选、vec_search 的 BLOB 兜底取最近 1000 行、recent_fallback_lines
    直接按它挑注入 prompt 的记忆；而 decay 的人群恰好是全库最旧最零命中的那一批，
    一刷就把它们重新钉到窗口顶部：库超过 500 条后新写好用的记忆被挤出候选，
    兜底路注入的反而是刚被判「无用」的垃圾。
    """
    conn = _conn()
    try:
        # ROUND(...,4) 不是装饰：importance=0.3 减 0.1 在二进制浮点里是
        # 0.19999999999999998，紧跟其后的地板判据 importance<0.2 就在同一次 tick 里
        # 把「刚好降到地板」的行判死。地板的语义是降到就不再往下，不是差一个 eps。
        c1 = conn.execute(
            "UPDATE memories SET importance = ROUND(importance - ?, 4) "
            "WHERE status='active' AND pinned=0 AND use_count=0 "
            "AND ifnull(last_used_at, created_at) < datetime('now', ?) "
            "AND importance > ?",
            (delta, f"-{days} day", floor),
        ).rowcount
        # S3-02 修复：第二条原本是一条**没有年龄门槛**的地板清扫 —— 只要
        # importance < floor 就在同一次 tick 里被作废，于是刚写入的低权重候选
        # （抽取器常给 0.1~0.2）出生即死，用户视角是「记了却立刻消失」。现在它
        # 与第一条共用同一个人群门槛（长期零命中且久未使用），跌破地板才清理的是
        # 同批被降权的旧行。
        stale = [row["id"] for row in conn.execute(
            "SELECT id FROM memories "
            "WHERE status='active' AND pinned=0 AND use_count=0 "
            "AND ifnull(last_used_at, created_at) < datetime('now', ?) "
            "AND importance < ?",
            (f"-{days} day", floor),
        ).fetchall()]
        conn.commit()
    finally:
        conn.close()
    # 逐行走 retire()：原实现是裸 UPDATE，绕过 retire 后 memory_audit 一条都不写，
    # 而 spec §4.4 要求「所有动作落审计」—— 用户翻历史抽屉时看不到任何东西，
    # 只能看见记忆凭空变成 invalidated。retire 自带状态守卫，重复调用幂等。
    for mid in stale:
        retire(mid, reason=f"decay: 降权后 importance 跌破地板 {floor}", job_id="")
    return {"decayed": c1, "invalidated": len(stale)}


def retire_all_unpinned() -> int:
    """整库清空（pinned 除外）：逐行走 retire()，一条不落审计。

    与 decay 的地板清扫同一个理由 —— spec §4.4 要求所有动作落审计，而裸 UPDATE 绕过
    retire() 时 memory_audit 一行都不写：用户点完「清空记忆」，历史抽屉里没有任何解释，
    只看见整库凭空 invalidated。retire 自带状态守卫（非 active/pending 命中 0 行返回
    False），计数按返回值而非 rowcount。
    """
    conn = _conn()
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM memories "
            "WHERE pinned=0 AND status IN ('active','pending_reconcile')"
        ).fetchall()]
    finally:
        conn.close()
    return sum(
        1 for mid in ids
        if retire(mid, reason="手动清空全部未固定记忆", job_id="")
    )
