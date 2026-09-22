"""
意识 tick — Agent 的后台心跳认知活动

v2（spec §5/§9）：后台职责收敛为三类——
  1. reconcile_jobs    — 消费 memory_jobs（Reconciler 对账，唯一写入口；
                         唯一可能调 LLM 的环节，失败按 job 重试 3 次）
  2. trim/decay        — 规则维护：按 max_items 修剪 + 长期零命中衰减（零 LLM）
  3. reflect_failures  — 失败模式规则识别，产物投 candidate_fact job
                         （kind=procedural，统一记忆而非独立策略表）

v1 的 consolidate/touch 任务已随统一记忆重构移除：固化被 Reconciler 的
candidate_fact 通路取代；touch 自增强回路整体删除（热度只在真正注入时由
retriever 计）。

设计原则:
  - 失败静默：任何子任务异常都 try/except，不影响 tick 主循环
  - 可配置：间隔、单次 job 上限、单任务超时；修剪上限来自记忆设置 max_items
  - 优雅关闭：通过 cancel event 停止
"""
import asyncio
import logging
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TickConfig:
    """意识 tick 配置"""
    interval_seconds: int = 300        # tick 间隔（默认 5 分钟）
    max_jobs_per_tick: int = 20        # 单 tick 消费 job 上限（spec §3）
    failure_lookback: int = 10         # 反思失败时回看的最近 run 数
    task_timeout_seconds: int = 180    # 对账含 LLM 往返，超时放宽
    max_items: int = 0                 # >0 时覆盖记忆设置的修剪上限（测试注入点）


