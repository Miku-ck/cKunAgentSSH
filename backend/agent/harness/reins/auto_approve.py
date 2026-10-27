"""自动审批（permission_mode="auto"）—— LLM 审批裁决器。

语义（与前端下拉文案对齐）：
  manual（"normal"）= 现状：dangerous 逐条弹审批卡，由**用户**批准；
  auto             = 发卡前让**模型**就同一次调用裁决一次：ALLOW → 放行，
                     其余一律照旧弹卡。用户仍是最终权威，LLM 只是代问一道前置筛。

fail-closed 是底线：超时 / 异常 / 未配置模型 / 输出不合约定 / 环境变量总闸关闭，
全部按 DENY 处理回退弹卡 —— "自动审批"绝不能因为模型不可用变成"无人审批"。

刻意**不经过裁决器**的路径（调用方负责保持这个顺序）：
  · hardline 绝对阻断 —— 不是审批问题；
  · prod —— 交互会话由用户现场点卡确认；无人值守只接受预授权 allowlist。
    让 LLM"替用户批准"会击穿「生产环境谁有权确认」这条边界；
  · 用户在 run 内明确拒绝过的命令 —— 裁决器不得推翻用户显式拒绝；
  · plan 模式 —— 只读强制优先于审批轴（PlanModeGuard 在任何审批之前拦截）。

审计：放行记 conclusion="auto_allowed"（agent/audit.py 既有值域），result 前缀
llm_judge_allow 便于事后区分"用户点的始终允许"与"模型裁的允许"。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from agent.audit import append_record_async

from ..contracts import ReinDecision, RunContext, TraceEvent
from .core import MODE_KEY, _audit_environment, _audit_target_id, _audit_target_type

logger = logging.getLogger(__name__)

MODE_AUTO = "auto"

# DB/UI 侧开关：设置面板 PUT /settings/permission-policy 经 set_db_enabled 推送。
# env 总闸 DISABLE_ENV_KEY=1 优先级更高（见 auto_approve_effective）—— 与代理
# 设置同姿态：运维侧一键退回纯手动审批不需要发版，也不允许设置页把它翻回来。
DISABLE_ENV_KEY = "CKUN_AGENT_DISABLE_AUTO_APPROVE"
AUTO_APPROVE_ENABLED = True


def set_db_enabled(enabled: bool) -> bool:
    """设置 DB/UI 侧开关。Returns: 实际写入值。"""
    global AUTO_APPROVE_ENABLED
    AUTO_APPROVE_ENABLED = bool(enabled)
    return AUTO_APPROVE_ENABLED


def auto_approve_effective() -> bool:
    """自动审批是否真正生效：env kill switch 优先压过设置面板开关。"""
    if os.environ.get(DISABLE_ENV_KEY) == "1":
        return False
    return bool(AUTO_APPROVE_ENABLED)

# 裁决要的是「一行 ALLOW/DENY」，给小 max_tokens 防模型开始长篇解释
_JUDGE_CHUNK_TIMEOUT_S = 2.0
_JUDGE_TOTAL_TIMEOUT_S = 6.0
_JUDGE_MAX_TOKENS = 16
# 进 prompt 的字段截断上限：args/命令再长也不该刷进裁决 prompt 全文
_MAX_SUBJECT_CHARS = 800
_MAX_TASK_CHARS = 300

_JUDGE_SYSTEM_PROMPT = (
    "你是运维命令审批裁决器。用户开启了自动审批：原本需要逐条向用户确认的工具调用"
    "先交由你裁决。只依据命令内容、风险判定理由与用户任务判断这次调用能否直接执行，"
    "宁可保守：有任何不确定（不可逆范围不明、目标疑似生产、脚本内容未读、疑似数据"
    "外发/删除用户数据）就拒绝。输出格式：第一行只写 ALLOW 或 DENY，不写其他内容。"
)


def is_auto_mode(ctx: RunContext) -> bool:
    """当前 run 是否处于自动审批档。未设置该键 = 手动（默认路径零影响）。"""
    return str((ctx.metadata or {}).get(MODE_KEY) or "").lower() == MODE_AUTO


def _parse_verdict(raw: Optional[str]) -> bool:
    """只认第一行的 ALLOW 关键字打头；其余（含空/异常输出）一律 DENY。"""
    if not raw:
        return False
    first = raw.strip().splitlines()[0].strip().upper()
    return first == "ALLOW" or first.startswith("ALLOW")


def _subject_of(tool: str, args: dict) -> str:
    cmd = ""
    if isinstance(args, dict):
        for k in ("command", "cmd", "script", "content"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                cmd = v
                break
    if not cmd:
        try:
            cmd = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            cmd = str(args)
    return f"{tool}: {cmd[:_MAX_SUBJECT_CHARS]}"


async def _judge_once(ctx: RunContext, subject: str, reason: str) -> bool:
    """一次性 LLM 裁决调用。任何失败路径返回 False（回退弹卡）。"""
    if not auto_approve_effective():
        return False
    # S10-07：只问 base_url。空 api_key 是本仓承认的合法态（面板的「自定义/vLLM」
    # 是 provider 初值且不渲染密钥输入框，库里存的就是空串；llm_adapter._get_client
    # 也按 `api_key or "EMPTY"` 处理）。原条件把它当成「没配模型」⇒ 自动审批对
    # 本地 vLLM 用户**永不生效**，开关拨上去没反应。
    if not ctx.llm_base_url:
        return False
    from post_processor import _call_llm_with_timeout

    model_cfg = {
        "base_url": ctx.llm_base_url,
        "api_key": ctx.llm_api_key,
        "model_id": ctx.llm_model,
    }
    env = getattr(ctx, "environment", "") or ""
    user_prompt = (
        f"环境: {env or '未知'}\n"
        f"待裁决调用: {subject}\n"
        f"风险判定理由: {reason or '（无，默认审批）'}\n"
        f"用户任务: {str(ctx.task or '')[:_MAX_TASK_CHARS]}\n"
        "第一行只输出 ALLOW 或 DENY。"
    )
    try:
        raw = await _call_llm_with_timeout(
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model_cfg=model_cfg,
            model_id=ctx.llm_model,
            temperature=0.0,
            max_tokens=_JUDGE_MAX_TOKENS,
            chunk_timeout=_JUDGE_CHUNK_TIMEOUT_S,
            total_timeout=_JUDGE_TOTAL_TIMEOUT_S,
            task_name="auto_approve_judge",
        )
    except Exception as e:  # 超时/网络/解析——全部按 DENY 处理
        logger.warning("自动审批裁决调用失败，回退人工审批: %s", e)
        return False
    return _parse_verdict(raw)


async def try_auto_approve(
    ctx: RunContext,
    tool: str,
    args: dict,
    reason: str,
    *,
    bound_command: str = "",
) -> Optional[ReinDecision]:
    """auto 档且模型裁 ALLOW → 返回放行决策（审计已记 auto_allowed，
    提示事件走 emit_event 通道）；否则 None。

    None = 调用方继续走原有的弹卡/拦截路径。非 auto 档直接返回 None，
    保证默认（手动）路径逐字段零差异 —— 与 plan_mode 的回归口径同款。
    裁决结果按 (tool, subject) 缓存在 ctx.metadata（per-run），
    同一命令在 run 内重发不重复刷 LLM。
    """
    if not is_auto_mode(ctx):
        return None

    subject = _subject_of(tool, args if isinstance(args, dict) else {})
    if bound_command:
        subject = f"{tool}: {str(bound_command)[:_MAX_SUBJECT_CHARS]}"

    cache: dict = ctx.metadata.setdefault("_auto_judge_cache", {})
    allow = cache.get(subject)
    if allow is None:
        allow = await _judge_once(ctx, subject, reason)
        cache[subject] = allow
    if not allow:
        return None

    await append_record_async(
        action=tool, conclusion="auto_allowed",
        run_id=ctx.run_id, iteration=ctx.iteration,
        target_type=_audit_target_type(ctx),
        target_id=_audit_target_id(ctx),
        environment=_audit_environment(ctx),
        command_raw=str(bound_command or subject),
        result=f"llm_judge_allow: {reason}",
    )
    # 放行提示必须走 emit_event 通道，**不能**挂在返回的 ReinDecision.events 上：
    # driver 的 PRE_TOOL_CALL 非拦截分支根本不消费 events（见 driver.py 拦截分支
    # 才有的 _emit 循环），挂上去 = 静默丢弃。emit_event 是审批卡片链路既有的
    # 「迟到增益」通道；单测/调度复用等无队列场景该键缺失，退回纯审计记录。
    emit = (ctx.metadata or {}).get("emit_event")
    if emit is not None:
        await emit(TraceEvent(
            kind="warning",
            source="reins.auto_approve",
            run_id=ctx.run_id,
            iteration=ctx.iteration,
            payload={
                "tool": tool,
                "message": (
                    f"[自动审批] 模型裁决放行（原判定：{reason or '默认审批'}）。"
                    "关闭方式：输入区权限模式切回「手动审批」。"
                ),
            },
        ))
    return ReinDecision()
