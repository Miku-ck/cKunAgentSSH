"""
定时任务调度引擎 — 负责按「分钟间隔 / 每天 / 每周 / 每月」周期触发一次 Agent 运行。

设计：
- 全局单例 ScheduleEngine（由 main.py lifespan 启停）。
- 后台循环每 30s 扫描一次启用的任务，用 next_run_at 判定是否到期。
- 到期后在后台 asyncio 任务里执行一次「无头」Agent 运行（复用 Harness），
  把最终回答写入 schedule_runs.summary，并刷新 next_run_at 防止重入。
- next_run_at 统一以 UTC naive 字符串存储（与 database._now() 一致）。

时区说明：用户填写的 time_hour/time_minute/weekday/day_of_month 视为本地时间，
计算 next_run 时先转成等价 UTC 瞬时再比较，避免与数据库 UTC 时间戳错位。
"""
import asyncio
import calendar
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import database as db
from agent.harness import Harness, HarnessConfig
from agent.harness.contracts import AgentConfig as HarnessAgentConfig
from agent.harness.llm_client import StreamLLMClient
from agent.cost_tracker import CostTracker
from agent.electron_bridge import ElectronBridge

logger = logging.getLogger(__name__)

_SCHEDULE_POLL_SECONDS = 30
_SCHEDULE_MAX_ITERATIONS = 8
_SCHEDULE_CONVERSATION_PREFIX = "sched-"
# schedule_runs 清理节流间隔（秒）
_RUNS_PRUNE_INTERVAL_S = 6 * 3600
# 模型未配置 temperature 时的兜底值（与 models_config 列默认值一致）
_DEFAULT_TEMPERATURE = 0.7


def _num_param(value, default: float) -> float:
    """数值型模型参数取值：只有缺失/NULL 才用默认值。

    `model_cfg.get("temperature") or 0.7` 会把 0.0 静默抬成 0.7 —— 设置页允许
    temperature=0（ge=0，确定性输出），交互式运行按 0 走、定时任务按 0.7 走，
    同一模型配置两条通道行为不一致。
    """
    return default if value is None else float(value)


