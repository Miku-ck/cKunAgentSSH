"""
文件变更追踪 — 记录 Agent 在运行过程中对文件系统的修改

设计目标:
- 在 write_file 等工具执行时记录变更前/后内容，计算行级 diff
- 按 agent_run_id 聚合，提供查询接口供 trace API 使用
- 行级 diff 简单实现（difflib），不依赖外部库
- 内存缓存 + 数据库持久化（避免长任务内存膨胀）
- 提供 unified diff 内容供前端展示具体改动
"""
import os
import difflib
import threading
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# 单文件 before/after 内容缓存上限（避免大文件吞噬内存）
_MAX_CONTENT_LINES = 2000


@dataclass
class FileChange:
    """单次文件变更记录"""
    agent_run_id: str
    file_path: str         # 相对路径（用于展示）
    abs_path: str          # 绝对路径（用于去重）
    operation: str         # "create" / "modify" / "delete"
    added_lines: int       # 新增行数
    removed_lines: int     # 删除行数
    before_size: int       # 变更前字节数（0 表示新建）
    after_size: int        # 变更后字节数（0 表示删除）
    created_at: str = ""   # ISO 时间戳
    # 保存 before/after 内容用于生成 unified diff（截断到 _MAX_CONTENT_LINES 行）
    before_content: list[str] = field(default_factory=list)
    after_content: list[str] = field(default_factory=list)


# ── 内存缓存：run_id -> {abs_path -> FileChange} ──
# 同一次运行内对同一文件的多次写入会合并（保留最终 diff）
_PENDING: dict[str, dict[str, FileChange]] = {}
# 说明：曾计划按 TTL 自动清理，但清理函数始终无调用方（死代码，已移除）；
# 当前由路由 finally 的 clear_file_changes 负责清理，进程重启后自然释放


