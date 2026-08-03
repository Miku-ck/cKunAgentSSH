"""
意识 tick — Agent 的后台心跳认知活动

类似人类"潜意识的整理思绪"：在无用户交互时，Agent 后台周期性执行
轻量级元认知任务，让记忆系统保持"新鲜"和"健康"。

认知活动（每次 tick 选择 1-2 个执行，全部基于规则，不调 LLM）:
  1. consolidate_memories  — 从近期 episodic 记忆提取 semantic 知识
  2. reflect_on_failures   — 分析失败的 agent_runs，识别模式并写入 semantic 记忆
  3. prune_stale_memories  — 清理低重要性 + 长期未用的记忆
  4. touch_hot_memories    — 提升高频使用记忆的 importance

设计原则:
  - 零 LLM 调用：纯规则，避免后台成本
  - 失败静默：任何子任务异常都 try/except，不影响 tick 主循环
  - 可配置：间隔、单次任务上限、最低重要性阈值
  - 优雅关闭：通过 cancel event 停止
"""
import asyncio
import logging
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
    max_tasks_per_tick: int = 2        # 单次 tick 最多执行任务数
    min_importance: float = 0.3        # 低于此重要性 + 长期未用 → 清理
    stale_days: int = 7                # stale_days 天未用视为过期
    failure_lookback: int = 10         # 反思失败时回看的最近 run 数


