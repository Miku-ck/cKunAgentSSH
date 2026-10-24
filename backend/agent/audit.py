"""
AuditEngine — 追加式、链式签名防篡改的运维审计

合规目标：
- 对齐等保2.0「安全审计」要求：Agent 对本地/远程主机执行的命令、
  审批结论、拦截决定，全部落一条不可篡改的审计链。
- 每次追加记录前，读取链尾 hash 作为 prev_hash，计算自身 hash，形成单向链。
- 任何一条记录的修改/删除都会让后续校验失败（tamper-evident）。

设计取舍：
- 追加写（append-only）：本模块不提供 UPDATE/DELETE 审计记录的接口；
  即使直接操纵 SQLite 也会破坏 hash 链而被 verify() 检出。
- 单条写入使用事务 + BEGIN IMMEDIATE 串行化，避免并发下链式 hash 竞态。
- 审计失败默认不阻塞 Agent：append 内部 try/except，写库异常仅记 warning，
  并把失败计入 health() 快照（原实现只写一行日志，丢失是静默的）。
- 但「授权类」决策必须 write-ahead：调用方传 critical=True 时写失败会抛
  AuditWriteError，让授权/放行**不落地**。理由见 AuditWriteError 的注释。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import database

logger = logging.getLogger(__name__)

AUDIT_TABLE = "audit_log"
_DEFAULT_ENV = "test"
# 公开别名：跨模块（routes/agent.py 的审批记录）需要「环境缺失时回落到什么」，
# 不该各自内联一份字面量 "test" 而与本模块漂移。
DEFAULT_ENVIRONMENT = _DEFAULT_ENV


class AuditWriteError(RuntimeError):
    """关键审计记录写不进去 —— 调用方必须让对应动作**不生效**。

    审计链的价值前提是「发生过的事都有记录」。当一条记录写失败而动作照样执行，
    事后取证看到的是一个干净但**不完整**的链 —— 比空链更危险，因为它看起来可信。
    故授权/放行这类「记录本身就是凭证」的写入使用 critical=True：写不进去就不授权
    （授权端点回 503、授权状态回滚），而不是带着一条缺口继续跑。

    反过来，blocked/denied 这类**收紧**动作不依赖写入成功 —— 拒绝已经生效，
    记录缺失只是少一条证据，不构成放行风险，故仍走静默路径。
    """


# 审计写入失败的可观测快照。原实现失败只留一行 logger.warning，审计页与设置页
# 无从得知「证据链最近漏过记录」。这里累计计数，由 GET /api/audit/health 暴露。
_FAILURE_STATE: dict[str, Any] = {
    "consecutive": 0,
    "total": 0,
    "last_action": "",
    "last_error": "",
    "last_error_at": "",
}


def _note_write_failure(action: str, err: BaseException) -> None:
    _FAILURE_STATE["consecutive"] = int(_FAILURE_STATE["consecutive"]) + 1
    _FAILURE_STATE["total"] = int(_FAILURE_STATE["total"]) + 1
    _FAILURE_STATE["last_action"] = str(action or "")
    _FAILURE_STATE["last_error"] = f"{type(err).__name__}: {err}"[:300]
    _FAILURE_STATE["last_error_at"] = _now()


def _note_write_success() -> None:
    _FAILURE_STATE["consecutive"] = 0


def health() -> dict[str, Any]:
    """审计写入健康度快照（只读）。

    ok=False 表示**最近一次**写入失败过（consecutive>0），审计页应据此提示
    「证据链当前不完整」，而不是显示一个空表当作「没有操作」。
    """
    consecutive = int(_FAILURE_STATE["consecutive"])
    return {
        "ok": consecutive == 0,
        "consecutive_failures": consecutive,
        "total_failures": int(_FAILURE_STATE["total"]),
        "last_failed_action": _FAILURE_STATE["last_action"],
        "last_error": _FAILURE_STATE["last_error"],
        "last_error_at": _FAILURE_STATE["last_error_at"],
    }


def reset_health() -> None:
    """清空失败计数（测试与「已人工排查完」的运维动作用）。"""
    _FAILURE_STATE.update({
        "consecutive": 0, "total": 0, "last_action": "",
        "last_error": "", "last_error_at": "",
    })


def _seal_environment(rows: list[dict[str, Any]]) -> str:
    """轮转封缄记录该记在哪个环境下 —— 被清空的证据里出现过 prod 就记 prod。

    审计页按 environment 过滤。原先封缄记录无条件写 _DEFAULT_ENV("test")，于是
    对生产主机做完轮转，prod 视图里连一条「谁清的库」都看不到，这次轮转在 prod
    审计面上凭空消失（S11-04）。取「任一行为 prod」这条单调规则而不是「众数/尾行」：
    封缄必须在它清掉的那些记录**原本可见的视图里同样可见**，宁可让混合轮转在
    prod 视图多出一条，也不能在 prod 视图少一条。
    """
    if any(str(r.get("environment") or "") == "prod" for r in rows):
        return "prod"
    return _DEFAULT_ENV


def _table_absent(exc: BaseException) -> bool:
    """「表还没建」是真空，其余异常是没读到 —— S11-01 要的就是这条分界线。

    首次运行前审计表不存在（还没有任何一条记录），这一情形必须继续返回空/0，
    否则新装机一进审计页就是一条错误；锁冲突、坏盘、schema 损坏这些都照抛。
    """
    return (
        isinstance(exc, sqlite3.OperationalError)
        and "no such table" in str(exc).lower()
    )

# 合法结论枚举 —— 单一事实来源。审计页的筛选下拉（src/views/ssh/AuditView.vue 的
# filters.conclusion）与渲染层追加审计的请求体类型（src/api/audit.ts 的 AuditConclusion）
# 必须与这里逐字相等，钉在
# backend/tests/test_ai_loop_core_regression.py::test_audit_conclusion_三处清单同源；
# 增删值改那几处，别只改这里。
# "released"：once 授权按 run 释放（release-run 兜底端点写入此结论）
# "answered"/"skipped"：澄清向导的两个终态（routes/agent.py clarification 端点）
LEGAL_CONCLUSIONS: tuple[str, ...] = (
    "executed", "failed", "blocked", "denied", "auto_allowed",
    "approved", "released", "revoked", "answered", "skipped",
)


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────

def _operator() -> str:
    """单机桌面形态下取操作系统用户名作为『操作人』"""
    for var in ("USERNAME", "USER", "LOGNAME"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        return os.getlogin()
    except (OSError, AttributeError):
        return "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(record: dict[str, Any]) -> str:
    """生成确定性规范串，供 hash 计算使用。字段顺序必须稳定，勿随意增删。"""
    return "|".join([
        str(record.get("ts", "")),
        str(record.get("operator", "")),
        str(record.get("target_type", "")),
        str(record.get("target_id", "")),
        str(record.get("environment", _DEFAULT_ENV)),
        str(record.get("action", "")),
        str(record.get("command_hash", "")),
        str(record.get("command_raw", "")),
        str(record.get("result", "")),
        str(record.get("conclusion", "")),
        str(record.get("run_id", "")),
        str(record.get("iteration", "0")),
    ])


def _chain_hash(canon: str, prev_hash: str) -> str:
    """链式签名：sha256(prev_hash | canon)"""
    return hashlib.sha256(f"{prev_hash}|{canon}".encode("utf-8")).hexdigest()


def command_hash(command: str) -> str:
    """命令规范化哈希（与 PermissionGate 使用同一规范化入口）"""
    if not command:
        return ""
    try:
        from agent.harness.safety import normalize_command_for_detection
        return hashlib.sha256(
            normalize_command_for_detection(str(command)).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(str(command).encode("utf-8")).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────────────
# 表结构（database.init_db 建表时有同源副本；本常量供测试建表引用）
# ──────────────────────────────────────────────────────────────────────────────

_AUDIT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    operator     TEXT NOT NULL DEFAULT '',
    target_type  TEXT NOT NULL DEFAULT '',
    target_id    TEXT NOT NULL DEFAULT '',
    environment  TEXT NOT NULL DEFAULT '{_DEFAULT_ENV}',
    action       TEXT NOT NULL DEFAULT '',
    command_hash TEXT NOT NULL DEFAULT '',
    command_raw  TEXT NOT NULL DEFAULT '',
    result       TEXT NOT NULL DEFAULT '',
    conclusion   TEXT NOT NULL DEFAULT '',
    run_id       TEXT NOT NULL DEFAULT '',
    iteration    INTEGER NOT NULL DEFAULT 0,
    prev_hash    TEXT NOT NULL DEFAULT '',
    hash         TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON {AUDIT_TABLE}(ts);
CREATE INDEX IF NOT EXISTS idx_audit_run    ON {AUDIT_TABLE}(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_target ON {AUDIT_TABLE}(target_id);
CREATE INDEX IF NOT EXISTS idx_audit_env    ON {AUDIT_TABLE}(environment);
"""