def _read_file_lines(path: str) -> list[str]:
    """安全读取文件行，文件不存在返回空列表"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _truncate_lines(lines: list[str]) -> list[str]:
    """截断超长文件，避免内存膨胀"""
    if len(lines) <= _MAX_CONTENT_LINES:
        return lines
    head = lines[:_MAX_CONTENT_LINES // 2]
    tail = lines[-_MAX_CONTENT_LINES // 2:]
    omitted = len(lines) - _MAX_CONTENT_LINES
    return head + [f"... [已省略中间 {omitted} 行] ..."] + tail


def _compute_diff(before: list[str], after: list[str]) -> tuple[int, int]:
    """使用 difflib 计算新增/删除行数"""
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            removed += (i2 - i1)
            added += (j2 - j1)
        elif tag == "delete":
            removed += (i2 - i1)
        elif tag == "insert":
            added += (j2 - j1)
    return added, removed


def record_file_write(
    agent_run_id: str,
    abs_path: str,
    workspace_path: str,
    new_content: str,
) -> None:
    """
    在 WriteFileTool 写入文件前调用，记录文件变更。

    约定：调用此函数时，文件尚未被覆盖（仍是旧内容），new_content 是即将写入的新内容。
    这样可以同时拿到 before/after 计算行级 diff。

    Args:
        agent_run_id: Agent 运行 ID（空字符串时静默跳过）
        abs_path: 文件绝对路径
        workspace_path: 工作区根路径（用于计算相对路径展示）
        new_content: 即将写入的新内容
    """
    if not agent_run_id:
        return

    try:
        # 计算 before
        before_lines = _read_file_lines(abs_path)
        before_size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
        operation = "modify" if before_size > 0 else "create"

        # 计算 after
        after_lines = new_content.splitlines()
        after_size = len(new_content.encode("utf-8"))

        added, removed = _compute_diff(before_lines, after_lines)

        # 相对路径（展示用）
        rel_path = abs_path
        if workspace_path:
            try:
                rel_path = os.path.relpath(abs_path, workspace_path)
            except ValueError:
                rel_path = abs_path

        change = FileChange(
            agent_run_id=agent_run_id,
            file_path=rel_path,
            abs_path=abs_path,
            operation=operation,
            added_lines=added,
            removed_lines=removed,
            before_size=before_size,
            after_size=after_size,
            # 审查 MINOR 修复：统一 UTC 时间戳（带 Z 标记），与 database._now 的 UTC 语义一致，
            # 旧实现用 time.localtime() 生成无时区标记的本地时间，跨时区显示/排序错乱
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            before_content=_truncate_lines(before_lines),
            after_content=_truncate_lines(after_lines),
        )

        with _LOCK:
            run_map = _PENDING.setdefault(agent_run_id, {})
            # 同一次运行多次写同一文件：合并 diff（取最大累计值）
            if abs_path in run_map:
                prev = run_map[abs_path]
                # 以最后一次为准，但累加 diff 计数（粗略聚合）
                prev.added_lines = max(prev.added_lines, added)
                prev.removed_lines = max(prev.removed_lines, removed)
                prev.after_size = after_size
                prev.after_content = change.after_content  # 更新为最新内容
                # C-review M13：不覆盖 operation — 首次写入的 operation 反映 run 前
                # 状态（create→modify 合并后仍是 "create"，undo 才会正确删文件；
                # before_content/before_size 同样保留首次值 = run 前原始）
            else:
                run_map[abs_path] = change

    except Exception as e:
        logger.warning("[file_changes] 记录文件变更失败: %s", e)


def get_file_changes(agent_run_id: str) -> list[dict]:
    """获取某次 Agent 运行的所有文件变更，按路径排序

    注意：返回结果不含 before_content/after_content（避免 done 事件过大）。
    如需查看具体 diff，调用 get_file_diff(agent_run_id, abs_path)。
    """
    with _LOCK:
        run_map = _PENDING.get(agent_run_id, {})
        # 复制一份避免外部修改，排除 before_content/after_content
        changes = []
        for c in run_map.values():
            d = asdict(c)
            d.pop("before_content", None)
            d.pop("after_content", None)
            changes.append(d)
    changes.sort(key=lambda x: x["file_path"])
    return changes


def get_file_diff(agent_run_id: str, abs_path: str) -> dict:
    """获取某次运行中某文件的具体 unified diff

    返回:
        {
            "file_path": "src/main.py",
            "before": "原内容（合并后）",
            "after": "新内容",
            "unified_diff": "--- before\\n+++ after\\n@@ ...",
            "operation": "modify",
            "added_lines": 5,
            "removed_lines": 2,
        }
    """
    with _LOCK:
        run_map = _PENDING.get(agent_run_id, {})
        change = run_map.get(abs_path)
        if not change:
            return {}
        before = change.before_content
        after = change.after_content
        # 生成 unified diff
        diff_lines = list(difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{change.file_path}",
            tofile=f"b/{change.file_path}",
            lineterm="",
        ))
        return {
            "file_path": change.file_path,
            "abs_path": change.abs_path,
            "before": "\n".join(before),
            "after": "\n".join(after),
            "unified_diff": "\n".join(diff_lines),
            "operation": change.operation,
            "added_lines": change.added_lines,
            "removed_lines": change.removed_lines,
        }


def clear_file_changes(agent_run_id: str) -> None:
    """清理某次运行的内存缓存（运行结束后调用）"""
    with _LOCK:
        _PENDING.pop(agent_run_id, None)


def get_file_changes_summary(agent_run_id: str) -> dict:
    """获取变更汇总（文件数、总新增、总删除）

    优先读取内存缓存；若内存已清空（如进程重启），尝试从数据库恢复。
    """
    changes = get_file_changes(agent_run_id)
    if not changes:
        try:
            import database as db
            changes = db.get_agent_file_changes(agent_run_id)
        except Exception as e:
            logger.warning("[file_changes] 从数据库恢复变更失败: %s", e)
    total_added = sum(c["added_lines"] for c in changes)
    total_removed = sum(c["removed_lines"] for c in changes)
    return {
        "files_changed": len(changes),
        "added_lines": total_added,
        "removed_lines": total_removed,
        "changes": changes,
    }


def persist_file_changes(agent_run_id: str) -> None:
    """将内存中的文件变更写入数据库（运行结束时调用）

    C-review C3：直接从 _PENDING 取含 before/after_content 的完整数据落库
    （get_file_changes 会 pop 掉内容字段以控制 done 事件体积，不能用于持久化）。
    """
    if not agent_run_id:
        return
    try:
        import database as db
        with _LOCK:
            run_map = _PENDING.get(agent_run_id)
            if not run_map:
                return
            changes = [asdict(c) for c in run_map.values()]
        if changes:
            db.save_agent_file_changes(agent_run_id, changes)
    except Exception as e:
        logger.warning("[file_changes] 持久化变更失败: %s", e)


def load_file_changes(agent_run_id: str) -> list[dict]:
    """从数据库加载文件变更到内存缓存（供 undo/keep API 使用）"""
    if not agent_run_id:
        return []
    try:
        import database as db
        changes = db.get_agent_file_changes(agent_run_id)
        with _LOCK:
            run_map = _PENDING.setdefault(agent_run_id, {})
            for c in changes:
                # C-review C3：回填 before/after_content（DB 列，C3 迁移后存在），
                # 使进程重启后的 undo/diff 也有内容源
                before_content = (c.get("before_content") or "").splitlines() if c.get("before_content") else []
                after_content = (c.get("after_content") or "").splitlines() if c.get("after_content") else []
                change = FileChange(
                    agent_run_id=c.get("agent_run_id", agent_run_id),
                    file_path=c.get("file_path", ""),
                    abs_path=c.get("abs_path", ""),
                    operation=c.get("operation", "modify"),
                    added_lines=c.get("added_lines", 0) or 0,
                    removed_lines=c.get("removed_lines", 0) or 0,
                    before_size=c.get("before_size", 0) or 0,
                    after_size=c.get("after_size", 0) or 0,
                    created_at=c.get("created_at", ""),
                    before_content=before_content,
                    after_content=after_content,
                )
                run_map[change.abs_path] = change
        return changes
    except Exception as e:
        logger.warning("[file_changes] 加载持久化变更失败: %s", e)
        return []