class ConsciousnessTick:
    """后台意识 tick — 周期性执行元认知任务"""

    def __init__(self, config: TickConfig | None = None):
        self.config = config or TickConfig()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # 互斥锁：trigger_now 与后台 _run_loop 调用 _tick_once 时互斥
        self._tick_lock = asyncio.Lock()
        # 已注册的认知任务（name → coroutine function）
        self._tasks: dict[str, Callable[[], Awaitable[dict]]] = {
            "consolidate_memories": self._consolidate_memories,
            "reflect_on_failures": self._reflect_on_failures,
            "prune_stale_memories": self._prune_stale_memories,
            "touch_hot_memories": self._touch_hot_memories,
        }
        # 上次执行时间（用于轮询调度）
        self._last_run: dict[str, datetime] = {}
        # 最近一次 tick 结果（供调试/查询）
        self.last_tick_result: dict | None = None

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
                await self._tick_once()
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
        """执行一次 tick：选择若干任务并运行"""
        # 与 trigger_now 互斥，避免后台循环与手动触发并发执行
        async with self._tick_lock:
            # 延迟导入避免循环依赖
            import database as db

            # 读取记忆设置 — 关闭时跳过所有记忆相关任务
            mem_settings = db.get_memory_settings()
            if not mem_settings["enabled"]:
                self.last_tick_result = {
                    "tick_at": _now_utc().isoformat(),
                    "executed": [],
                    "results": {"skipped": "memory disabled"},
                }
                return

            # 选择本轮要执行的任务：优先选最久未执行的
            now = _now_utc()
            candidates = sorted(
                self._tasks.keys(),
                key=lambda name: self._last_run.get(name, datetime.min),
            )
            selected = candidates[: self.config.max_tasks_per_tick]

            results: dict[str, dict] = {}
            for name in selected:
                try:
                    result = await self._tasks[name]()
                    results[name] = result
                    self._last_run[name] = now
                    logger.info(
                        "ConsciousnessTick 任务 '%s' 完成: %s",
                        name, result.get("summary", ""),
                    )
                except Exception as e:
                    results[name] = {"error": str(e)}
                    # C9 修复：失败也更新 _last_run，避免失败任务持续被优先选中导致饿死其他任务
                    self._last_run[name] = now
                    logger.warning(
                        "ConsciousnessTick 任务 '%s' 失败: %s", name, e, exc_info=True
                    )

            self.last_tick_result = {
                "tick_at": now.isoformat(),
                "executed": list(results.keys()),
                "results": results,
            }

    # ── 认知任务实现 ──

    def _get_memory_manager(self) -> "MemoryManager":
        """
        使用 get_memory_manager 工厂获取缓存实例，
        确保 tick 生成的 embedding 与 Agent 运行时使用同一来源（避免维度不一致）。
        """
        from agent.memory import get_memory_manager
        try:
            import database as db
            model_cfg = db.get_default_model(mode="ssh") or db.get_default_model()
            if model_cfg:
                return get_memory_manager(
                    base_url=model_cfg.get("base_url", ""),
                    api_key=model_cfg.get("api_key", ""),
                    embedding_model=model_cfg.get("model_id") or model_cfg.get("name", ""),
                )
        except Exception as e:
            logger.debug("获取默认模型配置失败，tick 将使用 fallback embedding: %s", e)
        return get_memory_manager()

    async def _consolidate_memories(self) -> dict:
        """
        记忆固化：从近期 episodic 记忆中提取关键事实写入 semantic 记忆
        纯规则提取：识别 episodic 内容中的"任务 → 工具 → 结果"模式
        """
        import database as db

        # C8 修复：使用带模型配置的 MemoryManager，保证 embedding 来源一致
        mgr = self._get_memory_manager()

        consolidated = 0
        for mode in ("ssh",):
            try:
                rows = db.get_agent_memories(mode=mode, mem_type="episodic", limit=20)
            except Exception:
                continue
            if not rows:
                continue

            # H12 修复：一次性查询 semantic 记忆，避免 N+1（原先每条 fact 都查一次 DB）
            try:
                existing_semantic = db.get_agent_memories(
                    mode=mode, mem_type="semantic", limit=200
                )
                existing_contents = [e.get("content", "") for e in existing_semantic]
            except Exception:
                existing_contents = []

            for row in rows:
                content = row.get("content", "")
                if not content:
                    continue
                # 提取"使用工具: X, Y"作为知识
                facts = _extract_facts(content)
                for fact in facts:
                    # H12 修复：在内存中匹配，不再每条 fact 查 DB
                    if any(fact in c for c in existing_contents):
                        continue
                    try:
                        await mgr.save_semantic(
                            mode=mode,
                            content=fact,
                            source_conv_id=row.get("source_conv_id", ""),
                            agent_run_id=row.get("agent_run_id", ""),
                            importance=0.5,
                        )
                        existing_contents.append(fact)  # 避免本批次重复
                        consolidated += 1
                    except Exception as e:
                        logger.debug("保存固化记忆失败: %s", e)

        return {
            "summary": f"固化 {consolidated} 条 semantic 记忆",
            "consolidated": consolidated,
        }

    async def _reflect_on_failures(self) -> dict:
        """
        失败反思：分析最近失败的 agent_runs，识别常见失败模式
        将"失败模式 → 建议"写入 semantic 记忆，供后续运行参考
        """
        import database as db

        lookback = self.config.failure_lookback
        try:
            runs = db.get_agent_runs(conversation_id=None, limit=lookback)
        except Exception as e:
            return {"summary": f"查询 runs 失败: {e}", "reflected": 0}

        failed = [r for r in runs if r.get("status") in ("failed", "max_reached")]
        if not failed:
            return {"summary": "无失败 run 可反思", "reflected": 0}

        # 简单模式识别：统计失败状态分布
        from collections import Counter
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

        # 写入 semantic 记忆（每条 mode 一个）
        reflected = 0
        # C8 修复：使用带模型配置的 MemoryManager
        mgr = self._get_memory_manager()
        for pattern in patterns:
            for mode in ("ssh",):
                try:
                    await mgr.save_semantic(
                        mode=mode,
                        content=f"[反思] {pattern}",
                        importance=0.7,
                    )
                    reflected += 1
                except Exception:
                    pass

        return {
            "summary": f"反思 {len(failed)} 次失败，提取 {len(patterns)} 个模式",
            "reflected": reflected,
            "failed_count": len(failed),
        }

    async def _prune_stale_memories(self) -> dict:
        """
        清理过期记忆：importance < min_importance 且 last_used_at 超过 stale_days
        """
        import database as db

        # 读取用户配置的最大条目数，按 mode/type 各保留 max_items 条
        mem_settings = db.get_memory_settings()
        max_items = mem_settings.get("max_items", 100)

        total_deleted = 0
        for mode in ("ssh",):
            for mem_type in ("episodic", "semantic"):
                try:
                    deleted = db.prune_agent_memories(
                        mode=mode, mem_type=mem_type, max_count=max_items
                    )
                    total_deleted += deleted
                except Exception as e:
                    logger.debug("清理 %s/%s 记忆失败: %s", mode, mem_type, e)

        return {
            "summary": f"清理 {total_deleted} 条过期记忆",
            "deleted": total_deleted,
        }

    async def _touch_hot_memories(self) -> dict:
        """
        热点记忆提升：use_count >= 3 的记忆 importance +0.05（上限 1.0）
        让频繁使用的记忆在检索时获得更高优先级
        """
        import database as db

        promoted = 0
        for mode in ("ssh",):
            for mem_type in ("episodic", "semantic"):
                try:
                    promoted += db.promote_hot_memories(
                        mode=mode, mem_type=mem_type, min_use_count=3,
                        importance_delta=0.05, max_importance=1.0,
                    )
                except Exception as e:
                    logger.debug("提升 %s/%s 热点记忆失败: %s", mode, mem_type, e)

        return {
            "summary": f"提升 {promoted} 条热点记忆重要性",
            "promoted": promoted,
        }

    # ── 手动触发（供 API 调用） ──

    async def trigger_now(self) -> dict:
        """立即执行一次 tick（不影响后台调度），返回执行结果"""
        await self._tick_once()
        return self.last_tick_result or {"summary": "tick 已执行但无结果"}


# ── 辅助函数 ──


def _extract_facts(episodic_content: str) -> list[str]:
    """
    从 episodic 记忆内容中提取事实
    简单规则：识别"使用工具: X, Y"和"任务: Z"模式
    """
    facts: list[str] = []
    # 提取使用的工具
    if "使用工具:" in episodic_content:
        # 形如 "使用工具: read_file, write_file"
        parts = episodic_content.split("使用工具:")
        if len(parts) > 1:
            tools_part = parts[1].split("|")[0].split(",")[0].strip()
            tools_part = tools_part.strip()
            if tools_part:
                facts.append(f"常用工具: {tools_part}")
    # 提取任务类型
    if "任务:" in episodic_content:
        parts = episodic_content.split("任务:")
        if len(parts) > 1:
            task_part = parts[1].split("|")[0].strip()[:80]
            if task_part:
                facts.append(f"曾处理任务: {task_part}")
    return facts


# ── 全局单例（供 main.py 启动） ──

_global_tick: ConsciousnessTick | None = None


def get_global_tick() -> ConsciousnessTick:
    """获取全局 ConsciousnessTick 实例"""
    global _global_tick
    if _global_tick is None:
        _global_tick = ConsciousnessTick()
    return _global_tick
