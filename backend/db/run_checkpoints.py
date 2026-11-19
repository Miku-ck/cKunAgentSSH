"""运行检查点存储 — run_checkpoints 单表（挂起 / 续跑的唯一持久化载体）。

为什么需要这张表（对应「用户确认前无限等待 + 应用意外关闭后继续」）：
- ``agent_runs`` 只有结果快照（status / 统计），``messages`` 表只有「用户消息 +
  终态 assistant」一条；run 的活状态（LLM 对话、待确认项、半截输出）此前全在
  进程内存，进程一重启就没了。
- 本表把「挂起那一刻」的完整上下文落库：
    · messages —— OpenAI 原始形状的完整消息数组，恢复时原样塞回 ``ctx.messages``，
      **绝不重新拼装**（重新拼装会拆散 assistant.tool_calls 与 role=tool 的配对，
      vLLM / OpenAI 直接 400）；
    · harness  —— 重建 Harness 所需的运行参数（模型、温度、权限档、专家、主机…）；
    · pending  —— 待确认项（审批卡 / 澄清题）；
    · partial  —— 已经产出的半截内容，供前端恢复后把气泡填回去。

工程约定（与 ``db/agent_runs.py``、``runbook_store.py`` 同风格）：
- 建表 ``CREATE TABLE IF NOT EXISTS`` 幂等，首用即建；memo 按库路径记忆 —— 测试会
  切库（``_close_all_connections`` + ``init_db`` 新路径），memo 键必须跟着路径走，
  否则新库上跳过建表直接 OperationalError。
- SQL 全部静态字面量，外部输入一律参数绑定；不拼接 SQL。
- 连接复用 ``db._conn._get_conn()``（同一个 SQLite 库、同一 WAL / PRAGMA）。
- 全部同步函数（调用方自行 ``asyncio.to_thread``），与 database.py / audit.py 同风格。
- 本表**刻意不建外键**：``prune_agent_runs`` 会按保留条数删 agent_runs 行，若这里
  声明 ``ON DELETE CASCADE``，等待中的卡片会被静默带走（不报错，最难查）。孤儿行
  由 ``clear_checkpoint`` 与显式清理负责。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

import db._conn as _conn_mod
from db._conn import _get_conn
from db._util import _now

logger = logging.getLogger(__name__)

__all__ = [
    "save_checkpoint",
    "load_checkpoint",
    "update_messages",
    "set_pending",
    "clear_checkpoint",
    "list_waiting_checkpoints",
    "ensure_tables",
]

# 保留最近 N 条消息。20 与前端 `context_window` 的默认值同量级：一次挂起发生在一轮
# 工具调用前后，20 条足够覆盖「上一轮工具结果 + 本轮 assistant tool_calls」的配对。
MAX_MESSAGES = 20
# 单条消息的软上限。超过时收缩 content 文本；**配对结构（tool_calls / tool_call_id）
# 永远保留** —— 结构被删比体积超限严重得多（超限只是慢，结构断裂是 400）。
MAX_MESSAGE_BYTES = 64 * 1024
# 内联二进制（base64 图片）的占位文案：既保留「这里原本有图」的事实，又给出补救路径。
BINARY_PLACEHOLDER = "[图片已省略，如需请重新读取]"
_TRUNCATED_SUFFIX = "…[truncated]"

_DDL = """
CREATE TABLE IF NOT EXISTS run_checkpoints (
    run_id          TEXT PRIMARY KEY,
    conv_id         TEXT NOT NULL DEFAULT '',
    messages_json   TEXT NOT NULL DEFAULT '[]',
    harness_json    TEXT NOT NULL DEFAULT '{}',
    pending_json    TEXT NOT NULL DEFAULT '{}',
    partial_json    TEXT NOT NULL DEFAULT '{}',
    iteration       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_checkpoints_conv ON run_checkpoints(conv_id);
"""

_UPSERT = """
INSERT INTO run_checkpoints
    (run_id, conv_id, messages_json, harness_json, pending_json, partial_json,
     iteration, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(run_id) DO UPDATE SET
    conv_id       = excluded.conv_id,
    messages_json = excluded.messages_json,
    harness_json  = excluded.harness_json,
    pending_json  = excluded.pending_json,
    partial_json  = excluded.partial_json,
    iteration     = excluded.iteration,
    updated_at    = excluded.updated_at
"""

# 建表幂等 memo：按 db_path 记忆（切库必须重跑 DDL，见模块 docstring）。
_ensured_paths: set[str] = set()
_ensure_lock = threading.Lock()


def ensure_tables() -> None:
    """幂等建表（同一路径只执行一次 DDL）。"""
    key = str(getattr(_conn_mod, "_db_path", ""))
    if key in _ensured_paths:
        return
    with _ensure_lock:
        if key in _ensured_paths:
            return
        conn = _get_conn()
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()
        _ensured_paths.add(key)


def _reset_memo_for_tests() -> None:
    """测试切库后强制下一条语句重新建表（生产路径不用）。"""
    with _ensure_lock:
        _ensured_paths.clear()


# ── JSON 序列化 ───────────────────────────────────────────────────────────────


def _dumps(value: Any) -> str:
    """紧凑序列化：``{}`` / ``[]`` 的落库形态与代码里比较的字面量严格一致
    （``list_waiting_checkpoints`` 用 SQL 比较，带空格的 ``{ }`` 会漏筛）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_list(raw: Any) -> list:
    """messages_json → list；脏数据兜底空数组（一条坏行不该炸整个恢复流程）。"""
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw if isinstance(raw, str) else (raw or "[]"))
    except Exception:
        return []
    return value if isinstance(value, list) else []


def _load_obj(raw: Any) -> dict:
    """*_json → dict；脏数据兜底空对象。"""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw if isinstance(raw, str) else (raw or "{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


# ── 二进制剥离 ───────────────────────────────────────────────────────────────


def _is_data_uri(value: Any) -> bool:
    """是否内联 data URI（``data:image/png;base64,...``）。只认 data: 前缀 ——
    http(s) 图片地址只有几十字节，剥掉反而丢信息。"""
    if isinstance(value, str):
        return value.lstrip().lower().startswith("data:")
    if isinstance(value, dict):
        return _is_data_uri(value.get("url"))
    return False


def _image_source_is_inline(source: Any) -> bool:
    """Anthropic 形状 ``{"type":"image","source":{"type":"base64","data":"..."}}``。"""
    if not isinstance(source, dict):
        return False
    if str(source.get("type") or "").lower() == "base64":
        return True
    return _is_data_uri(source.get("data"))


def _strip_part(part: Any) -> Any:
    """单个 content part → 剥离内联二进制后的 part（不修改入参）。"""
    if not isinstance(part, dict):
        return part
    part_type = str(part.get("type") or "").lower()
    if part_type in ("image_url", "input_image"):
        if (_is_data_uri(part.get("image_url"))
                or _is_data_uri(part.get("input_image"))
                or _is_data_uri(part.get("url"))):
            return {"type": "text", "text": BINARY_PLACEHOLDER}
        return part
    if part_type == "image":
        if _image_source_is_inline(part.get("source")):
            return {"type": "text", "text": BINARY_PLACEHOLDER}
        return part
    # 兜底：任何挂着 data URI url 的未知 part（保险起见，不枚举全部厂商形状）
    if _is_data_uri(part.get("url")):
        return {"type": "text", "text": BINARY_PLACEHOLDER}
    return part


def _strip_message(message: Any) -> Any:
    """单条 message → 剥离内联二进制后的 message（不修改入参）。"""
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, list):
        return {**message, "content": [_strip_part(part) for part in content]}
    if _is_data_uri(content):
        return {**message, "content": BINARY_PLACEHOLDER}
    return message


def _strip_binary_from_messages(messages: Any) -> list:
    """剥离全部内联二进制：单条 base64 图片可达数 MB，落库前必须换成占位文案。

    入参不被修改（返回新 list / 新 dict），调用方可以继续用原对象。
    """
    if not isinstance(messages, list):
        return []
    return [_strip_message(message) for message in messages]


# ── 截断（保留配对）────────────────────────────────────────────────────────────


def _clip_text(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节截断字符串，附截断标记（不把多字节字符切一半）。"""
    reserve = 256  # JSON 外框 + role 等字段的余量
    limit = max(0, max_bytes - reserve)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    clipped = raw[:limit].decode("utf-8", errors="ignore")
    return clipped + _TRUNCATED_SUFFIX


def _shrink_message(message: dict, max_bytes: int) -> dict:
    """把单条消息压到软上限以内：只收缩 content，**永不删除 tool_calls /
    tool_call_id**（删了配对就断，恢复时 LLM 直接 400）。"""
    if len(_dumps(message).encode("utf-8")) <= max_bytes:
        return message
    out = dict(message)
    content = out.get("content")
    if isinstance(content, str):
        out["content"] = _clip_text(content, max_bytes)
    elif isinstance(content, list):
        # 逐段收缩文本段；图片段早已被 _strip_part 换成短文本，这里只需兜底
        out["content"] = [
            ({"type": "text",
              "text": _clip_text(part.get("text") or "", max_bytes)}
             if isinstance(part, dict) and isinstance(part.get("text"), str)
             else part)
            for part in content
        ]
    else:
        out["content"] = _clip_text(str(content or ""), max_bytes)
    if len(_dumps(out).encode("utf-8")) > max_bytes:
        # 极端情况：超限来自 tool_calls.arguments 本体。结构优先于体积 ——
        # 保留 role / tool_calls / tool_call_id，只把 content 清成占位。
        out["content"] = BINARY_PLACEHOLDER
    return out


def _truncate_messages(
    messages: Any,
    max_count: int = MAX_MESSAGES,
    max_bytes: int = MAX_MESSAGE_BYTES,
) -> list:
    """保留最近 ``max_count`` 条 + 单条压缩到 ``max_bytes``，且**不拆分工具配对**。

    配对规则（OpenAI 形状）：``assistant(tool_calls=[...])`` 后必须紧跟对应的
    ``role='tool'`` 消息。因此：
    - 从头部裁剪后，若第一条是 ``role='tool'``，它的 owner 已经被裁掉 → 整条丢弃
      （连续多条一起丢，直到非 tool 消息为止）；
    - 只裁头部、不动尾部：``assistant(tool_calls)`` 与其结果永远同时存在或同时
      保留到尾部（尾部正是「挂起时那个未完成的调用」，属于合法形态）。
    """
    items = [message for message in (messages or []) if isinstance(message, dict)]
    if max_count > 0 and len(items) > max_count:
        items = items[-max_count:]
    while items and str(items[0].get("role") or "") == "tool":
        items.pop(0)
    return [_shrink_message(message, max_bytes) for message in items]


# ── 公开 CRUD ────────────────────────────────────────────────────────────────


def save_checkpoint(
    run_id: str,
    *,
    conv_id: str = "",
    messages: Any = None,
    harness: Any = None,
    pending: Any = None,
    partial: Any = None,
    iteration: int = 0,
) -> None:
    """写入**全量**检查点（不存在则插入，存在则整体覆盖）。

    语义是快照而非补丁：未提供的四个可选项按空值覆盖。只想改其中一项时用
    ``update_messages`` / ``set_pending``，不要用本函数。
    """
    ensure_tables()
    now = _now()
    rows = (
        str(run_id),
        str(conv_id or ""),
        _dumps(_truncate_messages(_strip_binary_from_messages(messages))),
        _dumps(harness if isinstance(harness, dict) else {}),
        _dumps(pending if isinstance(pending, dict) else {}),
        _dumps(partial if isinstance(partial, dict) else {}),
        int(iteration or 0),
        now,
        now,
    )
    conn = _get_conn()
    try:
        conn.execute(_UPSERT, rows)
        conn.commit()
    finally:
        conn.close()


def load_checkpoint(run_id: str) -> Optional[dict]:
    """读取检查点（四个 JSON 列已反序列化）；不存在返回 None。"""
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT run_id, conv_id, messages_json, harness_json, pending_json,"
            " partial_json, iteration, created_at, updated_at"
            " FROM run_checkpoints WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    data = dict(row)
    return {
        "run_id": data.get("run_id", ""),
        "conv_id": data.get("conv_id", ""),
        "messages": _load_list(data.get("messages_json")),
        "harness": _load_obj(data.get("harness_json")),
        "pending": _load_obj(data.get("pending_json")),
        "partial": _load_obj(data.get("partial_json")),
        "iteration": int(data.get("iteration") or 0),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
    }


def update_messages(run_id: str, messages: Any, iteration: Optional[int] = None) -> None:
    """仅刷新 messages / iteration / updated_at（行不存在时是 no-op）。

    ``iteration=None`` 表示保持原值（用 ``COALESCE(?, iteration)`` 表达，避免为
    「没传」再拼一套动态 SET 子句）。
    """
    ensure_tables()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE run_checkpoints SET messages_json = ?, iteration = COALESCE(?, iteration),"
            " updated_at = ? WHERE run_id = ?",
            (
                _dumps(_truncate_messages(_strip_binary_from_messages(messages))),
                None if iteration is None else int(iteration),
                _now(),
                str(run_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def set_pending(run_id: str, pending: Any) -> None:
    """写入 / 清除待确认项：``pending={}`` 表示清除（不是「不修改」）。

    行不存在时是 no-op —— 挂起前必须先 ``save_checkpoint``（否则恢复入口拿到
    的是「有待确认但没有上下文」，无法续跑）。
    """
    ensure_tables()
    payload = _dumps(pending if isinstance(pending, dict) else {})
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE run_checkpoints SET pending_json = ?, updated_at = ? WHERE run_id = ?",
            (payload, _now(), str(run_id)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_checkpoint(run_id: str) -> None:
    """删除检查点（run 终态 / 取消后调用；幂等）。"""
    ensure_tables()
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM run_checkpoints WHERE run_id = ?", (str(run_id),))
        conn.commit()
    finally:
        conn.close()


def list_waiting_checkpoints() -> list[dict]:
    """列出「还在等用户确认」的检查点（``pending_json`` 非空）。

    比较用 ``TRIM(...) NOT IN ('', '{}', 'null')`` 而不是裸 ``<> '{}'``：手工插入
    或旧版本写出的 ``'{ }'`` / ``'null'`` 不该被当成「有待确认项」捞出来，否则
    启动恢复入口会去恢复一个没有待确认内容的 run。
    """
    ensure_tables()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT run_id, conv_id, iteration, created_at, updated_at"
            " FROM run_checkpoints"
            " WHERE pending_json IS NOT NULL"
            "   AND TRIM(pending_json) NOT IN ('', '{}', 'null')"
            " ORDER BY updated_at DESC, run_id DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def promote_waiting_checkpoint_runs() -> int:
    """启动对账第一步：崩溃在「快照落库 → 状态改写」之间的 run 救回。

    挂起流程的正常顺序是 save_checkpoint → update_agent_run(waiting_confirmation)。
    进程若死在这两步之间，run 滞留 running/init 但快照完好 —— 这样的 run
    必须先提升回 waiting_confirmation，否则随后的 mark_stale 会把它标成
    failed，用户看到「崩溃」而实际上他只是还有一张没答的卡。
    """
    ensure_tables()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE agent_runs SET status = 'waiting_confirmation'"
            " WHERE status IN ('running', 'init')"
            "   AND id IN (SELECT run_id FROM run_checkpoints"
            "              WHERE pending_json IS NOT NULL"
            "                AND TRIM(pending_json) NOT IN ('', '{}', 'null')"
            "                AND json_extract(pending_json, '$.state')"
            "                    IN ('pending', 'resuming'))"
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