class ScheduleEngine:
    """后台定时任务调度器（单例）。"""

    def __init__(self, poll_seconds: int = _SCHEDULE_POLL_SECONDS):
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._executing: set[str] = set()  # 正在执行的 schedule_id，防重入
        # 审查修复（P2）：登记进行中的 _run_task — 原 stop() 只取消轮询循环，
        # 已 create_task 的任务（无引用、detached）会继续跑 SSH 会话，
        # schedule_runs 在进程退出后滞留 'running'
        self._running_tasks: dict[str, asyncio.Task] = {}
        # 删除任务时被打断的 schedule_id：区分 CancelledError 文案
        # （"任务已删除" vs "服务停止"），避免 cancel 与 finally 的竞态误写
        self._deleted_sids: set[str] = set()
        self._last_prune_at = 0.0  # schedule_runs 清理节流（monotonic）

    # ── 生命周期 ──
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("ScheduleEngine 已启动 (poll=%ds)", self._poll_seconds)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._stop_event.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # 审查修复（P2）：取消进行中的定时任务并等待其把 DB 状态落定
        # （_run_task 的 except CancelledError 会标记 run 为 failed/中断）
        if self._running_tasks:
            for t in self._running_tasks.values():
                if not t.done():
                    t.cancel()
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)
            self._running_tasks.clear()

    def ensure_started(self) -> None:
        self.start()

    # ── 后台循环 ──
    async def _run_loop(self) -> None:
        # 启动后先扫一遍（对遗留/新建任务立即生效），之后按周期轮询。
        # try 必须在 while 之内：原实现整圈包在单个 try 里，一次 _tick_once 抛异常
        # （DB 瞬时 locked、next_run_at 脏值）就让自动调度永久停摆，
        # 而且日志之后毫无征兆 —— 只有下一次 create_schedule/run_now 的
        # ensure_started 才会把它救回来。
        while not self._stop_event.is_set():
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "ScheduleEngine tick 失败，%ds 后重试: %s", self._poll_seconds, e
                )
            await asyncio.sleep(self._poll_seconds)

    async def _tick_once(self) -> None:
        schedules = await asyncio.to_thread(db.list_enabled_schedules)
        now = utcnow()
        for s in schedules:
            if s["id"] in self._executing:
                continue
            due = False
            if not s.get("next_run_at"):
                due = True
            else:
                try:
                    due = _parse_ts(s["next_run_at"]) <= now
                except Exception:
                    due = True
            if due:
                # 审查修复：必须 await — trigger 是 async def，原实现未 await
                # 导致协程被静默丢弃，后台轮询从未真正触发任何到期定时任务
                # （next_run_at 也不推进，自动调度整体失效，仅手动"立即执行"可用）
                await self.trigger(s)

    async def trigger(self, sched: dict, *, manual: bool = False) -> bool:
        """触发一次执行：先刷新 next_run_at，再后台跑 Agent。

        返回 False 表示该任务正在执行、本次触发被丢弃（路由据此回 409，
        而不是对着用户谎报「已触发立即执行」）。

        manual=True（用户点"立即执行"）不推进 next_run_at：手动跑不消耗本次
        定时。若手动执行期间恰好到期，tick 被 _executing 防重入跳过，执行完
        后的下一个 tick（≤poll_seconds）发现 next_run_at 已是过去值自动补跑，
        即"延后执行"而非"吞掉"。
        """
        sid = sched["id"]
        if sid in self._executing:
            logger.info("schedule=%s 正在执行，跳过重复触发", sid)
            return False
        # 去重标记必须落在第一个 await 之前：原实现先 await 线程池写 next_run_at
        # 再 add，轮询 tick 与手动「立即执行」同时到达时两边都通过上面的检查，
        # 同一个定时任务并发跑两个 Agent run（双开 SSH、双份模型开销），
        # 且先结束那个的 finally 会把后起任务的标记和 _running_tasks 条目一起清掉。
        self._executing.add(sid)
        try:
            if not manual:
                next_at = compute_next_run(sched)
                await asyncio.to_thread(db.set_schedule_next_run, sid, next_at)
        except Exception as e:
            logger.warning("schedule=%s 计算 next_run 失败: %s", sid, e)
        # 审查修复：执行标记须保持到任务结束（_run_task 的 finally 中移除）。
        # 原实现在 DB 写入后立即 discard，任务尚未开始就释放了去重锁，
        # 运行时间超过 tick 间隔（30s）的任务会被每次 tick 重复触发，
        # 并发启动多个 Agent 运行。
        try:
            t = asyncio.get_running_loop().create_task(self._run_task(sched.copy()))
            self._running_tasks[sid] = t
        except Exception:
            # 无运行循环或 create_task 失败：回滚标记，避免任务永久卡死
            self._executing.discard(sid)
            raise
        self._maybe_prune_runs()
        return True

    async def remove_schedule(self, sid: str) -> bool:
        """删除定时任务的唯一实现（REST DELETE 与 AI 工具 cancel 共用）。

        先取消在途执行体（无头 Agent 会继续操作 SSH，不能让它跑在已删除的
        任务上），把滞留 running 的 run 行标为终态，再删 schedules 行
        （schedule_runs 由 FK CASCADE 一并删除）。
        不 await 被取消的 task：DELETE 需及时返回，收尾交给 _run_task 自己跑。
        """
        t = self._running_tasks.get(sid)
        if t is not None and not t.done():
            self._deleted_sids.add(sid)
            t.cancel()
            # 未开始执行的 task 被 cancel 时协程体（含 _run_task 的 finally）根本
            # 不会运行，_executing/_running_tasks 条目会永久泄漏 —— done callback
            # 兜底回收；对已跑完 finally 的 task 这些 discard 是幂等空操作
            t.add_done_callback(lambda _t, s=sid: (
                self._executing.discard(s),
                self._running_tasks.pop(s, None),
                self._deleted_sids.discard(s),
            ))
        await asyncio.to_thread(
            db.fail_running_schedule_runs, sid, "任务已删除，执行被取消"
        )
        return await asyncio.to_thread(db.delete_schedule, sid)

    def _maybe_prune_runs(self) -> None:
        """低频清理 schedule_runs（每 6h 最多一次，best-effort）。

        放在 trigger 末尾而非 tick 循环：只在实际有触发活动时顺带清理，
        空转的 tick 不碰 DB；to_thread 避免同步 sqlite 占用事件循环。
        """
        now = time.monotonic()
        if now - self._last_prune_at < _RUNS_PRUNE_INTERVAL_S:
            return
        self._last_prune_at = now
        try:
            asyncio.get_running_loop().create_task(
                asyncio.to_thread(db.prune_schedule_runs)
            )
        except RuntimeError:
            pass  # 无运行循环（单测直调）— 跳过即可

    # ── 无头执行 ──
    async def _run_task(self, sched: dict) -> None:
        sid = sched["id"]
        run_id = uuid.uuid4().hex[:12]
        started = db._now() if hasattr(db, "_now") else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        run_in_db = False
        try:
            # 审查修复：DB 初始化移入 try — 原实现在 try 外执行，DB 异常时
            # finally 不执行，_executing 标记永不释放，该任务被永久视为执行中，
            # 后续所有 tick 全部跳过直至进程重启
            await asyncio.to_thread(db.create_schedule_run, sid, run_id)
            await asyncio.to_thread(
                db.mark_schedule_run, sid, "running", last_run_at=started, last_run_id=run_id
            )
            run_in_db = True
            content, err = await self._run_agent_once(sched, run_id)
            snippet = (content or "").strip()
            status = "failed" if (not snippet and err) else "completed"
            await asyncio.to_thread(
                db.update_schedule_run, run_id,
                status=status, summary=snippet, error=err or "",
            )
            await asyncio.to_thread(db.mark_schedule_run, sid, status)
            logger.info("schedule=%s run=%s %s", sid, run_id, status)
        except asyncio.CancelledError:
            # 审查修复（P2）：关停取消 — 把 run 标记为中断后重新抛出，
            # 否则 schedule_runs 永久滞留 'running'
            cancelled_by_delete = sid in self._deleted_sids
            logger.warning(
                "schedule=%s run=%s 被%s取消", sid, run_id, "删除" if cancelled_by_delete else "关停"
            )
            if run_in_db:
                try:
                    await asyncio.to_thread(
                        db.update_schedule_run, run_id,
                        status="failed",
                        error="任务已删除，执行被取消" if cancelled_by_delete else "服务停止，任务被中断",
                    )
                    await asyncio.to_thread(db.mark_schedule_run, sid, "failed")
                except BaseException:
                    pass
            raise
        except Exception as e:
            logger.exception("schedule=%s run=%s 执行失败", sid, run_id)
            await asyncio.to_thread(
                db.update_schedule_run, run_id, status="failed", error=f"{type(e).__name__}: {e}"
            )
            await asyncio.to_thread(db.mark_schedule_run, sid, "failed")
        finally:
            self._executing.discard(sid)
            self._running_tasks.pop(sid, None)
            self._deleted_sids.discard(sid)
            # 审查修复（P2）：清理本 run 的内存 todo 列表 — 定时 run 走独立
            # 路径，不经过交互式 Agent 的清理钩子，模块级 dict 会缓慢泄漏
            try:
                from agent.tools.builtin.todo_write import clear_todos
                clear_todos(run_id)
            except Exception:
                pass

    async def _run_agent_once(self, sched: dict, run_id: str) -> tuple[str, str]:
        """组装 Harness 无头运行一次，返回 (最终 report 文本, 内部错误信息)。"""
        model_cfg = await asyncio.to_thread(self._resolve_model)
        if not model_cfg:
            raise RuntimeError("未配置默认模型，无法执行定时任务")

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
        from agent.tools import create_registry_for_mode
        tool_registry = create_registry_for_mode("ssh")
        cost_tracker = CostTracker(model_id=model_name)
        harness.assemble(
            llm_client=llm_client,
            tool_registry=tool_registry,
            cost_tracker=cost_tracker,
        )

        prompt = (sched.get("task_prompt") or "").strip()
        if not prompt:
            prompt = "对当前环境执行一次例行检查，输出结构化报告。"
        host = (sched.get("target_host") or "").strip() or None

        # ── 自动建立 SSH 会话 ──
        # 定时任务为无头执行，主进程没有为该主机预建会话。这里临时按连接 ID
        # 建立一条轻量 SSH 会话，把真实会话 ID 交给 Agent 使用；任务结束后
        # 在 finally 中关闭，避免连接资源泄漏。
        from agent.electron_bridge import BridgeError, BridgeErrorCode, classify_bridge_error
        bridge = ElectronBridge()
        ssh_session_id = ""
        if host:
            # 1. 环境变量检查 — 快速拒绝未配置的场景
            if not bridge.is_available():
                raise BridgeError(
                    BridgeErrorCode.NOT_CONFIGURED,
                    "定时任务需要 Electron bridge，但未注入端口/token",
                )
            # 2. 探活检查 — 组装 Harness/LLM 开销大（数百毫秒），提前确认桥服务
            #    可达可避免资源浪费；失败意味着端口/token 过期（孤儿进程、主进程
            #    未启动等），给出明确操作指引。
            #    用 probe() 而非 ping()：需要具体失败原因码写入 run summary。
            #    原实现把 ping 的所有失败一律报成 PORT_CLOSED，导致"端口在监听但
            #    无响应（假监听/超时）"被错误描述为"端口已关闭"，把排查方向带偏。
            try:
                probe_error = await bridge.probe()
            except Exception as e:
                # probe 内部已全量捕获异常，此处仅为防御未来改动
                probe_error = classify_bridge_error(e)
            if probe_error is not None:
                logger.warning(
                    "schedule=%s 桥接探活失败 [%s] port=%s",
                    sched["id"], probe_error.value, bridge.port,
                )
                raise BridgeError(probe_error, f"port={bridge.port}")
            # 3. 正式建连
            try:
                conn = await bridge.connect_ssh(host)
                ssh_session_id = str(conn.get("sessionId") or "")
                if not ssh_session_id:
                    raise BridgeError(
                        BridgeErrorCode.HTTP_ERROR,
                        "connect_ssh 返回空 sessionId",
                    )
                logger.info("schedule=%s 已为目标主机建立会话 %s", sched["id"], ssh_session_id)
            except BridgeError:
                raise
            except Exception as e:
                # 非结构化异常 — 反向推断桥接错误码
                code = classify_bridge_error(e)
                logger.warning(
                    "schedule=%s 为目标主机建会话失败（connection=%s）: [%s] %s",
                    sched["id"], host, code, e,
                )
                raise BridgeError(code, str(e), original=e if isinstance(e, Exception) else None)

        # 输出 token 上限不能设得等于/接近上下文上限，否则一旦带入输入就必然
        # 超过模型上下文窗口 → vLLM 返回 400，任务瞬时失败且无输出。
        # 此处钳制为上下文上限的一半，保留足够输出空间又不会触发 400。
        out_limit = int(model_cfg.get("max_tokens") or ctx_limit)
        safe_out = max(1, min(out_limit, ctx_limit // 2))
        rt = model_cfg.get("reasoning_tokens")
        safe_reasoning = min(int(rt or 0), safe_out) if rt else None

        cfg = HarnessAgentConfig(
            max_iterations=_SCHEDULE_MAX_ITERATIONS,
            temperature=_num_param(model_cfg.get("temperature"), _DEFAULT_TEMPERATURE),
            max_tokens=safe_out,
            max_reasoning_tokens=safe_reasoning,
            enable_thinking=True,
            enable_tools=True,
            enable_intention_guide=False,
            enable_dynamic_budget=False,
        )
        system_prompt = _build_schedule_system_prompt(sched)
        conversation_id = _SCHEDULE_CONVERSATION_PREFIX + (sched["id"] or "")

        done_content = ""
        error_msg = ""
        try:
            async for ev in harness.run(
                task=prompt,
                mode="ssh",
                conversation_id=conversation_id,
                ssh_session_id=ssh_session_id,
                environment="prod",
                system_prompt=system_prompt,
                history=[],
                memories=[],
                config=cfg,
                run_id=run_id,
            ):
                if ev.kind == "error":
                    # harness 内部错误（如 LLM 调用失败）不要让用户看到"无输出"，回传展示
                    error_msg = ((ev.payload or {}).get("message") or "") or error_msg
                elif ev.kind == "done":
                    payload = ev.payload or {}
                    done_content = payload.get("content", "") or done_content
        finally:
            # 任务结束（含异常路径）关闭自动建立的 SSH 会话，释放连接资源
            if ssh_session_id:
                try:
                    await bridge.disconnect_ssh(ssh_session_id)
                    logger.info(
                        "schedule=%s 已关闭会话 %s", sched["id"], ssh_session_id
                    )
                except Exception as e:
                    logger.warning(
                        "schedule=%s 关闭会话 %s 失败: %s",
                        sched["id"], ssh_session_id, e,
                    )
        return done_content, error_msg

    @staticmethod
    def _resolve_model() -> dict | None:
        """取默认模型（未掩码）供 Harness 调用。"""
        default = db.get_default_model(None, mask_key=False)
        if not default:
            return None
        mid = default.get("id") or default.get("model_id")
        if not mid:
            return default
        cfg = db.get_model_config(mid, mask_key=False)
        return cfg or default


# 全局单例
_engine: ScheduleEngine | None = None


def get_schedule_engine() -> ScheduleEngine:
    global _engine
    if _engine is None:
        _engine = ScheduleEngine()
    return _engine


# ── 时间工具 ───────────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _local_tz() -> datetime.tzinfo:
    return datetime.now().astimezone().tzinfo


def _local_to_utc(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    local = datetime(y, mo, d, h, mi, tzinfo=_local_tz())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _local_now(now_utc_naive: datetime) -> datetime:
    """UTC naive → 本地时区 aware datetime（daily/weekly/monthly 取「今天/本周/本月」的基准）。"""
    return now_utc_naive.replace(tzinfo=timezone.utc).astimezone(_local_tz())


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def compute_next_run(sched: dict, now: datetime | None = None) -> str:
    """按规则计算下一次执行时间（UTC naive 字符串，保证 > now）。

    语义（三类周期共用同一形态）：先算"最近一个目标时刻"（可能就在今天/
    本周/本月），未过则取之，已过则滚动到下一周期。旧实现 daily/weekly
    无条件从"明天/下周"起步、不与 now 比较 — 今天时刻还没到时也会晚一天
    （实测：daily 23:00 在 10:00 触发会算出明天 23:00）。
    """
    now = now or utcnow()
    st = sched.get("schedule_type") or "interval"
    if st == "interval":
        # 注意：interval_minutes 保留 `or 60` — 0 是危险值（每 tick 触发），
        # 落到 60 是安全方向（路由层另有 ge=1 校验）
        minutes = int(sched["interval_minutes"]) if sched.get("interval_minutes") else 60
        return _fmt(now + timedelta(minutes=minutes))

    # None 判定而非 `or`：0 是合法值（午夜 0 点）。旧实现 `time_hour or 9`
    # 会把 0 点任务静默挪到 9 点（路由校验 0<=hour<=23 明确允许 0）。
    h = int(sched["time_hour"]) if sched.get("time_hour") is not None else 9
    mi = int(sched["time_minute"]) if sched.get("time_minute") is not None else 0

    if st == "daily":
        # 审查R2修复：原实现以 UTC 日期当「今天」且只滚动一次 — UTC+8 下本地
        # 午夜窗口（00:00–07:59）内算出的 next_run 是过去时刻，落库即到期，
        # _tick_once 每 30s 重复触发同一个定时任务（headless run + SSH 执行
        # 反复开跑）。基准改为本地日期，并用 while 滚动兜底保证 > now。
        ld = _local_now(now).date()
        while True:
            cand = _local_to_utc(ld.year, ld.month, ld.day, h, mi)
            if cand > now:
                return _fmt(cand)
            ld += timedelta(days=1)

    if st == "weekly":
        target = int(sched["weekday"]) if sched.get("weekday") is not None else 0  # 0=周一
        ld = _local_now(now).date()
        ld += timedelta(days=(target - ld.weekday()) % 7)
        while True:
            cand = _local_to_utc(ld.year, ld.month, ld.day, h, mi)
            if cand > now:
                return _fmt(cand)
            ld += timedelta(days=7)

    if st == "monthly":
        dom = int(sched["day_of_month"]) if sched.get("day_of_month") is not None else 1
        local = _local_now(now)
        y, mo = local.year, local.month
        while True:
            last = calendar.monthrange(y, mo)[1]
            cand = _local_to_utc(y, mo, min(dom, last), h, mi)
            if cand > now:
                return _fmt(cand)
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)

    return _fmt(now + timedelta(hours=1))


def _build_schedule_system_prompt(sched: dict) -> str:
    parts = []
    try:
        from prompts import _current_time_str, MODE_PROMPTS
        parts.append(f"[系统时间] 当前时间：{_current_time_str()}。")
        mode_prompt = MODE_PROMPTS.get("ssh", "")
        if mode_prompt:
            parts.append(mode_prompt)
    except Exception:
        pass
    parts.append(
        "你现在以「定时任务」方式自动执行。请独立完成给定任务，"
        "并输出结构化中文报告：先给总体结论，再用 ## 二级标题分节说明发现的问题、"
        "已执行的操作与后续建议。"
    )
    parts.append("请以只读、安全的操作为主，涉及破坏性操作时先说明计划再执行。")
    return "\n\n".join(parts)