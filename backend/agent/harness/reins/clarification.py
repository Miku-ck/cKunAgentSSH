"""澄清向导（ClarificationGate）— pending 存储、检测器/题集与拦截器 — 拆分自原 reins.py。

设计要点（与原实现一致）：一个 run 最多一道进行中澄清题；detect_vague 返回 None
表示意图明确零打扰；前端本地推进多步后一次性 POST；skip = 空答案放行。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..contracts import (
    READONLY_TOOLS,
    ClarificationOption,
    ClarificationStep,
    ReinDecision,
    ReinPoint,
    RunContext,
    TraceEvent,
)
from ...clarification_policy import (
    KIND_INTENT,
    KIND_RISK,
    KIND_SCOPE,
    OUTCOME_ANSWERED,
    OUTCOME_EXPIRED,
    OUTCOME_SKIPPED,
    RISK_HIGH,
    classify_tool_risk,
    decide_policy,
    skip_policy_for_tool,
)
from ..safety import classify_command, classify_constructed_command
from .core import (
    AWAIT_APPROVAL_KEY,
    command_from_args,
    extract_bound_command,
    resolve_registry,
    tool_call_signature,
)

logger = logging.getLogger(__name__)


def _mark_pending_sig(ctx: RunContext) -> None:
    """审查R2修复：把等待中的工具调用签名登记给 LoopDetector 豁免。"""
    tc = ctx.active_tool_call
    if tc is not None:
        ctx.metadata.setdefault("clarification_pending", set()).add(
            tool_call_signature(tc))

# 澄清向导 — 全局 pending 存储（供 routes 层写入用户回答）
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# 澄清向导（ClarificationGate）
#
# 设计要点：
# - 一个 run 最多一道进行中澄清题（简化：按 run_id 键控）
# - 仅在工具调用"意图不明"且 detect_vague 返回非空时触发；精准调用零打扰
# - 前端在本地推进多步（A/B/C 卡片 + D 自定义输入），最终一次性 POST 答案
# - POST /agent/clarification/answer 写 _CLARIFICATION_DONE → 下一轮 PRE_TOOL_CALL
#   注入答案上下文 + 放行工具（LLM 按澄清结果重规划）
# - 跳过（skip）= 全部留空 → 放行原调用走既有权限门控
# ──────────────────────────────────────────────────────────────────────────────


# run_id → 已回答答案 dict {step_idx: {choice, custom_text}}
_CLARIFICATION_DONE: dict[str, dict] = {}
# run_id → 整治中题元信息（含 steps）；_done=True 表示答案就绪待注入
_CLARIFICATION_CONTEXT: dict[str, dict] = {}

# P1-5: 全局 pending 容量上限，超限时 FIFO 淘汰最老条目（防内存泄漏）
_CLARIFICATION_MAX_ENTRIES = 128
# P1-6: 未完成的澄清 10 分钟过期 — 用户中途关窗/跑题后，
# 残留 pending 题不再让后续 PRE_TOOL_CALL 无限重发拦截
_CLARIFICATION_TTL_SEC = 600


def _purge_expired_clarifications() -> int:
    """只回收孤儿（done 有、context 无），绝不按 ts 删 context。

    永久等待语义（2026-11）：澄清题没有 TTL —— 用户什么时候答都算数。
    原实现按 600s 清 context，会把用户还没答的题删掉；现在唯一允许的
    清理是「答案条目没有对应 context」的孤儿回收（消费完成后的残留）。
    """
    orphans = [rid for rid in _CLARIFICATION_DONE
               if rid not in _CLARIFICATION_CONTEXT]
    for rid in orphans:
        _CLARIFICATION_DONE.pop(rid, None)
    return len(orphans)


# run_id → 已被用户取消的 run（挂起后用户点了「取消任务」）。
# 挂起模型下 run task 已返回，内存态通常已被释放；这个集合只服务
# 「取消发生在答案消费之前的竞态窗口」，DB 的 pending_json.state 才是权威。
_CLARIFICATION_CANCEL: set[str] = set()


def cancel_clarification(run_id: str) -> bool:
    """用户取消整个任务：登记取消哨兵 + 回收本题内存态。

    返回是否存在过未完成/未消费的题。DB 侧（pending_json.state=cancelled、
    run 置 CANCELLED 终态）由路由层负责，这里只管内存。
    """
    _CLARIFICATION_CANCEL.add(run_id)
    had = _CLARIFICATION_CONTEXT.pop(run_id, None) is not None
    had = _CLARIFICATION_DONE.pop(run_id, None) is not None or had
    return had


def build_clarification_pending(run_id: str) -> Optional[dict]:
    """把当前待确认题序列化为可落库的 payload（挂起快照用）。

    字段与 clarification_needed 事件 payload 对齐（前端恢复卡片直接复用），
    另带重放所需的重放索引信息由 driver 在 checkpoint 层补写。
    risk = safe|moderate|dangerous（前端只读这个）；澄清矩阵的
    low|medium|high 放 policy_risk，前端不要读。
    """
    ctx_info = _CLARIFICATION_CONTEXT.get(run_id)
    if not ctx_info or ctx_info.get("_done"):
        return None
    policy_risk = str(ctx_info.get("risk") or RISK_HIGH)
    # 前端权限卡口径：low→safe / medium→moderate / high→dangerous
    card_risk = {"low": "safe", "medium": "moderate"}.get(policy_risk, "dangerous")
    steps = ctx_info.get("steps") or []
    return {
        "kind": str(ctx_info.get("kind") or KIND_SCOPE),
        "tool": str(ctx_info.get("tool") or ""),
        "tool_call_id": str(ctx_info.get("tool_call_id") or ""),
        "owner_tool_call_id": str(ctx_info.get("owner_tool_call_id") or ""),
        "args": ctx_info.get("args") or {},
        "command": str((ctx_info.get("args") or {}).get("command") or ""),
        "risk": card_risk,
        "policy_risk": policy_risk,
        "steps": [_step_to_dict(s) for s in steps],
        "step": int(ctx_info.get("current_step") or 0),
        "total": int(ctx_info.get("total") or len(steps)),
        "reason": str(ctx_info.get("reason") or ""),
        "created_at": time.time(),
    }


def reinject_pending(run_id: str, pending: dict) -> bool:
    """把落库的待确认题回填进内存态（resume 用），已存在则覆盖。

    恢复路径的答案消费依赖 _CLARIFICATION_CONTEXT 存在；进程重启后内存
    为空，必须先回填。answers 为空 dict 是合法输入（等价跳过）—— 但
    永久等待语义下跳过按钮已删，空 answers 只在「用户明确取消」时出现。
    """
    if not isinstance(pending, dict) or not pending.get("tool_call_id"):
        return False
    steps_raw = pending.get("steps") or []
    steps: list = []
    for s in steps_raw:
        if isinstance(s, dict):
            steps.append(ClarificationStep(
                question=str(s.get("question") or ""),
                options=[ClarificationOption(
                    id=str(o.get("id") or ""), label=str(o.get("label") or ""),
                ) for o in (s.get("options") or [])],
                allow_custom=bool(s.get("allow_custom", True)),
            ))
        else:
            steps.append(s)
    _CLARIFICATION_CONTEXT[run_id] = {
        "tool": str(pending.get("tool") or ""),
        "args": pending.get("args") or {},
        "tool_call_id": str(pending.get("tool_call_id") or ""),
        "owner_tool_call_id": str(pending.get("owner_tool_call_id") or ""),
        "kind": str(pending.get("kind") or KIND_SCOPE),
        "risk": str(pending.get("policy_risk") or RISK_HIGH),
        "policy": str(pending.get("skip_policy") or ""),
        "skip_policy": str(pending.get("skip_policy") or ""),
        "steps": steps,
        "current_step": int(pending.get("step") or 0),
        "total": int(pending.get("total") or len(steps)),
        "reason": str(pending.get("reason") or ""),
        "_done": False,
        "ts": time.time(),
    }
    _enforce_clarification_cap()
    return True


def record_clarification_answers(
    run_id: str, answers: dict, tool_call_id: str = "",
) -> str:
    """写入答案；票据必须与当前 pending 匹配。

    返回 "recorded" | "rejected"。不补桩、不创建孤儿答案。
    """
    ctx_info = _CLARIFICATION_CONTEXT.get(run_id)
    if not ctx_info:
        # 挂起模型下：题随 run 挂起而离开内存（或 run 已取消/已恢复），
        # 对调用方这是一张已失效的卡，与「票据不匹配」的参数错误区分开。
        return "gone"
    ticket = str(ctx_info.get("tool_call_id") or "")
    if tool_call_id and str(tool_call_id or "") != ticket:
        return "rejected"

    steps = ctx_info.get("steps") or []
    if not isinstance(answers, dict):
        return "rejected"
    for key, item in answers.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            return "rejected"
        if idx < 0 or idx >= len(steps) or not isinstance(item, dict):
            return "rejected"
        choice = item.get("choice", "")
        if choice == "custom":
            continue
        step = steps[idx]
        options = step.get("options") if isinstance(step, dict) else getattr(step, "options", None)
        ids = {
            (o.get("id") if isinstance(o, dict) else getattr(o, "id", ""))
            for o in (options or [])
        }
        if choice not in ids:
            return "rejected"

    _CLARIFICATION_DONE[run_id] = answers
    ctx_info["_done"] = True
    ctx_info["ts"] = time.time()
    _enforce_clarification_cap()
    return "recorded"


def peek_clarification_context(run_id: str) -> Optional[dict]:
    """只读查看 pending，供 timeout 分支计算同一份 risk/policy。"""
    ctx_info = _CLARIFICATION_CONTEXT.get(run_id)
    return dict(ctx_info) if ctx_info else None


def release_clarification_run(run_id: str) -> bool:
    """run 终态回收该 run 的澄清状态（未答完的题随 run 作废）"""
    had = _CLARIFICATION_CONTEXT.pop(run_id, None) is not None
    had = _CLARIFICATION_DONE.pop(run_id, None) is not None or had
    return had


async def wait_for_clarification_answer(run_id: str) -> Optional[dict]:
    """阻塞等待工具级澄清答案（Driver 澄清等待闸使用）。

    返回答案 dict（可能为空 = 用户点击跳过）；返回 None 表示 TTL 到期或
    pending 被回收（run 终止 / 断连 / release）—— 调用方应落回原拦截路径。
    取消（CancelledError）原样上抛，由 Driver 的取消链收尾。

    与 intent_resolver 的任务级等待同源：答案由路由层写入 _CLARIFICATION_DONE，
    这里只轮询这一个事实，不额外维护唤醒通道（跨线程/跨请求写入，轮询最稳）。
    """
    deadline = time.monotonic() + _CLARIFICATION_TTL_SEC
    while time.monotonic() < deadline:
        if run_id in _CLARIFICATION_DONE:
            return _CLARIFICATION_DONE[run_id]
        if run_id not in _CLARIFICATION_CONTEXT:
            return None
        await asyncio.sleep(0.2)
    return None


def _enforce_clarification_cap():
    """FIFO 淘汰最老的 done/context 条目，保持总规模可控"""
    while len(_CLARIFICATION_DONE) > _CLARIFICATION_MAX_ENTRIES:
        try:
            oldest = next(iter(_CLARIFICATION_DONE))
            _CLARIFICATION_DONE.pop(oldest, None)
        except StopIteration:
            break
    while len(_CLARIFICATION_CONTEXT) > _CLARIFICATION_MAX_ENTRIES:
        try:
            oldest = next(iter(_CLARIFICATION_CONTEXT))
            _CLARIFICATION_CONTEXT.pop(oldest, None)
        except StopIteration:
            break


# ── 任务级澄清（Phase 1 意图级联，intent_resolver 消费）──
# 与工具级共用 pending 存储 / TTL / 容量治理，但生命周期完全不同：
# 登记发生在 driver 主循环启动之前（意图识别点），消费/回收也全部在那
# 一处完成 —— ClarificationInterceptor 永远不会见到未消费的任务级题
# （PRE_TOOL_CALL 首次触发时 pending 早已 pop），故无需改动拦截器。

# 任务级澄清的展示名：前端向导头部的命令标签行（tool 字段直出）。
# 不用 "__task__" 之类的内部记号 —— 用户看得见。
TASK_CLARIFICATION_TOOL = "任务意图"


def register_task_clarification(
    run_id: str,
    steps: list,
    reason: str,
    task_preview: str,
    *,
    kind: str = KIND_INTENT,
    risk: str = "medium",
    skip_policy: str = "",
) -> str:
    """登记任务级澄清题，返回 tool_call_id（uuid 防并发碰撞）。"""
    tool_call_id = f"clarify_{_uuid.uuid4().hex[:12]}"
    _CLARIFICATION_CONTEXT[run_id] = {
        "tool": TASK_CLARIFICATION_TOOL,
        "args": {"command": task_preview},
        "tool_call_id": tool_call_id,
        "owner_tool_call_id": "",
        "kind": kind,
        "risk": risk,
        "policy": skip_policy,
        "skip_policy": skip_policy,
        "steps": steps,
        "current_step": 0,
        "total": len(steps),
        "reason": reason,
        "_done": False,
        "ts": time.time(),
    }
    _enforce_clarification_cap()
    return tool_call_id


def pop_task_clarification(run_id: str) -> tuple:
    """一次性消费任务级澄清状态，返回 (题元, 答案)；都可能是 None。

    与工具级的关键差异：任务级在注册后到消费前**没有**任何人会来读这两
    个字典（driver 主循环未启动），所以必须自己负责弹出 —— 残留的
    context 会被 TTL 清扫回收，残留的 done 则会污染拦截器的答案归属判断。
    """
    ctx_info = _CLARIFICATION_CONTEXT.pop(run_id, None)
    answers = _CLARIFICATION_DONE.pop(run_id, None)
    return ctx_info, answers


def _shell_detect_vague(args: dict) -> Optional[str]:
    """shell_exec 意图模糊判定：命令含破坏性动词 + 通配符/递归且未指定明确目标。

    Returns:
        模糊原因（触发澄清）或 None（目标明确 → 不触发）。
    """
    if not args or not isinstance(args, dict):
        return None
    cmd = command_from_args(args)
    if not cmd or not isinstance(cmd, str):
        return None
    lower = cmd.strip().lower()
    # 1) 含破坏性动词（P1-2: find 已从 _DESTRUCTIVE 移除——只读命令不触发；
    #    仅当 find 管道接 xargs 时视为潜在破坏性，在下方单独判定）
    _DESTRUCTIVE = ("rm ", "rm\t", "rmdir ", "rmdir\t", "xargs ", "chmod", "chown")
    if not any(v in lower for v in _DESTRUCTIVE):
        if "find " in lower and "| xargs" in lower:
            pass  # find ... | xargs → 继续走后续递归/通配符判断
        else:
            return None
    # 2) 含通配符才视为范围不明；精确路径的 rm -rf 是明确命令，不拦。
    #    find ... | xargs 没有通配符但目标来自管道，仍视为范围不明。
    has_wildcard = any(c in cmd for c in ("*", "?"))
    if not has_wildcard:
        if "find " in lower and "| xargs" in lower:
            return f"命令 {cmd!r} 通过管道批量处理，影响范围不确定"
        return None
    return f"命令 {cmd!r} 使用通配符操作但未指定具体目标文件，影响范围不确定"


def _shell_build_steps(args: dict) -> list[ClarificationStep]:
    """shell/job 只问一个阻塞问题：影响范围。处置与汇报使用安全默认。"""
    cmd = command_from_args(args)
    cmd_display = (cmd[:40] + "…") if len(str(cmd)) > 40 else str(cmd)
    return [
        ClarificationStep(
            question=f"\"{cmd_display}\" 在目标机上的作用范围是？",
            options=[
                ClarificationOption(id="A", label="仅当前目录一层，不递归子目录"),
                ClarificationOption(id="B", label="包含全部子目录（按命令中的递归语义执行）"),
                ClarificationOption(id="C", label="我指定确切路径（在自定义输入中列出）"),
            ],
            allow_custom=True,
        ),
    ]


# ── file_write：覆写系统配置文件 ──

_SENSITIVE_WRITE_ROOTS = ("/etc/", "/boot/", "/usr/lib/systemd/", "/lib/systemd/",
                          "/usr/share/", "/proc/", "/sys/")


def _file_write_detect_vague(args: dict) -> Optional[str]:
    """file_write 意图模糊判定：整体覆写（reversible=False）系统配置路径。

    LLM 覆写 /etc 等系统文件时常见缺口是不做备份、不处理配置生效方式
    （reload/restart）——权限卡只回答"能不能写"，这里补齐"怎么写才安全"。
    """
    if not args or not isinstance(args, dict):
        return None
    path = str(args.get("path") or "")
    if not path:
        return None
    if path.startswith(_SENSITIVE_WRITE_ROOTS) or path in ("/etc", "/boot"):
        return f"即将整体覆写系统配置文件 {path!r}，备份策略与配置生效方式未明确"
    return None


def _file_write_build_steps(args: dict) -> list[ClarificationStep]:
    path = ""
    if isinstance(args, dict):
        path = str(args.get("path") or "")
    path_display = (path[:48] + "…") if len(path) > 48 else path
    return [
        ClarificationStep(
            question=f"覆写 {path_display!r} 前是否备份原文件？",
            options=[
                ClarificationOption(id="A", label="先 cp 备份为 <文件>.bak-<时间戳> 再写入（推荐）"),
                ClarificationOption(id="B", label="无需备份（新部署/文件可丢弃）"),
                ClarificationOption(id="C", label="我指定备份方式或路径（在自定义输入中说明）"),
            ],
            allow_custom=True,
        ),
        ClarificationStep(
            question="写入完成后如何处理配置生效？",
            options=[
                ClarificationOption(id="A", label="仅写入，服务重载/重启由用户手动执行"),
                ClarificationOption(id="B", label="写入后执行对应的低影响 reload（如 systemctl reload）"),
                ClarificationOption(id="C", label="写入后先用只读命令校验语法（如 nginx -t / sshd -t）再决定是否 reload"),
            ],
            allow_custom=True,
        ),
    ]


# ── service_manage：stop/restart 会话与网络关键服务 ──

# 停这些服务可能直接掐断当前 SSH 会话、阻断网络/防火墙或波及全部容器 —
# 与权限卡（能不能停）正交，这里问清影响确认与恢复核验（要不要停、停了怎么收尾）
_SESSION_CRITICAL_SERVICES = frozenset({
    "sshd", "ssh", "systemd-logind", "NetworkManager", "network",
    "firewalld", "iptables", "nftables", "docker", "kubelet",
})


def _service_detect_vague(args: dict) -> Optional[str]:
    """service_manage 意图模糊判定：stop/restart 会话/网络关键服务。

    name 已被工具层白名单正则约束（无通配符），因此模糊点不在目标标识，
    而在"是否知道这会掐断自己的连接/影响整机容器"。
    """
    if not args or not isinstance(args, dict):
        return None
    action = str(args.get("action") or "")
    name = str(args.get("name") or "").rstrip()
    if action not in ("stop", "restart"):
        return None
    base = name.removesuffix(".service") if name else ""
    if base in _SESSION_CRITICAL_SERVICES:
        return (
            f"{action} {name!r} 属于会话/网络关键服务，"
            "可能造成当前 SSH 连接中断或整机服务受影响，影响确认与恢复方案未明确"
        )
    return None


def _service_build_steps(args: dict) -> list[ClarificationStep]:
    action = str(args.get("action") or "") if isinstance(args, dict) else ""
    name = str(args.get("name") or "") if isinstance(args, dict) else ""
    return [
        ClarificationStep(
            question=f"{action} {name!r} 可能中断当前 SSH 会话或相关依赖，确认影响范围？",
            options=[
                ClarificationOption(id="A", label="确认执行，我了解可能断开连接/影响依赖服务"),
                ClarificationOption(id="B", label="先只查状态（status），暂不 stop/restart"),
                ClarificationOption(id="C", label="换执行方式（在自定义输入中说明，如预约维护窗口）"),
            ],
            allow_custom=True,
        ),
        ClarificationStep(
            question="执行前是否需要预检？",
            options=[
                ClarificationOption(id="A", label="直接执行，无需预检"),
                ClarificationOption(id="B", label="先用只读命令（systemctl status/依赖查看）确认当前状态"),
                ClarificationOption(id="C", label="我指定预检内容（在自定义输入中说明）"),
            ],
            allow_custom=True,
        ),
        ClarificationStep(
            question="执行后如何收尾？",
            options=[
                ClarificationOption(id="A", label="直接结束，无需汇报"),
                ClarificationOption(id="B", label="核验服务状态（systemctl status / 端口监听）并汇报"),
                ClarificationOption(id="C", label="若为 restart，等待恢复并确认可再次连接后汇报"),
            ],
            allow_custom=True,
        ),
    ]


# 可澄清注册表：tool_name → (detect_vague(args)->Optional[str], build_steps(args)->list[ClarificationStep])
# detect_vague 返回 None 表示意图明确 → 跳过澄清直达执行
_CLARIFIABLE_TOOLS: dict[str, tuple] = {
    "shell_exec": (_shell_detect_vague, _shell_build_steps),
    # job_run 与 shell_exec 同属命令入口（cmd 参数），复用检测器；
    # 此前仅注册 shell_exec 导致后台任务里的 rm -rf * 不澄清（2026-09 演进补齐）
    "job_run": (_shell_detect_vague, _shell_build_steps),
    "file_write": (_file_write_detect_vague, _file_write_build_steps),
    "service_manage": (_service_detect_vague, _service_build_steps),
}


def _step_to_dict(s: ClarificationStep) -> dict:
    return {
        "question": s.question,
        "options": [{"id": o.id, "label": o.label} for o in s.options],
        "allow_custom": s.allow_custom,
    }


def _build_clarification_needed(run_id: str, tool: str, args: dict,
                                steps: list[ClarificationStep], step: int,
                                total: int, tool_call_id: str,
                                reason: str = "", *, kind: str = KIND_SCOPE,
                                risk: str = "", skip_policy: str = "",
                                owner_tool_call_id: str = "") -> TraceEvent:
    return TraceEvent(
        kind="clarification_needed",
        source="reins.clarification",
        run_id=run_id,
        payload={
            "run_id": run_id,
            "tool": tool,
            "tool_call_id": tool_call_id,
            "owner_tool_call_id": owner_tool_call_id,
            "args": args,
            "step": step,
            "total": total,
            "kind": kind,
            "risk": risk,
            "skip_policy": skip_policy,
            "reason": reason,
            "steps": [_step_to_dict(s) for s in steps],
        },
    )


def _step_field(step: object, name: str) -> str:
    """steps 里存的可能是 ClarificationStep 对象，也可能是 dict（历史调用方两种都传）"""
    if step is None:
        return ""
    if isinstance(step, dict):
        value = step.get(name)
    else:
        value = getattr(step, name, None)
    return value if isinstance(value, str) else ""


def _option_label(step: object, choice: str) -> str:
    """把答案字母还原成用户当时看见的选项文字。

    向导的选项只存在于 `clarification_needed` 的 SSE payload 里（前端本地推进），
    模型侧从头到尾只收到过一句「一个澄清问题正在等待用户回答」。所以注入文本里
    只写 `Q1: A` 等于把用户的答案压缩成模型无法解读的字母 —— 实测原注入就是
    "  Q1: A" / "  Q2: B" 两行，整条澄清回路对模型不产生任何可用信息。
    """
    if step is None or not choice:
        return ""
    options = step.get("options") if isinstance(step, dict) else getattr(step, "options", None)
    for opt in options or []:
        oid = opt.get("id") if isinstance(opt, dict) else getattr(opt, "id", None)
        if oid == choice:
            label = opt.get("label") if isinstance(opt, dict) else getattr(opt, "label", "")
            return label if isinstance(label, str) else ""
    return ""


def _format_answers(answers: dict, steps: Optional[list] = None) -> str:
    """把答案 dict 格式化为一段用户消息（注入 LLM 让模型按澄清结果重规划）

    带出问题原文 + 选项文字，让模型（以及只拿到这段文本的审计/前端）不必回到向导
    也能读懂「A」是什么。字母保留在前面，与 UI 上的选择一一对应。
    """
    if not answers:
        return "用户选择跳过，原操作不执行。"
    steps = steps or []
    lines = ["用户已回答澄清问题："]
    for step_idx in sorted(answers.keys(), key=lambda x: int(x)):
        a = answers[step_idx] if isinstance(answers[step_idx], dict) else {"choice": str(answers[step_idx])}
        choice = a.get("choice", "")
        custom_text = a.get("custom_text")
        idx = int(step_idx)
        step = steps[idx] if 0 <= idx < len(steps) else None
        question = _step_field(step, "question")
        head = f"  Q{idx + 1}" + (f"「{question}」" if question else "")
        label = _option_label(step, choice)
        lines.append(f"{head} → {choice}. {label}" if label else f"{head} → {choice}")
        if custom_text:
            lines[-1] += f"（用户补充：{custom_text}）"
    return "\n".join(lines)


def consume_answer_decision(ctx: RunContext, outcome_override: str = "") -> Optional[ReinDecision]:
    """消费答案并生成“作废原调用”的决策。

    恒 intercepted=True：答案只决定原调用作废及重规划，不是执行许可；执行许可
    必须由下一轮新 tool call 重新经过完整 reins/权限链。outcome_override="expired"
    由 driver 超时路径使用。
    """
    run_id = ctx.run_id
    ctx_info = _CLARIFICATION_CONTEXT.get(run_id)
    if not ctx_info:
        return None
    if not outcome_override and not ctx_info.get("_done"):
        return None
    if not outcome_override and run_id not in _CLARIFICATION_DONE:
        return None

    answers = _CLARIFICATION_DONE.pop(run_id, {}) or {}
    ctx_info = _CLARIFICATION_CONTEXT.pop(run_id, {}) or ctx_info
    outcome = outcome_override or (OUTCOME_ANSWERED if answers else OUTCOME_SKIPPED)
    risk = str(ctx_info.get("risk") or RISK_HIGH)
    policy = decide_policy(risk, outcome)
    ticket = str(ctx_info.get("tool_call_id") or "")
    owner = str(ctx_info.get("owner_tool_call_id") or "")
    summary = _format_answers(answers, (ctx_info.get("steps") or []))
    if outcome == OUTCOME_EXPIRED:
        summary = "等待澄清超时，原操作不执行。"
    skip_reason = {
        OUTCOME_SKIPPED: "clarification_skipped",
        OUTCOME_EXPIRED: "clarification_timeout",
    }.get(outcome, "")
    if outcome == OUTCOME_ANSWERED:
        content = f"[用户澄清结果]\n{summary}\n请按上述澄清范围重新规划；不要重试原始工具调用。"
    elif outcome == OUTCOME_EXPIRED:
        content = "[用户澄清结果]\n等待用户回答超时，原操作不执行。请重新规划安全替代方案。"
    else:
        content = "[用户澄清结果]\n用户选择跳过，原操作不执行。请重新规划安全替代方案。"
    inject = [{"role": "user", "content": content}]
    ctx.metadata["clarification_abandoned_turn"] = {
        "tool_call_id": ticket,
        "owner_tool_call_id": owner,
        "outcome": outcome,
        "policy": policy,
        "skip_reason": skip_reason,
        "inject_messages": inject,
    }
    ctx.metadata.pop("clarification_pending", None)
    # 取消哨兵已消费完毕（无论本题最终走向哪个 outcome），清掉防泄漏；
    # 真正的 run 终止由路由层写 DB 终态，这里只管内存回收。
    _CLARIFICATION_CANCEL.discard(run_id)
    ev = TraceEvent(
        kind="clarification_done",
        source="reins.clarification",
        run_id=run_id,
        iteration=ctx.iteration,
        payload={
            "run_id": run_id,
            "tool": ctx_info.get("tool", ""),
            "tool_call_id": ticket,
            "owner_tool_call_id": owner,
            "outcome": outcome,
            "policy": policy,
            "answers": answers,
            "summary": summary,
            "steps_total": len(ctx_info.get("steps") or []),
            "expired": outcome == OUTCOME_EXPIRED,
        },
    )
    return ReinDecision(
        intercepted=True,
        intercept_reason=f"clarification_abandoned: {outcome}",
        events=[ev],
        inject_messages=inject,
    )



@dataclass
class ClarificationInterceptor:
    """澄清向导拦截器 — 对 PRE_TOOL_CALL 的不明意图工具前置问答收窄范围。

    触发流程：
    1. 首个 PRE_TOOL_CALL（本次工具刚被 append 进 tool_calls_log[-1]）
       → detect_vague 通过 + detect_vague 返回非空
       → 构建 Pending 题元，发 clarification_needed(step=0) + inject 等待提示
       → intercepted=True（本轮该工具不执行）
    2. 等待间再次 PRE_TOOL_CALL（LLM 重试同一工具时）
       → 重发当前 step 的 clarification_needed + inject 等待提示
       → intercepted=True（仍不执行）
    3. POST /agent/clarification/answer → _CLARIFICATION_DONE[run_id]=answers
       随后 PRE_TOOL_CALL → 注入 answers 上下文 + clarification_done 事件
       → intercepted=False（LLM 按澄清结果重规划后放行本工具）
    """
    point: ReinPoint = field(default=ReinPoint.PRE_TOOL_CALL, init=False)
    # 7 < PermissionGate(8) < FilesystemGuard(10)：先问清意图，再走授权/阻断
    priority: int = 7
    # 算绑定命令用（saddle.registry）；向导不再判断 allowlist，故不需要权限门。
    saddle: Optional[Any] = None

    def _skip_clarification(self, ctx: RunContext, tool: str,
                            args: dict) -> bool:
        """本次调用是否不该开向导。

        四类跳过：
        · 非交互 run —— 没人能作答，Driver 本来就不会等，建题只会把模型拖进重试；
        · prod —— 危险调用由审批卡承载（卡片自带命令与后果说明），safe 调用直接执行；
        · hardline —— 无条件阻断，答什么都执行不了；
        · 有效风险 safe —— 不需要授权就会执行，不该先拦下来问 3 个问题。
        """
        if not ctx.metadata.get(AWAIT_APPROVAL_KEY):
            return True
        if str(ctx.metadata.get("permission_mode") or "").lower() == "plan":
            return True
        if (getattr(ctx, "environment", "") or "") == "prod":
            return True

        registry = resolve_registry(ctx, self.saddle)
        bound, is_free_form = extract_bound_command(registry, tool, args)
        if bound:
            level, _reason = (classify_command if is_free_form
                              else classify_constructed_command)(bound)
            return level in ("hardline", "safe")

        # 无绑定命令：看工具级风险；未知风险不放宽（宁可多问一次）
        get = getattr(registry, "get", None)
        impl = get(tool) if callable(get) else None
        meta = getattr(impl, "meta", None)
        return getattr(meta, "risk_level", "") == "safe"

    async def check(self, ctx: RunContext) -> ReinDecision:
        tc = ctx.active_tool_call
        if tc is None:
            return ReinDecision()

        # P1-6: 每轮先回收过期澄清（O(n) 小字典，n≤128）
        _purge_expired_clarifications()

        run_id = ctx.run_id
        tool = tc.get("tool", "")

        # 审查R2修复：LoopDetector 的等待豁免原先只覆盖审批路径
        # （permission_pending），澄清等待期模型重发同一命令 3 次即被判循环，
        # 第 3 次检测直接把 run 打成 FAILED、用户的答案变孤儿。本拦截器把
        # 等待中的签名登记进 clarification_pending，供 LoopDetector 豁免。
        # TTL 到期/答案已消费后清空，恢复常态检测。
        if (run_id not in _CLARIFICATION_CONTEXT
                and run_id not in _CLARIFICATION_DONE):
            ctx.metadata.pop("clarification_pending", None)

        # ── 答案就绪 → 作废原调用，回到规划层 ──
        consumed = consume_answer_decision(ctx)
        if consumed is not None:
            return consumed

        # ── 整治中（未完成）── ─ 重发当前步事件 + 拦截 ──
        ctx_info = _CLARIFICATION_CONTEXT.get(run_id)
        if ctx_info and not ctx_info.get("_done"):
            # 只放行「无副作用」的观测工具（contracts.READONLY_TOOLS，与并行化/
            # 循环检测共用同一判定），其余一律冻结 —— 包括 file_edit、schedule_op、
            # docker 这类不在 _CLARIFIABLE_TOOLS 里的改写工具。原实现只冻结注册表
            # 内 4 个工具，于是待答期间模型仍可 file_edit / 建定时任务：用户在
            # 确认「要不要删」的时候，AI 已经把别的东西改了。
            # 注意不能落到下面的「新建题」分支：同一 run 只允许一张向导，
            # 否则 _CLARIFICATION_CONTEXT 被第二张题覆盖，用户的答案无处可回。
            if tool in READONLY_TOOLS and tool != ctx_info.get("tool"):
                return ReinDecision()
            if tool == ctx_info.get("tool"):
                # 拦截分支不消费 inject_messages（见 driver），等待/重试指令
                # 必须写进 intercept_reason 才能到达模型。
                reason_text = "clarification_pending：请等待用户回答，不要重试调用同一工具"
            else:
                reason_text = (
                    "clarification_pending：澄清未答，写操作已冻结；"
                    "请等待用户回答后再继续")
            step = ctx_info.get("current_step", 0)
            steps = ctx_info.get("steps", [])
            ev = _build_clarification_needed(
                run_id, ctx_info.get("tool", tool), ctx_info.get("args", {}),
                steps, step, ctx_info.get("total", len(steps)),
                ctx_info.get("tool_call_id", ""),
                reason=ctx_info.get("reason", ""),
                kind=ctx_info.get("kind", KIND_SCOPE),
                risk=ctx_info.get("risk", ""),
                skip_policy=ctx_info.get("skip_policy", ""),
                owner_tool_call_id=ctx_info.get("owner_tool_call_id", ""),
            )
            _mark_pending_sig(ctx)
            return ReinDecision(
                intercepted=True,
                intercept_reason=reason_text,
                events=[ev],
                inject_messages=[{
                    "role": "user",
                    "content": "一个澄清问题正在等待用户回答，请不要重试调用同一工具。",
                }],
            )

        # ── 无整治中的题 ── 仅在工具 intent 不明时才创建新题 ──
        # 同一 run 只允许一张向导：上一张的答案还在等「它问的那次调用」来消费时，
        # 再叠一张会覆盖 _CLARIFICATION_CONTEXT，用户已经答过的东西就此无主。
        # 宁可这一次不澄清（审批层照旧挡危险命令），也不能丢答案。
        if run_id in _CLARIFICATION_DONE:
            return ReinDecision()
        # Driver 澄清等待闸在重判前打的标：本次调用已答过澄清。答案消费后，
        # 权限门若再重判一次 PRE_TOOL_CALL，没有这道标记就会在同一调用上
        # 重建向导 —— 用户答完又弹一张，永远执行不到工具。
        if tc.get("clarification_answered"):
            return ReinDecision()
        entry = _CLARIFIABLE_TOOLS.get(tool)
        if not entry:
            return ReinDecision()   # 工具不在注册表：直达执行
        detect_vague, build_steps_fn = entry
        args = tc.get("args", {})
        reason = detect_vague(args)
        if not reason:
            return ReinDecision()   # 意图明确：直达执行

        # 向导只在「有人答 + 答完能执行 + 真的需要授权」时才有意义。
        if self._skip_clarification(ctx, tool, args):
            return ReinDecision()

        steps = build_steps_fn(args)
        if not steps:
            return ReinDecision()   # 未配置题目：直达执行

        # 已等满 TTL 又被模型重试的同一条命令：不再弹第二张没人等答案的向导。
        # Driver 超时后把签名留在 clarification_waited（见 _finish_clarification），
        # 这里若照常建题：前端重新弹向导，run 却不会再停 —— 用户迟到的作答会落到
        # 一个已经往下跑的 run 上。改为直接拦截并要求换一条明确的命令。
        waited = ctx.metadata.get("clarification_waited")
        if isinstance(waited, set) and tool_call_signature(tc) in waited:
            return ReinDecision(
                intercepted=True,
                intercept_reason=(
                    "clarification_timeout：该命令已等待用户澄清超时，"
                    "请勿重试同一命令；请改用目标明确的命令，或先向用户说明再继续"),
            )

        # 创建 Pending 题元 + 发首步事件 + 拦截
        # P1-3: 使用 uuid 防止 tool_call_id 在多个工具调用时碰撞
        tool_call_id = f"clarify_{_uuid.uuid4().hex[:12]}"
        owner_tool_call_id = str(tc.get("tool_call_id") or "")
        kind = KIND_RISK if tool in ("file_write", "service_manage") else KIND_SCOPE
        if kind == KIND_RISK:
            risk = RISK_HIGH
        else:
            command_level = ""
            try:
                command_level = classify_command(command_from_args(args))[0]
            except Exception:
                pass
            risk = classify_tool_risk(
                environment=getattr(ctx, "environment", "") or "",
                command_level=command_level,
                is_write=tool not in READONLY_TOOLS,
                tool=tool,
            )
        skip_policy = skip_policy_for_tool(
            environment=getattr(ctx, "environment", "") or "",
            command_level=(classify_command(command_from_args(args))[0]
                           if tool in ("shell_exec", "job_run") else ""),
            is_write=tool not in READONLY_TOOLS,
            tool=tool,
        )
        ctx_info = {
            "tool": tool, "args": args, "tool_call_id": tool_call_id,
            "owner_tool_call_id": owner_tool_call_id,
            "kind": kind, "risk": risk, "policy": skip_policy,
            "skip_policy": skip_policy,
            "steps": steps, "current_step": 0, "total": len(steps),
            "reason": reason, "_done": False, "ts": time.time(),
        }
        _CLARIFICATION_CONTEXT[run_id] = ctx_info
        _enforce_clarification_cap()
        ev = _build_clarification_needed(
            run_id, tool, args, steps, 0, len(steps), tool_call_id,
            reason=reason, kind=kind, risk=risk, skip_policy=skip_policy,
            owner_tool_call_id=owner_tool_call_id,
        )
        _mark_pending_sig(ctx)
        return ReinDecision(
            intercepted=True,
            # 同 pending 分支：拦截决策的 inject_messages 不被 Driver 消费，
            # 兜底指令（等待闸超时/非交互 run 走到这里时）必须落在 reason 上。
            intercept_reason=(
                f"clarification_needed：工具 {tool} 意图不明，已向用户发起澄清；"
                "请等待回答，不要重试调用"),
            events=[ev],
            inject_messages=[{
                "role": "user",
                "content": f"工具 {tool} 调用意图不明：{reason}\n已发送问题给用户，请等待回答后按澄清结果执行。",
            }],
        )