# ──────────────────────────────────────────────────────────────────────────────
# 追加写
# ──────────────────────────────────────────────────────────────────────────────

def append_record(
    *,
    action: str,
    conclusion: str,
    run_id: str = "",
    iteration: int = 0,
    target_type: str = "",
    target_id: str = "",
    environment: str = _DEFAULT_ENV,
    command_raw: str = "",
    result: str = "",
    operator: Optional[str] = None,
    critical: bool = False,
) -> Optional[dict[str, Any]]:
    """追加一条审计记录。

    critical=False（默认）：写失败静默，返回 None 不阻塞 Agent，但失败计入
    health()。用于 blocked/denied/executed 这类「动作已经发生（或被拦住）
    之后补记」的记录。

    critical=True：写失败抛 AuditWriteError。用于「记录本身就是授权凭证」的
    路径（grant / allow）。调用方必须先拿到它成功再让授权生效 —— 否则一次
    磁盘/锁故障就会静默产出一条「没有证据的授权」，事后无法证明谁批了什么。
    """
    if conclusion not in LEGAL_CONCLUSIONS:
        # 白名单保证枚举列有界，但"静默改成 executed"会让这条链式签名记录声称
        # 「这条命令执行过了」—— 历史上 answered/skipped 就是这样被改名的。不阻塞
        # Agent 的前提下改为留痕：原始结论进 result（它在本条记录的哈希覆盖范围内），
        # 并在日志上吵一句，让加新值不加白名单这件事在开发期就看得见。
        raw = " ".join(str(conclusion).split())[:60]
        logger.warning("审计结论非法，按 executed 落库: action=%s conclusion_raw=%s",
                       action, raw)
        marker = f"conclusion_raw={raw}"
        result = f"{result}｜{marker}" if result else marker
        conclusion = "executed"
    environment = environment or _DEFAULT_ENV
    record = {
        "ts": _now(),
        "operator": operator or _operator(),
        "target_type": target_type or "",
        "target_id": target_id or "",
        "environment": environment,
        "action": action,
        "command_hash": command_hash(command_raw),
        "command_raw": command_raw,
        "result": result,
        "conclusion": conclusion,
        "run_id": run_id,
        "iteration": int(iteration or 0),
    }
    try:
        conn = database._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            prev = _last_hash(conn)
            record["prev_hash"] = prev
            record["hash"] = _chain_hash(_canonical(record), prev)
            cur = conn.execute(
                f"INSERT INTO {AUDIT_TABLE} ({', '.join(_INSERT_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_INSERT_COLUMNS))})",
                tuple(record[c] for c in _INSERT_COLUMNS),
            )
            record["id"] = cur.lastrowid
            # COMMIT 必须落在同一个回滚守卫内：并发写者持共享锁时 COMMIT 会抛
            # database is locked 而 in_transaction 仍为 True。原写法把它放在
            # try/except 的 else 分支，异常被外层吞掉后半截事务就长在该线程上 ——
            # 池化连接的 close 是 rollback-and-reuse，底层连接永不真正关闭，
            # 写锁滞留后所有经 to_thread 的写都会在 busy_timeout 后失败。
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        _note_write_success()
        return record
    except Exception as e:
        # 带上 action：调用方不再各自包 try/except（本函数已承诺"失败静默返回 None"），
        # 这条日志是唯一的失败线索，缺了 action 就不知道该记录丢的是哪个决策
        logger.warning("审计写入失败: action=%s err=%s", action, e)
        _note_write_failure(action, e)
        if critical:
            raise AuditWriteError(
                f"关键审计记录写入失败（action={action}）: {e}") from e
        return None


