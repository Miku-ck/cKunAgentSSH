"""Agent 权限路由：授权/撤销/手动登记/列表/拒绝/release-run + 命令风险判定。

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与请求/响应契约不变。

`_run_environment` / `_run_host` / `append_record_async` 从 routes.agent 局部导入：
前者与 run 环境登记状态（_RUN_ENV）同体、后两者被测试 monkeypatch，
局部导入保证拿到的是同一份可变状态与替换后的函数。
"""

import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import database as db

logger = logging.getLogger(__name__)

router = APIRouter()


class PermissionGrantRequest(BaseModel):
    # 与 AllowPermissionRequest 同口径限长：这个键会原样落进 allowlist.txt，
    # 不限长度就等于给授权清单文件开了一个无界写入入口（字符集校验管不住长度）
    tool: str = Field(..., min_length=1, max_length=160, description="工具名")
    # 审查 M-3：默认 scope 从 "always" 收紧为 "once"（仅放行下一次调用）。
    # 原默认 always 会在用户点一次后永久免审批。
    # Q-B4：裸 str 改枚举 —— 非法 scope 在请求模型层就被 422 拒掉，
    # 不再需要路由函数里的手工 if 白名单。
    scope: Literal["once", "session", "always"] = Field(
        "once", description="授权范围：once（仅本次）/ session（会话）/ always（永久）")

    @field_validator("scope")
    @classmethod
    def no_permanent_for_mcp(cls, value: str, info) -> str:
        # 插件配置变更会递增 config_rev 并撤销同意，一条「永久」allowlist
        # 会跨配置代际继续放行同名工具 —— 那等于授权了一个已经换掉的进程。
        if value == "always" and str(info.data.get("tool") or "").startswith("mcp__"):
            raise ValueError("插件工具不提供永久授权（仅本次 / 本会话）")
        return value
    # 命令执行工具（shell_exec）时传入被批准的具体命令，
    # 授权绑定到命令哈希（同命令免审批，其他 dangerous 命令仍需审批）
    command: Optional[str] = Field(None, description="命令执行工具的具体命令（用于命令级绑定）")
    # C-review M11：session 级授权绑定的会话 ID — 防跨会话授权泄漏
    conversation_id: str = Field("", description="会话 ID（session 授权按会话键控）")
    # P1 竞态修复：once 授权绑定的运行 ID — 运行终止时按 run_id 清理
    run_id: str = Field("", description="运行 ID（once 授权按 run_id 隔离，终态自动清理）")


