"""无头 Harness run 装配 — 无人值守入口（定时任务 / 运维剧本）共用的运行装配层。

语义母本：backend/agent/schedule_engine.py:466-639（ScheduleEngine._run_agent_once），
以及 :685-703（_resolve_model）、:802-812（_schedule_environment）。

同步责任（设计评审意见 2 定案，docs/p0-ops-agent-enhancements.md §3.1/§7-2）：
本文件是母本装配段当前状态的**语义对齐独立实现**，v1 零改动 schedule_engine.py
（schedule_engine 是 P0 稳定性优先的热文件，抽取收敛 defer 到 v2 专门一轮，届时
schedule 回归测试做证据）。母本装配段 —— 模型解析、Harness+StreamLLMClient+
create_registry+CostTracker assemble、bridge probe→connect_ssh→disconnect_ssh、
resolve_run_environment 环境判定、harness.run 迭代与 done/error 收集 —— 变更时
**必须同步检查本文件**，否则两份装配漂移。

治理边界（与母本同源）：
- 全程走完整 Harness 治理链（reins PRE/POST → 审计落链，run_id 贯通 audit_log）。
  刻意**不复用** agent/tools/builtin/ops_common.run_headless：那是既有运维工具内部
  「在 ToolContext 内代执行一条命令」的旁路（无 Reins PRE 检查、无审计落链），
  无人值守入口禁止借用。
- await_approval=False（无人值守）：非 safe 命令被 reins 拦截即返回而非挂起。
  拦截在事件流上表现为 permission_needed 事件（headless 无人等待卡片）——本模块
  把它收集为 permission_requested 标志，调用方（runbook_engine）据此把该步骤判为
  blocked（失败），绝不当作执行成功。
- 环境判定只收紧不放宽：resolve_run_environment 按主机连接配置判定；查不到按
  prod 兜底（无人值守没有「用户在场」这层兜底，与母本 _schedule_environment 同姿态）。
- bridge 不可达 fail-closed：probe 失败抛 BridgeError 明确报错，不降级、不静默跳过。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import database as db
from agent.environment_policy import EnvironmentResolution, resolve_run_environment
from agent.cost_tracker import CostTracker
from agent.electron_bridge import ElectronBridge
from agent.harness import Harness, HarnessConfig
from agent.harness.contracts import POLICY_DENY_MARKER
from agent.harness.contracts import AgentConfig as HarnessAgentConfig
from agent.harness.llm_client import StreamLLMClient
from prompts import SSH_MODE

logger = logging.getLogger(__name__)

# 默认迭代上限（与母本 _SCHEDULE_MAX_ITERATIONS 同值：轻量单步骤 run 不需要长循环）
_DEFAULT_MAX_ITERATIONS = 8
# 模型未配置 temperature/top_p 时的兜底值（与母本同口径，取 models_config 列默认）
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_TOP_P = 0.9


@dataclass
class HeadlessRunResult:
    """一次无头 run 的可观察结果。

    state 来自 driver done 事件 payload（completed / failed / max_reached /
    human_help…），是「这次 run 到底收敛了没有」的唯一权威（S7-01 同款判据）；
    permission_requested 表示 run 期间出现过 permission_needed（headless 下的
    拦截信号），调用方据此 fail-closed。
    """

    content: str = ""
    error: str = ""
    state: str = ""
    host: str = ""
    environment: str = ""
    permission_requested: bool = False


def _num_param(value, default: float) -> float:
    """数值型模型参数：只有缺失/None 才用默认值（0 是合法值，不能被 or 吞掉）。"""
    return default if value is None else float(value)


def resolve_model() -> dict | None:
    """取默认模型（未掩码）供 Harness 调用：先 SSH 档再全局档。

    与母本 ScheduleEngine._resolve_model（schedule_engine.py:685-703）同口径 ——
    设置页 ★ 说明写的是「对话 / 终端 / 定时任务读的那档」，只读全局 is_default
    会在用户只勾 ★ 没勾 ⊕ 时误报「未配置默认模型」。
    """
    default = (
        db.get_default_model(SSH_MODE, mask_key=False)
        or db.get_default_model(None, mask_key=False)
    )
    if not default:
        return None
    mid = default.get("id") or default.get("model_id")
    if not mid:
        return default
    cfg = db.get_model_config(mid, mask_key=False)
    return cfg or default


def environment_for_unattended(resolution: EnvironmentResolution) -> str:
    """无人值守 run 的环境标签：主机连接配置说了算，查不到按 prod 保守兜底。

    与母本 _schedule_environment（schedule_engine.py:802-812）同语义：无人值守
    没有「用户在场兜底」，客户端声明的 test 在这里没有任何可信度。只收紧不放宽。
    """
    if resolution.source == "connection":
        return resolution.effective
    return "prod"


def _is_deny_signal(kind: str, payload: dict | None) -> bool:
    """事件是否为「策略拦截、无人可批」的确定性信号（headless 下的 blocked 判据）。

    permission_needed（非 prod 发卡路径）之外，prod 未预登记（gate/guards 的
    prod_deny_no_allowlist）只发 warning、「连续拒绝停机」只发 human_help_needed
    —— 拦截证据认 payload 文本里的机器可读标记 POLICY_DENY_MARKER，不匹配面向
    人的中文文案（改文案不破判据）。预算/墙钟等常规 warning 不是拦截证据。
    """
    if kind in ("permission_needed", "human_help_needed"):
        # human_help_needed 在 headless 下同样无人应答，run 无法继续 —— 与拦截同归 blocked
        return True
    if kind == "warning":
        return any(
            isinstance(v, str) and POLICY_DENY_MARKER in v
            for v in (payload or {}).values()
        )
    return False


async def open_session(connection_id: str, *, log_tag: str = "") -> tuple[str, ElectronBridge]:
    """为目标连接建一条 SSH 会话：探活 → 正式建连，返回 (session_id, bridge)。

    与母本 schedule_engine.py:511-559 同语义：
    - 未注入端口/token（is_available False）→ BridgeError 快速拒绝；
    - probe 用 probe() 而非 ping()：需要具体失败原因码；
    - connect_ssh 返回空 sessionId 视为 HTTP_ERROR；
    - 非结构化异常反向 classify_bridge_error，统一以 BridgeError 抛出（fail-closed，
      不降级、不静默）。
    会话的关闭责任在调用方（close_session）——通常放在 finally。
    """
    from agent.electron_bridge import BridgeError, BridgeErrorCode, classify_bridge_error

    bridge = ElectronBridge()
    if not bridge.is_available():
        raise BridgeError(
            BridgeErrorCode.NOT_CONFIGURED,
            "无人值守任务需要 Electron bridge，但未注入端口/token",
        )
    probe_error = None
    try:
        probe_error = await bridge.probe()
    except Exception as e:
        # probe 内部已全量捕获异常，此处仅为防御未来改动（对齐母本 schedule_engine）
        probe_error = classify_bridge_error(e)
    if probe_error is not None:
        logger.warning("%s 桥接探活失败 [%s]", log_tag, probe_error.value)
        raise BridgeError(probe_error)
    try:
        conn = await bridge.connect_ssh(connection_id)
        session_id = str(conn.get("sessionId") or "")
        if not session_id:
            raise BridgeError(BridgeErrorCode.HTTP_ERROR, "connect_ssh 返回空 sessionId")
    except BridgeError:
        raise
    except Exception as e:
        code = classify_bridge_error(e)
        logger.warning("%s 建会话失败（connection=%s）: [%s] %s", log_tag, connection_id, code, e)
        raise BridgeError(code, str(e), original=e if isinstance(e, Exception) else None)
    logger.info("%s 已为目标主机建立会话 %s", log_tag, session_id)
    return session_id, bridge


async def close_session(
    bridge: ElectronBridge | None, ssh_session_id: str, *, log_tag: str = ""
) -> None:
    """关闭 open_session 建立的会话（best-effort，只 warning 不抛）。"""
    if not bridge or not ssh_session_id:
        return
    try:
        await bridge.disconnect_ssh(ssh_session_id)
        logger.info("%s 已关闭会话 %s", log_tag, ssh_session_id)
    except Exception as e:
        logger.warning("%s 关闭会话 %s 失败: %s", log_tag, ssh_session_id, e)


async def run_headless_once(
    task: str,
    *,
    run_id: str,
    conversation_id: str,
    ssh_session_id: str = "",
    connection_id: str = "",
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    system_prompt: str = "",
    log_tag: str = "",
) -> HeadlessRunResult:
    """组装 Harness 无头运行一次，返回 HeadlessRunResult。

    SSH 会话两种模式（设计 §3.1「SSH 会话复用」）：
    - ssh_session_id 非空（预建会话，executor 跨步骤复用）：直接使用，**不自建、
      不关闭** —— 建连/断连责任归调用方（每执行 1 次建连，finally 统一断开）；
    - 否则 connection_id 非空：本函数自建（open_session），并在 finally 中自关 ——
      与母本「任务结束关闭自动建立的会话」同语义；
    - 两者皆空：无 SSH 会话运行（远程工具按各自 fail-closed 语义失败）。
    """
    model_cfg = await asyncio.to_thread(resolve_model)
    if not model_cfg:
        raise RuntimeError("未配置默认模型，无法执行无人值守任务")

    model_name = model_cfg.get("model_id") or model_cfg.get("id") or ""
    ctx_limit = int(model_cfg.get("max_tokens") or 16384)

    harness = Harness(HarnessConfig(
        base_url=model_cfg["base_url"],
        api_key=model_cfg.get("api_key") or "",
        model=model_name,
        max_context_tokens=ctx_limit,
    ))
    llm_client = StreamLLMClient(
        base_url=model_cfg["base_url"],
        api_key=model_cfg.get("api_key") or "",
    )
    from agent.tools import create_registry
    tool_registry = create_registry()
    cost_tracker = CostTracker(model_id=model_name)
    harness.assemble(
        llm_client=llm_client,
        tool_registry=tool_registry,
        cost_tracker=cost_tracker,
    )

    # ── SSH 会话：预建复用 or 自建自关（见 docstring）──
    owned_session = False
    bridge: ElectronBridge | None = None
    if not ssh_session_id and connection_id:
        ssh_session_id, bridge = await open_session(connection_id, log_tag=log_tag)
        owned_session = True

    result = HeadlessRunResult()
    try:
        # ── 环境与目标主机：与交互式 /agent/run 同源（服务端按连接配置判定，只收紧）──
        resolution = await resolve_run_environment("test", ssh_session_id)
        environment = environment_for_unattended(resolution)
        result.host = resolution.host or ""
        result.environment = environment
        logger.info(
            "%s 环境判定: %s（source=%s, host=%s）",
            log_tag, environment, resolution.source, resolution.host or "-",
        )

        # 输出 token 上限钳制为上下文的一半（与母本同款注释：贴近上限必超窗 →
        # vLLM 400，run 瞬时失败且无输出）
        out_limit = int(model_cfg.get("max_tokens") or ctx_limit)
        safe_out = max(1, min(out_limit, ctx_limit // 2))
        rt = model_cfg.get("reasoning_tokens")
        # 0 是合法配置值（0=使用 max_output_tokens），仅未设置才回落 None
        safe_reasoning = min(int(rt), safe_out) if rt is not None else None

        cfg = HarnessAgentConfig(
            max_iterations=max_iterations,
            temperature=_num_param(model_cfg.get("temperature"), _DEFAULT_TEMPERATURE),
            top_p=_num_param(model_cfg.get("top_p"), _DEFAULT_TOP_P),
            max_tokens=safe_out,
            max_reasoning_tokens=safe_reasoning,
            enable_thinking=True,
            enable_tools=True,
            enable_intention_guide=False,
            enable_dynamic_budget=False,
        )

        done_content = ""
        done_state = ""
        error_msg = ""
        permission_requested = False
        async for ev in harness.run(
            task=task,
            conversation_id=conversation_id,
            ssh_session_id=ssh_session_id,
            environment=environment,
            # 主机限域授权的匹配维度：连接配置里的 host，不是连接 ID
            target_host=resolution.host,
            system_prompt=system_prompt,
            history=[],
            memories=[],
            config=cfg,
            run_id=run_id,
        ):
            if ev.kind == "error":
                # harness 内部错误（如 LLM 调用失败）回传，不让调用方拿到「无输出」
                error_msg = ((ev.payload or {}).get("message") or "") or error_msg
            elif _is_deny_signal(ev.kind, ev.payload):
                # headless（await_approval=False）下卡片无人可点：命令被拦截即返回。
                # 这是「本次 run 出现过拦截」的确定性信号，调用方按 blocked 处理。
                permission_requested = True
            elif ev.kind == "done":
                payload = ev.payload or {}
                done_content = payload.get("content", "") or done_content
                # S7-01：driver 的 done 载荷一直带着终态，消费端不能丢
                done_state = payload.get("state") or done_state

        result.content = done_content
        result.error = error_msg
        result.state = done_state
        result.permission_requested = permission_requested
        return result
    finally:
        # finally 覆盖 open_session 成功之后的全部路径（成功 return 与任何异常）：
        # 自建会话必须回收，不得泄漏连接；预建会话模式（owned_session=False）
        # 绝不在此关闭 —— 断连责任归调用方（executor 统一 finally 断开）
        if owned_session and ssh_session_id:
            await close_session(bridge, ssh_session_id, log_tag=log_tag)