class ConsciousnessTick:
    """后台意识 tick — 周期性对账 + 规则维护"""

    def __init__(self, config: TickConfig | None = None):
        self.config = config or TickConfig()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # 互斥锁：trigger_now 与后台 _run_loop 调用 _tick_once 时互斥
        self._tick_lock = asyncio.Lock()
        self._tasks: dict[str, Callable[[], Awaitable[dict]]] = {
            "reconcile_jobs": self._reconcile_jobs,
            "reflect_failures": self._reflect_failures,
            "trim_pools": self._trim_pools,
            "decay_importance": self._decay_importance,
        }
        # 上次执行时间（供调试查看）
        self._last_run: dict[str, datetime] = {}
        # 最近一次 tick 结果（供调试/查询）
        self.last_tick_result: dict | None = None
        # 同一失败批次只反思一次（P2 审查教训保留：无签名去重会淹没记忆表）
        self._reflected_signatures: deque[str] = deque(maxlen=500)
        self._mem_max_items = 0

    # ── 生命周期 ──

    def start(self) -> None:
        """启动后台 tick 任务"""
        if self._task is not None and not self._task.done():
            logger.warning("ConsciousnessTick 已在运行")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "ConsciousnessTick 已启动 (interval=%ds)", self.config.interval_seconds
        )

    def ensure_started(self) -> None:
        """
        懒启动 — 仅在首次 Agent 运行时触发后台 tick。

        避免应用启动即开 tick 导致纯聊天用户产生空 DB 查询开销。
        幂等：已运行则直接返回。
        """
        if self._task is None or self._task.done():
            self.start()

    async def stop(self) -> None:
        """停止后台 tick 任务"""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("ConsciousnessTick 已停止")

    async def _run_loop(self) -> None:
        """主循环：周期性执行 tick"""
        try:
            # 启动后等一个间隔再开始（避免与启动初始化抢资源）
            await asyncio.sleep(self.config.interval_seconds)
            while not self._stop_event.is_set():
                # 单次 tick 的异常不能带走主循环：_tick_once 开头的
                # db.get_memory_settings 在 SQLite 锁库时会抛 OperationalError，
                # 让它冒出去会静默杀死后台任务（异常无人 retrieve），
                # 记忆整理从此不再运行，只有聊天路径的 ensure_started 能拉起它。
                try:
                    await self._tick_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("ConsciousnessTick 单次 tick 失败，进入下一轮")
                # 等待下一个间隔，但可以被 stop_event 提前唤醒
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.config.interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass  # 正常超时，进入下一轮
        except asyncio.CancelledError:
            logger.info("ConsciousnessTick 主循环被取消")
            raise

    async def _tick_once(self) -> None:
        """执行一次 tick：四个任务按序全跑（对账优先，规则维护兜底容量）"""
        # 与 trigger_now 互斥，避免后台循环与手动触发并发执行
        async with self._tick_lock:
            import database as db

            # 读取记忆设置 — 关闭时跳过所有记忆相关任务
            mem_settings = await asyncio.to_thread(db.get_memory_settings)
            if not mem_settings["enabled"]:
                self.last_tick_result = {
                    "tick_at": _now_utc().isoformat(),
                    "executed": [],
                    "results": {"skipped": "memory disabled"},
                }
                return
            self._mem_max_items = mem_settings.get("max_items", 100)

            now = _now_utc()
            results: dict[str, dict] = {}
            for name, fn in self._tasks.items():
                try:
                    # wait_for 防止任务挂起（DB 锁、LLM/embedding 超时）阻塞后续任务
                    result = await asyncio.wait_for(
                        fn(), timeout=self.config.task_timeout_seconds,
                    )
                    results[name] = result
                    logger.info(
                        "ConsciousnessTick 任务 '%s' 完成: %s",
                        name, result.get("summary", ""),
                    )
                except asyncio.TimeoutError:
                    results[name] = {"error": f"超时 ({self.config.task_timeout_seconds}s)"}
                    logger.warning(
                        "ConsciousnessTick 任务 '%s' 超时 (%ds)",
                        name, self.config.task_timeout_seconds,
                    )
                except Exception as e:
                    results[name] = {"error": str(e)}
                    logger.warning(
                        "ConsciousnessTick 任务 '%s' 失败: %s", name, e, exc_info=True
                    )
                self._last_run[name] = now

            self.last_tick_result = {
                "tick_at": now.isoformat(),
                "executed": list(results.keys()),
                "results": results,
            }

    # ── 认知任务实现 ──

    async def _reconcile_jobs(self) -> dict:
        """消费 memory_jobs：相似检索 + 一次 LLM 批量裁决（唯一写入口）"""
        from agent.memory import reconcile

        result = await reconcile.process_jobs(max_jobs=self.config.max_jobs_per_tick)
        processed = result.get("processed", 0)
        applied = result.get("applied", {})
        summary = f"消费 {processed} job" + (f": {applied}" if processed else "")
        if result.get("failed"):
            summary += f", 失败 {result['failed']}"
        return {"summary": summary, **result}

    async def _reflect_failures(self) -> dict:
        """失败模式规则识别 → procedural candidate job（对账由 Reconciler 完成）"""
        import database as db
        from agent.memory import mstore, reconcile

        lookback = self.config.failure_lookback
        try:
            runs = await asyncio.to_thread(
                db.get_agent_runs, conversation_id=None, limit=lookback
            )
        except Exception as e:
            return {"summary": f"查询 runs 失败: {e}", "reflected": 0}

        failed = [r for r in runs if r.get("status") in ("failed", "max_reached")]
        if not failed:
            return {"summary": "无失败 run 可反思", "reflected": 0}

        signature = "|".join(
            f"{r.get('id')}:{r.get('status')}"
            for r in sorted(failed, key=lambda x: x.get("id") or "")
        )
        if signature in self._reflected_signatures:
            return {
                "summary": f"失败批次已反思过（{len(failed)} 个 run 无变化）",
                "reflected": 0,
                "failed_count": len(failed),
            }

        status_counts = Counter(r.get("status") for r in failed)
        patterns: list[str] = []
        if status_counts.get("max_reached", 0) >= 2:
            patterns.append(
                f"近期有 {status_counts['max_reached']} 次 Agent 达到迭代上限，"
                f"建议：拆分复杂任务，或增加 max_iterations 配置"
            )
        if status_counts.get("failed", 0) >= 2:
            patterns.append(
                f"近期有 {status_counts['failed']} 次 Agent 异常失败，"
                f"建议：检查模型 API 可用性、工具权限配置"
            )

        for pattern in patterns:
            await asyncio.to_thread(
                mstore.enqueue_job,
                reconcile.JOB_CANDIDATE,
                {
                    "items": [{
                        "title": "失败模式反思",
                        "content": f"[反思] {pattern}",
                        "kind": "procedural",
                        "scope": "global",
                        "importance": 0.7,
                    }],
                    "conv_id": None,
                    "run_id": None,
                },
                6,
            )
        # 签名在 job 入队后登记：入队本身只是写 memory_jobs，失败会抛异常走到
        # _tick_once 的 except，不会误登记。
        if patterns:
            self._reflected_signatures.append(signature)
        return {
            "summary": f"反思 {len(failed)} 次失败，投 {len(patterns)} 个模式候选",
            "reflected": len(patterns),
            "failed_count": len(failed),
        }

    async def _trim_pools(self) -> dict:
        """每 kind 保留 max_items 条，超出者 invalidate（pinned/pending 不参与）"""
        from agent.memory import mstore

        max_items = self.config.max_items or self._mem_max_items or 100
        retired = await asyncio.to_thread(mstore.trim_pools, max_items)
        return {"summary": f"修剪 {retired} 条超额记忆", "retired": retired}

    async def _decay_importance(self) -> dict:
        """90 天零命中降权，跌破地板失效"""
        from agent.memory import mstore

        result = await asyncio.to_thread(mstore.decay_memories)
        return {
            "summary": f"衰减 {result.get('decayed', 0)} 条 / 失效 {result.get('invalidated', 0)} 条",
            **result,
        }

    # ── 手动触发（供 API 调用） ──

    async def trigger_now(self) -> dict:
        """立即执行一次 tick（不影响后台调度），返回执行结果"""
        await self._tick_once()
        return self.last_tick_result or {"summary": "tick 已执行但无结果"}


# ── 全局单例（供 main.py 启动） ──

_global_tick: ConsciousnessTick | None = None


def get_global_tick() -> ConsciousnessTick:
    """获取全局 ConsciousnessTick 实例"""
    global _global_tick
    if _global_tick is None:
        _global_tick = ConsciousnessTick()
    return _global_tick
