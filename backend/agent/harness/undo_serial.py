"""Undo 包序列化 — run 级一键回滚（功能 B）的编解码与可信度派生。

职责单一：把 Saddle 在 run 内构建的三种 Undo 包（ShellUndo / FileContentUndo /
NoOpUndo，见 ``saddle.py``）转成可落库、可回读的 dict，并从形态派生可信度。
本模块**不碰数据库**（那是 ``undo_store.py`` 的事）、**不执行反向操作**
（回滚以新 Agent run 走 POST /agent/run 全链；现有 apply_reverse 直连
ElectronBridge 是治理旁路，一键回滚绝不复用，见设计文档 §2.1 结论 1）。

为什么不直接在 saddle.py 的三个 dataclass 上加 to_dict：saddle 的职责是
「执行前构建 Undo 包」，序列化 + 可信度派生是回滚产品功能的语义，集中一处
避免 saddle 职责膨胀；本模块 import saddle 的类型，反向零依赖。

可信度派生（代码中无现成 confidence 字段，只能从形态推导，设计文档 §2.1）：
    snapshot       FileContentUndo 且 existed + snapshot —— 内容级还原，最可信
    derived        ShellUndo 且 reverse_command 非空 —— 已过 _shell_reverse 的
                   元字符拒绝 + 二次 classify_command 过滤（saddle.py:230-280）
    delete_created FileContentUndo 且 existed=False —— 反向是 rm -f 删除，
                   回滚会真删文件，UI 需反色提示
    manual         ShellUndo reverse=None / NoOpUndo / 未知形态 —— 不进回滚
                   任务，仅展示

子 Agent（spawn_subagent）的 ctx 独立 undo 栈，但工具面限 READONLY_TOOLS，
栈内恒为 NoOpUndo —— 无需合并进主 run，落库时各自独立记录即可（设计文档明示）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from agent.harness.contracts import NoOpUndo, RunContext
from agent.harness.saddle import FileContentUndo, ShellUndo

logger = logging.getLogger(__name__)

# kind 列的取值域（undo_store.run_undo_actions.kind 的约束来源）
KIND_SHELL = "shell"
KIND_FILE = "file"
KIND_NOOP = "noop"

# confidence 列的取值域
CONF_SNAPSHOT = "snapshot"
CONF_DERIVED = "derived"
CONF_DELETE_CREATED = "delete_created"
CONF_MANUAL = "manual"

# FileContentUndo 只可能由这两个工具产生（UndoBuilder._build_file_undo 的调用面）
_FILE_TOOLS = ("file_write", "file_edit")
# ShellUndo 只可能由 shell_exec 产生（UndoBuilder.build 的调用面）
_SHELL_TOOL = "shell_exec"


def derive_confidence(undo: Any) -> str:
    """从 Undo 包形态派生可信度（fail-closed：认不出的形态一律 manual）。"""
    if isinstance(undo, FileContentUndo):
        # 空文件快照（b64=""）也是内容级证据：existed=True 且 snapshot 非 None 即可
        if undo.existed and undo.snapshot_b64 is not None:
            return CONF_SNAPSHOT
        if not undo.existed:
            # 反向是 rm -f —— 回滚会真删文件
            return CONF_DELETE_CREATED
        # existed=True 却没有快照（不应出现的形态）：内容级还原无从谈起，
        # 按 manual 处理，宁可少回滚不可错回滚
        return CONF_MANUAL
    if isinstance(undo, ShellUndo):
        return CONF_DERIVED if undo.reverse_command else CONF_MANUAL
    return CONF_MANUAL


def undo_to_dict(undo: Any) -> dict[str, Any]:
    """单个 Undo 包 → 可 JSON 序列化的 dict（三种实现字段全为 str/bool/None）。"""
    if isinstance(undo, ShellUndo):
        kind = KIND_SHELL
        body: dict[str, Any] = {
            "command": undo.command or "",
            "reverse_command": undo.reverse_command,
            "path": None,
            "existed": None,
            "snapshot_b64": None,
            "reason": None,
        }
    elif isinstance(undo, FileContentUndo):
        kind = KIND_FILE
        body = {
            "command": "",
            "reverse_command": None,
            "path": undo.path or "",
            # existed=False = 原文件不存在，反向是 rm -f；snapshot 单独一列
            "existed": bool(undo.existed),
            "snapshot_b64": undo.snapshot_b64,
            "reason": None,
        }
    else:
        # NoOpUndo 与一切未知形态：fail-closed 按空操作记录
        reason = getattr(undo, "reason", None)
        kind = KIND_NOOP
        body = {
            "command": "",
            "reverse_command": None,
            "path": None,
            "existed": None,
            "snapshot_b64": None,
            "reason": reason if isinstance(reason, str) else "unknown",
        }
    body["kind"] = kind
    body["confidence"] = derive_confidence(undo)
    return body


def undo_from_dict(data: dict[str, Any]) -> Optional[Any]:
    """dict → Undo 包（还原为原类型；只用于把落库行交回内存形态的场景）。

    识别不了 kind / 形态残缺时返回 None —— 调用方不得把它当 NoOpUndo 静默
    放行（NoOp 的语义是「只读无副作用」，而残缺行可能是快照损坏的 file 行）。
    """
    kind = data.get("kind")
    try:
        if kind == KIND_SHELL:
            return ShellUndo(
                command=str(data.get("command") or ""),
                reverse_command=data.get("reverse_command"),
                ssh_session_id=str(data.get("ssh_session_id") or ""),
            )
        if kind == KIND_FILE:
            existed = data.get("existed")
            snapshot = data.get("snapshot_b64")
            return FileContentUndo(
                path=str(data.get("path") or ""),
                ssh_session_id=str(data.get("ssh_session_id") or ""),
                # 空串是合法快照（0 字节文件），与 None（不存在快照）语义不同
                snapshot_b64=str(snapshot) if snapshot is not None else None,
                existed=bool(existed),
            )
        if kind == KIND_NOOP:
            return NoOpUndo(reason=str(data.get("reason") or "unknown"))
    except (TypeError, ValueError):
        return None
    return None


def _tool_names_for_stack(ctx: RunContext) -> list[str]:
    """为栈内每个 Undo 条目找出产生它的工具名（best-effort，取不到留空）。

    对齐精度说明（设计文档 §6.2 明示未做运行时全量验证）：
    - ShellUndo 只可能来自 shell_exec、FileContentUndo 只可能来自
      file_write/file_edit（UndoBuilder.build 的调用面）—— 这两类工具名由
      kind 直接决定，不依赖日志对齐；
    - NoOpUndo 的产生者可能是任何工具（只读观测、job_run、被 Saddle 沙箱
      拦截的调用等），只能按序从 ctx.tool_calls_log 里挑未消费条目；
    - 工具失败回滚（remove_undo）与 pending_delivery 出栈会让「日志条目数
      ≥ 栈条目数」，此时按未消费顺序近似对齐，偏差只影响展示用的 tool 名
      字段，不影响 kind/confidence 等回滚判定字段。
    """
    log = [
        e for e in (ctx.tool_calls_log or [])
        if isinstance(e, dict) and not e.get("intercept_reason")
    ]
    used = [False] * len(log)
    names: list[str] = []
    for undo in ctx.undo_stack or []:
        if isinstance(undo, ShellUndo):
            names.append(_SHELL_TOOL)
            continue
        if isinstance(undo, FileContentUndo):
            idx = next(
                (i for i, e in enumerate(log)
                 if not used[i] and e.get("tool") in _FILE_TOOLS),
                None,
            )
            if idx is not None:
                used[idx] = True
                names.append(str(log[idx].get("tool") or ""))
            else:
                # 日志里找不到同 kind 的条目（极端：日志被截断），退回
                # kind 决定的默认名，不留误导性的空串
                names.append(_FILE_TOOLS[0])
            continue
        idx = next((i for i, e in enumerate(log) if not used[i]), None)
        if idx is not None:
            used[idx] = True
            names.append(str(log[idx].get("tool") or ""))
        else:
            names.append("")
    return names


def snapshot_stack(ctx: RunContext) -> list[dict[str, Any]]:
    """把 ctx.undo_stack 快照为一组可落库的行 dict（纯同步、纯内存）。

    必须在 run 终态、FAILED 分支 undo_all 之前调用 —— FAILED 的栈会先被
    执行再清空（driver._emit_done → stirrups.undo_all），之后快照只能拿到
    空栈。栈内此时已是「净副作用」：失败工具与 pending_delivery 的条目已被
    saddle 按身份移除；只读工具的 NoOpUndo 照实保留（kind=noop、confidence
    =manual，回滚计划会排除它们，但审计展示要诚实）。
    """
    tool_names = _tool_names_for_stack(ctx)
    host = ""
    try:
        host = str((ctx.metadata or {}).get("target_host") or "")
    except Exception:
        host = ""
    ssh_session_id = str(getattr(ctx, "ssh_session_id", "") or "")
    environment = str(getattr(ctx, "environment", "test") or "test")

    rows: list[dict[str, Any]] = []
    for seq, undo in enumerate(ctx.undo_stack or []):
        row = undo_to_dict(undo)
        row.update({
            "seq": seq,
            "tool": tool_names[seq] if seq < len(tool_names) else "",
            "ssh_session_id": ssh_session_id,
            "host": host,
            "environment": environment,
        })
        rows.append(row)
    return rows