@router.post("/agent/permission/grant")
async def grant_tool_permission(req: PermissionGrantRequest):
    """授权工具

    审查R2修复（线程竞态）：本组审批端点此前是同步 def 路由，被 FastAPI 丢进
    anyio 线程池执行，与事件循环上的 PermissionGate 消费/清理代码（reins.check、
    event_stream finally 的回收）无锁并发读写同一批可变 dict/set —— sorted()
    迭代中另一线程 add/discard 会抛 RuntimeError，defaultdict 复合 += 丢账。
    改为 async def 后 gate 状态操作全部回到事件循环单线程，天然串行；
    审计落盘（append_record，阻塞文件写）经 asyncio.to_thread 移出事件循环。
    gate 内部的小额本地文件写（allowlist 覆盖写，几行文本）随 grant 留在循环内：
    把它单独挪进线程会把状态突变也带出循环，竞态复活 —— 两害相权取小文件同步写。

    scope=once: 仅放行下一次相同工具（命令工具为相同命令）调用
    scope=session: 进程级单例 _confirmed_session，跨 run 共享，进程重启失效（H13 修复 A5）；
      C-review M11：按 conversation_id 键控，只对发起会话的 run 生效
    scope=always: 写入 ~/.ckunagentic/allowlist.txt，所有后续 run 放行
      （命令工具写入的是 `tool:cmdhash` 键，不覆盖其他命令 — 审查 M-3）
    """
    from agent.harness.contracts import TERMINAL_RUN_STATES
    from agent.harness.reins import _get_global_permission_gate_sync, approval_wait
    from agent.harness.reins.gate import normalize_host, scoped_key
    from routes.agent import (
        _run_environment, _run_host, append_record_async)
    # hardline 预检（Q-B4①）：底线命令在 FilesystemGuard 层无条件阻断
    # （reins.guards 的 hardline 分支不问 PermissionGate、不看 allowlist），
    # 把它写进 allowlist 的预授权必然落空 —— 与其 200 登记「已授权」的死键
    # （审计里还留下一条 approved），不如 400 把不可放行原因摆到面上。
    # 只拦 hardline：dangerous 是审批机制要处理的正常输入，不在此收窄。
    if req.command and str(req.command).strip():
        from agent.harness.safety import classify_command
        _level, _reason = classify_command(str(req.command))
        if _level == "hardline":
            raise HTTPException(
                status_code=400,
                detail=(f"命令不可放行：{_reason or '命中 hardline 底线'}。"
                        "hardline 命令被无条件阻断，allowlist 无法放行，"
                        "登记预授权不会生效；请改用安全的替代方案。"),
            )
    # once 授权的两道前置守卫（都只在 scope==once 时触发，合并到一个条件块）：
    #   1) 缺 run_id：无法被任何 run 消费，直接 400（原实现落空 run_id 全局池，有跨 run 竞态）
    #   2) run 已终态（P2 修复，审批审查 2026-10-11）：cleanup_run_once 已清空该 run 的池，
    #      此刻落库的键永不被消费也不再被清理（死键泄漏），前端却显示「已授权」。典型触发：
    #      审批兜底到期后 run 结束、SSE 迟到/断连窗口内的迟到点击。行缺失同样 409（fail-closed）：
    #      审批卡只在 run 运行中产生，届时 DB 行必已存在，缺行说明 run_id 本身不可信。
    if req.scope == "once":
        if not req.run_id:
            raise HTTPException(status_code=400, detail="scope=once 授权必须携带 run_id")
        try:
            _row = await asyncio.to_thread(db.get_agent_run, req.run_id)
        except Exception as e:
            # 查询本身失败不拦授权：这条校验防的是「死键泄漏 + 假已授权」的完整性
            # 问题，不是安全闸门（once 键消费时 PermissionGate 本就还要查表）。
            # 拿不到证据不等于 run 已死，告警放行。
            logger.warning("grant 前置查询 run=%s 状态失败，放行授权: %s", req.run_id, e)
        else:
            _run_status = (_row or {}).get("status", "")
            if _row is None or _run_status in TERMINAL_RUN_STATES:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"run {req.run_id} 已结束（status={_run_status or '不存在'}），"
                        "once 授权无处消费；请在新的运行中重新确认该命令"
                    ),
                )

    # H13 修复 A5：session scope 改用进程级单例（原 501 限制移除）
    pg = _get_global_permission_gate_sync()
    # 写前审计：先把「这条授权发生了」落进链，再让授权生效。顺序反过来（先 grant
    # 再审计）时，一次写失败就会留下「授权已生效、没有记录」的缺口 —— 而审计链里
    # 最危险的形态不是空链，是看起来完整、实际漏了一条的链。
    # grant_key_for 只校验 + 算键、不改任何授权状态，保证审计记录不会先于一条
    # 最终被 ValueError 拒绝的授权落地。
    try:
        grant_key = pg.grant_key_for(
            req.tool, req.command, scope=req.scope, run_id=req.run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # C-review C2 修复：批准动作落审计链（原实现只写内存/allowlist，
    # 事后无法证明"谁批准了什么"，等保审计目标落空）。
    # S2-04：审批决策必须带 run_id。已退役的 docs/security-boundary.md §6 声明每条审批
    # 记录可追到它服务的 run，而原实现八处 append_record 全都没传 run_id ——
    # grant 这一处连 req.run_id 都攥在手里（scope=once 还强制它非空，否则 400）。
    # 事后只能证明「有人批过 foo」，证不了「批的是哪次运行」，等保审计目标落空。
    # critical=True：写不进去就不授权（全局 handler 转 503），详见 audit.append_record。
    # 主机限域：always 与 session 都用 —— once 本来就随 run 结束而失效，run 只跑
    # 一台主机，不需要再限定作用域。session 不限定就会漏成「测试机上点本会话允许，
    # 同一对话切到生产机后同一条命令直接免审」（会话跨主机，而主机是具体的）。
    # host 来自服务端反查（与 environment 同源），拿不到时为空 → 落全局条目，
    # 与匹配端「host 为空只查全局」对称，不会出现「授权了但永不命中」。
    grant_host = _run_host(req.run_id) if req.scope in ("always", "session") else ""
    await append_record_async(
        action=f"permission_grant:{req.tool}",
        conclusion="approved",
        run_id=req.run_id,
        target_type="tool",
        target_id=scoped_key(grant_host, grant_key),
        # 环境列原先恒为默认值 "test"：对 prod 主机的授权在审计页的 prod 视图里
        # 直接不存在，等于把生产审批藏起来了。取本次 run 的服务端判定结果。
        environment=_run_environment(req.run_id),
        command_raw=req.command or "",
        result=(f"scope={req.scope},conversation={req.conversation_id or '-'}"
                f",host={grant_host or '全局'}"),
        critical=True,
    )
    # 审计已落地，此刻才让授权生效。
    pg.grant(req.tool, scope=req.scope, command=req.command,
             conversation_id=req.conversation_id, run_id=req.run_id,
             host=grant_host)
    # 唤醒正在等这张卡的 run：Driver 在审批点阻塞（见 reins.approval_wait），
    # 不叫醒它就得一直挂到兜底超时。授权已落库，醒来重判自然放行。
    approval_wait.resolve_for_call(
        req.run_id, req.tool, grant_key, approval_wait.ALLOWED)
    return {"tool": req.tool, "scope": req.scope, "grant_key": grant_key,
            "host": normalize_host(grant_host), "granted": True}


@router.post("/agent/permission/revoke")
async def revoke_tool_permission(tool: str, command: Optional[str] = None,
                                 conversation_id: Optional[str] = None,
                                 host: Optional[str] = None):
    """撤销工具授权（从永久 allowlist 和 session scope 移除）

    审查R2修复：改 async def 回到事件循环单线程操作 PermissionGate 状态，
    与 grant 端点同一姿态（详见 grant 路由 docstring）。

    提供 command 时只撤销该命令的绑定授权；否则撤销该工具全部授权。
    C-review M11：提供 conversation_id 时只撤该会话的 session 授权，
    否则（默认）撤所有会话。
    host：不传 = 全局与所有主机限域条目全撤（「全部撤销」按钮的语义）；
          host="" = 只撤全局条目；host="h" = 只撤该主机的条目。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    from routes.agent import append_record_async

    pg = _get_global_permission_gate_sync()
    pg.revoke(tool, command=command, conversation_id=conversation_id, host=host)
    # C-review C2 修复：撤销动作同样落审计链
    await append_record_async(
        action=f"permission_revoke:{tool}",
        conclusion="revoked",
        # S2-04：这两处（撤销 / 手动登记永久授权）按设计不属于任何 run —— 它们改的是
        # 跨 run 的 allowlist 与会话级状态，run_id 留空是正确取值，不是漏传。
        target_type="tool",
        # 撤销标的带主机前缀（与 allowlist 行同形），事后能分辨「撤掉了某台
        # 机器的预授权」还是「撤掉了全局条目」。host=None = 全撤，不加前缀。
        target_id=(f"@{host}|{command or tool}" if host else (command or tool)),
        command_raw=command or "",
        result=f"host={host if host is not None else '所有作用域'}",
    )
    return {"tool": tool, "granted": False, "host": host}


@router.get("/agent/permission/list")
async def list_tool_permissions():
    """查询已永久授权的工具列表

    审查R2修复：改 async def —— `sorted(pg._confirmed_permanent)` 是跨多行的
    迭代快照，此前在 anyio 线程池执行时与循环内 grant/revoke/check 的并发
    add/discard 相撞可直接抛 RuntimeError（迭代中集合被改）。

    `tools` 保持原来的字符串数组语义（前端与 test_admin_permission_and_webkeys 都按此消费）。
    `entries`/`ttl_hours`（E3）是附加信息：授权何时写入、何时到期，面板要能诚实
    回答"这条预登记还会有效多久"，而不是只报"已授权"。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    pg = _get_global_permission_gate_sync()
    return {
        "tools": sorted(pg._confirmed_permanent),
        "entries": pg.permanent_entries(),
        "ttl_hours": max(0, int(pg.allowlist_ttl_hours)),
    }


class AllowPermissionRequest(BaseModel):
    """手动登记永久授权（免去先触发审批弹窗再点"始终允许"的路径）"""
    tool: str = Field(..., min_length=1, max_length=160, description="工具名")

    @field_validator("tool")
    @classmethod
    def reject_mcp(cls, value: str) -> str:
        if value.startswith("mcp__"):
            raise ValueError("插件工具不提供永久授权，请在审批卡里逐次授权")
        return value
    command: Optional[str] = Field(None, max_length=4096,
                                   description="命令执行工具的绑定命令；省略 = 工具级授权")
    host: Optional[str] = Field(
        None, max_length=255,
        description="限定到该主机（allowlist 写成 @host|键）；省略 = 全局条目")


@router.post("/agent/permission/allow")
async def allow_tool_permission(req: AllowPermissionRequest):
    """手动写入永久 allowlist（与审批"始终允许"同源，落 allowlist 文件 + 审计）

    审查R2修复：改 async def 回到事件循环单线程操作 PermissionGate 状态，
    与 grant 端点同一姿态（详见 grant 路由 docstring）。

    安全边界：仅登记，不放宽任何判定 — 命令工具写入的是 `tool:cmdhash`
    命令级绑定键（审查 M-3 语义）。hardline 底线命令在 FilesystemGuard 层
    无条件阻断，allowlist 无法放行 —— 因此本端点对 hardline 命令直接 400
    拒绝（Q-B4①）：原实现照单登记，返回 200 的「已授权」是一条 FilesystemGuard
    必然拦下的死预授权，面板显示与真实判定自相矛盾。

    主机维度：提供 host 时写成 `@host|键`（只对该主机生效）；省略 = 全局
    （对所有主机生效）。默认值刻意不用「当前连接主机」—— 这个端点没有
    run 上下文，猜测目标主机会把「全局登记」静默变成「只对某台生效」。
    """
    from agent.harness.reins import _get_global_permission_gate_sync
    from agent.harness.reins.gate import (
        is_valid_allowlist_entry, normalize_host, scoped_key)
    from routes.agent import append_record_async

    # hardline 预检：与 grant 端点同一判据（classify_command，只拦 hardline）。
    # 手动登记入口是自由文本框，恰恰是最需要这道闸的地方。
    if req.command and str(req.command).strip():
        from agent.harness.safety import classify_command
        _level, _reason = classify_command(str(req.command))
        if _level == "hardline":
            raise HTTPException(
                status_code=400,
                detail=(f"命令不可放行：{_reason or '命中 hardline 底线'}。"
                        "hardline 命令被无条件阻断，allowlist 无法放行，"
                        "登记预授权不会生效；请改用安全的替代方案。"),
            )
    pg = _get_global_permission_gate_sync()
    host = (req.host or "").strip()
    try:
        grant_key = pg.grant_key_for(req.tool, req.command, scope="always")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 主机段与键拼成一行再校验：复用读侧同一判据，避免「面板允许输入
    # `a|b`，写进文件后读侧判非法丢弃」这种写得进读不回的形态。
    entry = scoped_key(host, grant_key)
    if not is_valid_allowlist_entry(entry):
        raise HTTPException(
            status_code=400,
            detail=("主机标识不在授权清单允许的格式内"
                    "（期望 [A-Za-z0-9_.:-]，不接受空白、换行或斜杠）"),
        )
    await append_record_async(
        action=f"permission_allow_manual:{req.tool}",
        # S2-04：手动登记永久授权不属于任何 run（它写的是跨 run 的 allowlist），
        # run_id 留空是正确取值，不是漏传 —— 与 permission_revoke 同一条口径。
        # environment 同样留空：登记本身不针对某一次运行；作用范围改由
        # host 维度表达（全局 or 某台主机），写进 result 供审计页辨认。
        # "granted" 不在 audit.append_record 的结论枚举里，会被静默改写为 "executed"
        # —— 手动登记永久授权就伪装成了"执行过一条命令"。用同义且在枚举内的值。
        conclusion="approved",
        target_type="tool",
        target_id=(f"@{normalize_host(host)}|{req.command or req.tool}"
                   if host else (req.command or req.tool)),
        command_raw=req.command or "",
        result=f"host={normalize_host(host) or '全局'},entry={entry}",
        # 与 grant 同一姿态：登记生效前必须已有记录，写不进去则不登记
        critical=True,
    )
    pg.grant(req.tool, scope="always", command=req.command, host=host)
    return {"tool": req.tool, "grant_key": grant_key,
            "host": normalize_host(host), "granted": True}


class SafetyClassifyRequest(BaseModel):
    """批量下发等「人主导通道」的执行前判定请求"""
    commands: list[str] = Field(..., min_length=1, max_length=50,
                                description="待执行命令原文，1-50 条")

    @field_validator("commands")
    @classmethod
    def _each_within_limit(cls, v: list[str]) -> list[str]:
        for i, c in enumerate(v):
            if len(c) > 4096:
                raise ValueError(f"第 {i + 1} 条命令超过 4096 字符")
        return v


@router.post("/agent/safety/classify")
def classify_safety_commands(req: SafetyClassifyRequest):
    """命令风险判定的唯一来源（safety.classify_command）对外暴露，只读无状态。

    为什么要这个端点：批量下发是「一个人的动作 × N 台机器」的放大点，此前它只在
    渲染层自带一份词表来弹确认（旧 bulkCommandDiff.ts 的 DANGEROUS_PATTERNS，
    现已随安全分类端点下线）—— 那是
    第四份规则表，与后端 HARDLINE/DANGEROUS 两套口径各写各的，且 mkfs、rm -rf /
    在那条路上只是"多问一句"，点确认照样打到 N 台上。前端自此不再自带规则。

    口径与 shell_exec 完全一致：模型自由文本 → classify_command（含「未入只读白
    名单即审批」）。不区分 constructed 模板口径，因为这里的输入就是人敲的原文。

    本端点只判定，不放行任何操作：不写审计、不建 run、不碰 allowlist。真正的阻断
    发生在调用方（批量下发据 hardline 直接中止）。
    """
    from agent.harness.command_explain import explain_for_reason
    from agent.harness.safety import classify_command, get_normalized_for_display
    results = []
    for raw in req.commands:
        level, reason = classify_command(raw)
        display = get_normalized_for_display(raw)
        expl = explain_for_reason(reason or "")
        results.append({
            "command": raw,
            "level": level,
            "reason": reason or "",
            "normalized": display,
            # 中文后果说明：dangerous 的确认框给人看得懂的理由，而不是英文 reason
            "explain": expl.text,
        })
    return {"results": results}


class DenyCommandRequest(BaseModel):
    """visible_terminal 拒绝命令请求 — 标记某 run 下某命令被拒绝"""
    tool: str = Field(..., description="工具名（通常为 shell_exec）")
    command: str = Field(..., description="被拒绝的命令")
    run_id: str = Field("", description="运行 ID（拒绝记录按 run_id 隔离）")


@router.post("/agent/permission/deny-command")
async def deny_command(req: DenyCommandRequest):
    """标记某 run 下某命令被用户拒绝

    审查R2修复：改 async def 回到事件循环单线程操作 PermissionGate 状态，
    与 grant 端点同一姿态（详见 grant 路由 docstring）。

    visible_terminal 拒绝时调用：后续该 run 再次请求此命令时 PermissionGate
    check_permission 直接返回 False，不会再次触发 permission_needed 卡片。
    """
    from agent.harness.reins import _get_global_permission_gate_sync, approval_wait
    from routes.agent import _run_environment, append_record_async

    if not req.run_id:
        raise HTTPException(status_code=400, detail="deny_command 必须携带 run_id（拒绝记录按 run 隔离）")
    pg = _get_global_permission_gate_sync()
    try:
        denied_key = pg.deny_command(req.tool, req.command, req.run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 先把拒绝标记落了再唤醒：醒来时重判会走「命令曾被用户拒绝」分支，
    # 顺序反过来就是「用户拒了、run 却拿到一句放行」。
    approval_wait.resolve_for_call(
        req.run_id, req.tool, denied_key, approval_wait.DENIED)
    # 审计记录
    await append_record_async(
        action=f"permission_deny_command:{req.tool}",
        conclusion="denied",
        run_id=req.run_id,
        target_type="tool",
        target_id=denied_key,
        # 拒绝同样是「对某台主机的处置决定」：环境列取 run 的服务端判定，
        # 否则一次 prod 上的拒绝在 prod 审计视图里查不到
        environment=_run_environment(req.run_id),
        command_raw=req.command,
    )
    return {"tool": req.tool, "command": req.command,
            "run_id": req.run_id, "denied_key": denied_key}


def _release_run_interaction_state(run_id: str) -> tuple[int, int]:
    """回收 run 级交互状态：once 授权池 + 拒绝记录 + 澄清向导上下文 + 审批等待项。

    ReflectionRein 只在 AGENT_COMPLETE / AGENT_FAILED 正常终态触发。用户点"停止"
    或客户端断连时 driver 直接被 cancel，它不会跑 —— 三个池子便随每次中止的 run
    无界增长（授权池残留尤其危险：无人消费的 once 额度只能等进程重启）。
    全部是同步字典操作（内部无 await），因此无需 shield 也不会被取消打断。

    Returns:
        (释放的 once 授权条数, 释放的拒绝记录条数)
    """
    from agent.harness.reins import (
        _get_global_permission_gate_sync, approval_wait, release_clarification_run)
    pg = _get_global_permission_gate_sync()
    released = pg.cleanup_run_once(run_id)
    try:
        # 先把还在等用户点击的调用叫醒（裁决=aborted → 不重判，走原有拦截路径）。
        # 漏这一步的话，release-run 后等待项还挂着，下一次 grant 会去 set_result
        # 一个没人在等的 future：日志里只剩一句「已授权」，run 早已不在。
        approval_wait.abort_run(run_id)
    except Exception:
        logger.warning("release_run: 审批等待项回收异常 run=%s", run_id, exc_info=True)
    try:
        denied = pg.cleanup_run_denied(run_id)
    except Exception:
        logger.warning("release_run: denied 清理异常 run=%s", run_id, exc_info=True)
        denied = 0
    try:
        release_clarification_run(run_id)
    except Exception:
        logger.warning("release_run: 澄清回收异常 run=%s", run_id, exc_info=True)
    return released, denied


@router.post("/agent/permission/release-run/{run_id}")
async def release_run_permissions(run_id: str):
    """手动清理某次 run 的全部 once 授权/拒绝记录/澄清状态（前端显式终止或超时时调用）

    审查R2修复：改 async def —— _release_run_interaction_state 同时被 SSE 侧
    finally（事件循环线程）调用，此前本路由在 anyio 线程池里 pop 同一批 dict，
    两处并发互为竞态源；回到循环后互斥天然成立。审计文件写放线程池。

    ReflectionRein 在 AGENT_COMPLETE / AGENT_FAILED 时自动回收这三样，
    本接口作为兜底：前端在用户点击"停止"或 run 被强制终止时主动释放。
    SSE 侧的 finally 也调用同一 helper —— 客户端直接断连时不会经过本接口。
    """
    from routes.agent import _run_environment, append_record_async

    released, denied_count = _release_run_interaction_state(run_id)
    await append_record_async(
        action=f"permission_release_run:{run_id}",
        conclusion="released",
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        # 释放动作也属于这次 run 的环境（与 grant / deny 同口径）
        environment=_run_environment(run_id),
        result=f"released={released} denied={denied_count}",
    )
    return {"run_id": run_id, "released": released, "denied": denied_count}