async def append_record_async(*args, **kwargs) -> Optional[dict[str, Any]]:
    """异步包装 — 审查修复：原实现在 asyncio 上下文（reins 拦截器）中同步调用
    append_record，其 BEGIN IMMEDIATE 在其它连接持写锁时 busy_timeout 最长
    阻塞事件循环 5s。改由 to_thread 执行，不阻塞事件循环。
    """
    return await asyncio.to_thread(append_record, *args, **kwargs)


def _last_hash(conn) -> str:
    cur = conn.execute(f"SELECT hash FROM {AUDIT_TABLE} ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else ""


# ──────────────────────────────────────────────────────────────────────────────
# 查询 / 校验 / 导出
# ──────────────────────────────────────────────────────────────────────────────

# query 与 count 的过滤条件必须逐字一致。两者原先各自手抄同一份 5 字段，count
# 漏抄 action/run_id 曾导致 total 与 items 数量不一致（见 count docstring 里的
# 审查修复记录）。收敛到唯一构造点，杜绝再次分叉。
_FILTER_COLUMNS = ("environment", "target_id", "action", "conclusion", "run_id")


def _build_filters(
    environment: Optional[str],
    target_id: Optional[str],
    action: Optional[str],
    conclusion: Optional[str],
    run_id: Optional[str],
) -> tuple[str, list[Any]]:
    """按固定列序拼 WHERE 子句与对应参数；返回 (where, params)，params 为新列表。"""
    conds: list[str] = []
    params: list[Any] = []
    for col, val in zip(_FILTER_COLUMNS, (environment, target_id, action, conclusion, run_id), strict=True):
        if val:
            conds.append(f"{col} = ?")
            params.append(val)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def query(
    environment: Optional[str] = None,
    target_id: Optional[str] = None,
    action: Optional[str] = None,
    conclusion: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按条件查询审计记录（倒序）

    S11-01：读不到就抛，不再吞成空列表 —— 「空列表」在审计页只有一个合法含义：
    确实一条记录都没有。原先把 DB 故障也折成空列表，于是锁冲突/坏盘时的界面与
    轮转清空后的界面一模一样，等于用「确认没有」冒充「我没读到」。
    页面本来就有 error 通道（AuditView 的 `加载审计记录失败`），抛上去即可见。
    """
    where, params = _build_filters(environment, target_id, action, conclusion, run_id)
    params.append(limit)
    params.append(offset)
    try:
        conn = database._get_conn()
        cur = conn.execute(
            f"SELECT * FROM {AUDIT_TABLE} {where} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        if _table_absent(e):
            return []
        logger.warning("审计查询失败: %s", e)
        raise


def count(
    environment: Optional[str] = None,
    target_id: Optional[str] = None,
    action: Optional[str] = None,
    conclusion: Optional[str] = None,
    run_id: Optional[str] = None,
) -> int:
    """审计记录计数。过滤集与 query 共用 _build_filters，天然一致
    （曾手抄漏 action/run_id 导致 total 与 items 分叉，现收敛到唯一构造点）。
    失败口径同 query：表还没建 = 0 条，其余异常照抛（S11-01）。"""
    where, params = _build_filters(environment, target_id, action, conclusion, run_id)
    try:
        conn = database._get_conn()
        cur = conn.execute(f"SELECT COUNT(*) FROM {AUDIT_TABLE} {where}", params)
        return int(cur.fetchone()[0])
    except Exception as e:
        if _table_absent(e):
            return 0
        logger.warning("审计计数失败: %s", e)
        raise


def verify() -> dict[str, Any]:
    """全链校验：重算每条记录 hash 与 prev_hash 是否一致

    Returns:
        {"intact": bool, "total": int, "checked": int, "broken": [id,...]}
    """
    broken: list[int] = []
    total = 0
    checked = 0
    try:
        conn = database._get_conn()
        cur = conn.execute(
            f"SELECT id, prev_hash, hash, ts, operator, target_type, target_id, "
            f"environment, action, command_hash, command_raw, result, conclusion, "
            f"run_id, iteration FROM {AUDIT_TABLE} ORDER BY id ASC"
        )
        prev_hash = ""
        for row in cur.fetchall():
            total += 1
            record = {
                "ts": row[3], "operator": row[4], "target_type": row[5],
                "target_id": row[6], "environment": row[7], "action": row[8],
                "command_hash": row[9], "command_raw": row[10], "result": row[11],
                "conclusion": row[12], "run_id": row[13], "iteration": row[14],
            }
            expected = _chain_hash(_canonical(record), prev_hash)
            checked += 1
            if row[2] != expected or row[1] != prev_hash:
                broken.append(int(row[0]))
                # 链已断，后续记录也不可信，但继续统计便于定位
            prev_hash = row[2]
    except Exception as e:
        # S11-01 的反号兄弟：原先把读失败报成 {"intact": False}，而审计页拿到
        # intact=false 就渲染「链被篡改!」（tooltip 是「检测到篡改: 」+ 空列表）。
        # 一次锁冲突/坏盘于是对着评估方喊「审计证据被篡改」—— 这比报「没有记录」
        # 更糟：它是一项不存在的指控。口径与 query/count/export 同一条线：
        # 表还没建 = 空链、真空、自洽（0 条记录本来就没有可篡改的东西）；其余照抛。
        if _table_absent(e):
            return {"intact": True, "total": 0, "checked": 0, "broken": []}
        logger.warning("审计校验失败: %s", e)
        raise
    return {"intact": not broken, "total": total, "checked": checked,
            "broken": broken}


def export_rows() -> list[dict[str, Any]]:
    """导出全部审计记录（供 CSV/JSON 导出）

    失败口径与 query/count 同一条线（S11-01）：表还没建 = 真空、返回空列表，
    其余异常照抛。导出比列表更严重 —— 产物是要交到评估方手里的文件，
    把「没读到」折成空列表会产出一份看起来「审计没有记录」的 CSV，
    而且轮转清空后的导出长得一模一样。
    """
    try:
        conn = database._get_conn()
        cur = conn.execute(
            f"SELECT * FROM {AUDIT_TABLE} ORDER BY id ASC"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        if _table_absent(e):
            return []
        logger.warning("审计导出失败: %s", e)
        raise


# 表列的顺序清单 —— INSERT 列名与导出表头的唯一来源，顺序必须与 _AUDIT_SCHEMA 逐字一致。
# 空表导出也要给出同一份表头：原先退化成只有 `id` 一列，于是「审计还没有记录」
# 导出的是一个结构上根本不是审计日志的文件，交付给等保评估会被打回。
AUDIT_COLUMNS: tuple[str, ...] = (
    "id", "ts", "operator", "target_type", "target_id", "environment", "action",
    "command_hash", "command_raw", "result", "conclusion", "run_id", "iteration",
    "prev_hash", "hash", "created_at",
)
# 自增 id 与带默认值的 created_at 由库自己填，其余 14 列是记录本体的字段
_INSERT_COLUMNS: tuple[str, ...] = AUDIT_COLUMNS[1:-1]


# ──────────────────────────────────────────────────────────────────────────────
# 归档轮转（受控 clear）
# ──────────────────────────────────────────────────────────────────────────────

def archive_and_clear(archive_dir: Optional[str] = None) -> dict[str, Any]:
    """归档式清空（审计轮转）— append-only 契约下唯一安全的清理入口。

    与裸 DELETE 的区别（为什么这不违背防篡改设计）：
    1. 证据不消失：全量记录（含 hash/prev_hash 链字段）先落盘 CSV 归档，
       归档文件可离线重放校验同一条链；
    2. 清空动作本身入链：写入一条 action=audit_cleared 的封缄记录作为新链
       genesis（prev_hash=""），result 记录归档条数、文件名与旧链尾 hash，
       verify() 对 live 链依旧自洽；
    3. 原子性：删除与封缄同事务，任一步失败整体回滚，链不会"半断"；
       归档写盘发生在删除之前，不存在丢数据窗口（回滚后残留的归档文件
       只是未被引用的冗余副本）。

    Returns:
        {"archived": n, "file": 归档绝对路径, "last_hash": 旧链尾 hash,
         "seal_hash": 封缄记录 hash（新链尾）}；空表时 archived=0 且不动库。
    """
    import csv as _csv
    import io as _io

    conn = database._get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(f"SELECT * FROM {AUDIT_TABLE} ORDER BY id ASC")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if not rows:
            conn.execute("COMMIT")
            return {"archived": 0, "file": "", "last_hash": "", "seal_hash": ""}

        last_hash = str(rows[-1].get("hash") or "")

        base = archive_dir or os.path.dirname(os.path.abspath(database._db_path or ".")) or os.getcwd()
        os.makedirs(base, exist_ok=True)
        # 归档文件名走 UTC：同文件 _now()（审计记录 ts / 封印 ts）就是 UTC，
        # 用本地时间命名会让归档文件的字典序与链上时间顺序在跨时区部署下错位
        fname = f"audit_archive_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}.csv"
        archive_path = os.path.join(base, fname)
        buf = _io.StringIO()
        writer = _csv.DictWriter(buf, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        # utf-8-sig BOM：与 /audit/export 一致，Excel 直开不乱码
        with open(archive_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(buf.getvalue())

        conn.execute(f"DELETE FROM {AUDIT_TABLE}")

        seal = {
            "ts": _now(),
            "operator": _operator(),
            "target_type": "system",
            "target_id": "",
            "environment": _seal_environment(rows),
            "action": "audit_cleared",
            "command_hash": "",
            "command_raw": "",
            "result": f"archived={len(rows)} file={fname} old_last_hash={last_hash}",
            "conclusion": "executed",
            "run_id": "",
            "iteration": 0,
        }
        seal["prev_hash"] = ""  # 新链 genesis
        seal["hash"] = _chain_hash(_canonical(seal), "")
        conn.execute(
            f"INSERT INTO {AUDIT_TABLE} ({', '.join(_INSERT_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_INSERT_COLUMNS))})",
            tuple(seal[c] for c in _INSERT_COLUMNS),
        )
        conn.execute("COMMIT")
        logger.info("审计归档轮转: %d 条 → %s", len(rows), archive_path)
        return {"archived": len(rows), "file": archive_path,
                "last_hash": last_hash, "seal_hash": seal["hash"]}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise