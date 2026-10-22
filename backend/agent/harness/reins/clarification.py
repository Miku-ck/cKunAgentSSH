"""澄清向导（ClarificationGate）— pending 存储、检测器/题集与拦截器 — 拆分自原 reins.py。

设计要点（与原实现一致）：一个 run 最多一道进行中澄清题；detect_vague 返回 None
表示意图明确零打扰；前端本地推进多步后一次性 POST；skip = 空答案放行。
"""

from __future__ import annotations

import logging
import re as _re
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Optional

from ..contracts import (
    ClarificationOption,
    ClarificationStep,
    ReinDecision,
    ReinPoint,
    RunContext,
    TraceEvent,
)
from .core import command_from_args, tool_call_signature

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
    """清理超过 TTL 的澄清条目（context 与配对的 done），返回清理条数"""
    now = time.time()
    expired = [
        rid for rid, ci in _CLARIFICATION_CONTEXT.items()
        if now - float(ci.get("ts", now)) > _CLARIFICATION_TTL_SEC
    ]
    for rid in expired:
        _CLARIFICATION_CONTEXT.pop(rid, None)
        _CLARIFICATION_DONE.pop(rid, None)
    # done 必与 context 配对（record_clarification_answers 保证桩），孤儿一并回收
    orphans = [rid for rid in _CLARIFICATION_DONE
               if rid not in _CLARIFICATION_CONTEXT]
    for rid in orphans:
        _CLARIFICATION_DONE.pop(rid, None)
    return len(expired) + len(orphans)


def record_clarification_answers(run_id: str, answers: dict) -> None:
    """路由层写入答案/跳过结果。

    统一维护 done + context 配对与时间戳：无既有 context（run 已换新/题已清）
    时补最小桩，保证 TTL 清扫能找到并回收孤儿答案。
    """
    _CLARIFICATION_DONE[run_id] = answers
    ctx_info = _CLARIFICATION_CONTEXT.get(run_id)
    if ctx_info is None:
        _CLARIFICATION_CONTEXT[run_id] = {
            "tool": "", "args": {}, "steps": [], "current_step": 0,
            "total": 0, "reason": "", "_done": True, "ts": time.time(),
        }
    else:
        ctx_info["_done"] = True
        ctx_info.setdefault("ts", time.time())
    _enforce_clarification_cap()


def release_clarification_run(run_id: str) -> bool:
    """run 终态回收该 run 的澄清状态（未答完的题随 run 作废）"""
    had = _CLARIFICATION_CONTEXT.pop(run_id, None) is not None
    had = _CLARIFICATION_DONE.pop(run_id, None) is not None or had
    return had


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
    # 2) 含通配符或递归标记（-rR 可能以 -rf/-fr/-rR 组合形式出现，做宽松匹配）
    has_wildcard = any(c in cmd for c in ("*", "?"))
    # 匹配 -r / -R / -rf / -fr / -rR 等任何含 r/R 的短选项 token
    has_recurs = bool(_re.search(r'(?:^|\s)-[a-zA-Z]*[rR][a-zA-Z]*\b', lower))
    if not has_wildcard and not has_recurs:
        return None
    # 3) 走到这里必然同时满足「含破坏性动词」与「含通配符/递归」，才报影响范围不确定。
    #    精准单文件的 rm 是在第 2 步 return None；`mv` 根本不在第 1 步的 _DESTRUCTIVE 表里
    #    （只有 rm/rmdir/xargs/chmod/chown），它在第 1 步就退出 —— 两条不是同一个出口。
    return f"命令 {cmd!r} 使用通配符/递归操作但未指定具体目标文件，影响范围不确定"


def _shell_build_steps(args: dict) -> list[ClarificationStep]:
    """为模糊 shell 命令构建 3 步澄清题（范围 / 处置方式 / 完成后）。

    面向 SSH 远程运维语境：目标是远端主机上的文件，措辞不得假设本地
    git 工作区（原实现选项为"仅 git 已修改文件/自动 commit"，与场景错位）。
    """
    cmd = command_from_args(args)
    cmd_display = (cmd[:40] + "…") if len(str(cmd)) > 40 else str(cmd)
    return [
        ClarificationStep(
            question=f"\"{cmd_display}\" 在目标机上的作用范围是？",
            options=[
                ClarificationOption(id="A", label="仅通配符所在目录一层，不递归子目录（影响最小）"),
                ClarificationOption(id="B", label="含全部子目录（按命令中的递归语义执行）"),
                ClarificationOption(id="C", label="我指定确切路径（在自定义输入中列出）"),
            ],
            allow_custom=True,
        ),
        ClarificationStep(
            question="对匹配项的处理方式？",
            options=[
                ClarificationOption(id="A", label="仅报告，列出匹配项但不做任何修改"),
                ClarificationOption(id="B", label="直接执行删除/清理"),
                ClarificationOption(id="C", label="执行但生成 undo 脚本（可回滚）"),
            ],
            allow_custom=True,
        ),
        ClarificationStep(
            question="执行完成后？",
            options=[
                ClarificationOption(id="A", label="直接结束，无需汇报"),
                ClarificationOption(id="B", label="用只读命令（ls/du 等）复核结果并给出前后对比"),
                ClarificationOption(id="C", label="汇总受影响文件清单与释放空间报告供我审查"),
            ],
            allow_custom=True,
        ),
    ]


# ── file_write：覆写系统配置文件 ──

_SYSTEM_CONFIG_PREFIXES = ("/etc/", "/boot/", "/usr/lib/systemd/", "/lib/systemd/",
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
    if path.startswith(_SYSTEM_CONFIG_PREFIXES) or path in ("/etc", "/boot"):
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
        ClarificationStep(
            question="完成后需要什么形式的结果汇报？",
            options=[
                ClarificationOption(id="A", label="直接结束，无需汇报"),
                ClarificationOption(id="B", label="给出修改前后 diff 摘要"),
                ClarificationOption(id="C", label="diff 摘要 + 相关服务运行状态核验"),
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
                                reason: str = "") -> TraceEvent:
    return TraceEvent(
        kind="clarification_needed",
        source="reins.clarification",
        run_id=run_id,
        payload={
            "run_id": run_id,
            "tool": tool,
            "tool_call_id": tool_call_id,
            "args": args,
            "step": step,
            "total": total,
            "reason": reason,
            "steps": [_step_to_dict(s) for s in steps],   # 全量步，前端本地推进
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
        return "用户选择跳过澄清，未指定具体范围。"
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

        # ── 答案就绪 → 注入上下文 + 放行 ──
        if run_id in _CLARIFICATION_DONE:
            # 答案是针对哪一次调用问的就还给谁。原先只看 run_id：答完之后紧接着的
            # 第一次 PRE_TOOL_CALL（不管是不是同一个工具）都把答案消费掉。而等待期
            # 注入的文案明写「请不要重试调用同一工具」，模型转去调别的工具是常态路径，
            # 于是 ① 澄清被花在无关操作上（注入文本还接着说"请按上述澄清范围重新执行
            # 操作"），② 向导上下文被 pop，真正触发澄清的那次调用再进来时无据可依。
            pending_ctx = _CLARIFICATION_CONTEXT.get(run_id)
            owner = str((pending_ctx or {}).get("tool") or "")
            if owner and owner != tool:
                return ReinDecision()
            answers = _CLARIFICATION_DONE.pop(run_id)
            ctx.metadata.pop("clarification_pending", None)
            ctx_info = _CLARIFICATION_CONTEXT.pop(run_id, None)
            summary = _format_answers(answers, (ctx_info or {}).get("steps") or [])
            done_ev = TraceEvent(
                kind="clarification_done",
                source="reins.clarification",
                run_id=run_id,
                iteration=ctx.iteration,
                payload={"run_id": run_id, "answers": answers,
                         "summary": summary,
                         "tool": (ctx_info or {}).get("tool", tool)},
            )
            return ReinDecision(
                intercepted=False,
                events=[done_ev],
                inject_messages=[{
                    "role": "user",
                    "content": f"[用户澄清结果]\n{summary}\n请按上述澄清范围重新执行操作。",
                }],
            )

        # ── 整治中（未完成）── ─ 重发当前步事件 + 拦截 ──
        ctx_info = _CLARIFICATION_CONTEXT.get(run_id)
        if ctx_info and not ctx_info.get("_done"):
            # 只冻结「会改写系统」的调用（注册表内 4 个工具，含触发澄清的那个）。
            # 原实现拦下一切工具：用户离席几十秒、模型只想 file_read / sys_overview
            # 也要逐次吃 "clarification_pending"，直到 600s TTL 才结束，整个 run
            # 被一张没人答的向导拖死。只读工具直接放行。
            # 注意不能落到下面的「新建题」分支：同一 run 只允许一张向导，
            # 否则 _CLARIFICATION_CONTEXT 被第二张题覆盖，用户的答案无处可回。
            if tool != ctx_info.get("tool") and tool not in _CLARIFIABLE_TOOLS:
                return ReinDecision()
            step = ctx_info.get("current_step", 0)
            steps = ctx_info.get("steps", [])
            ev = _build_clarification_needed(
                run_id, ctx_info.get("tool", tool), ctx_info.get("args", {}),
                steps, step, ctx_info.get("total", len(steps)),
                ctx_info.get("tool_call_id", ""),
                reason=ctx_info.get("reason", ""),
            )
            _mark_pending_sig(ctx)
            return ReinDecision(
                intercepted=True,
                intercept_reason="clarification_pending",
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
        entry = _CLARIFIABLE_TOOLS.get(tool)
        if not entry:
            return ReinDecision()   # 工具不在注册表：直达执行
        detect_vague, build_steps_fn = entry
        args = tc.get("args", {})
        reason = detect_vague(args)
        if not reason:
            return ReinDecision()   # 意图明确：直达执行

        steps = build_steps_fn(args)
        if not steps:
            return ReinDecision()   # 未配置题目：直达执行

        # 创建 Pending 题元 + 发首步事件 + 拦截
        # P1-3: 使用 uuid 防止 tool_call_id 在多个工具调用时碰撞
        tool_call_id = f"clarify_{_uuid.uuid4().hex[:12]}"
        ctx_info = {
            "tool": tool, "args": args, "tool_call_id": tool_call_id,
            "steps": steps, "current_step": 0, "total": len(steps),
            "reason": reason, "_done": False, "ts": time.time(),
        }
        _CLARIFICATION_CONTEXT[run_id] = ctx_info
        _enforce_clarification_cap()
        ev = _build_clarification_needed(
            run_id, tool, args, steps, 0, len(steps), tool_call_id,
            reason=reason,
        )
        _mark_pending_sig(ctx)
        return ReinDecision(
            intercepted=True,
            intercept_reason="clarification_needed",
            events=[ev],
            inject_messages=[{
                "role": "user",
                "content": f"工具 {tool} 调用意图不明：{reason}\n已发送问题给用户，请等待回答后按澄清结果执行。",
            }],
        )
