"""
MetaReflection — Agent 事后复盘 + 策略评价

每次 Agent 运行结束后，对运行轨迹进行反思，生成"评价 + 改进建议"，
存入 agent_strategies 表（strategy_type="reflection"）。
后续运行时可以加载相关反思作为系统提示的一部分。

反思维度:
  1. 效率: 迭代次数是否过多？工具调用是否有冗余？
  2. 鲁棒性: 是否有工具失败？错误处理是否合理？
  3. 安全: 是否触发了权限拦截或 Hook 拦截？
  4. 完成: 是否达到 MAX_REACHED？最终内容是否非空？
  5. 成本: 单次运行 USD 成本是否过高？（H12 新增，供 EvolutionEngine 多维演化）

事后洞察（H12 新增）:
  - 工具调用冲突检测：先做后撤销的浪费模式（如先创建后删除同一对象）
  - 状态转移异常检测：非法状态跳转（如 COMPLETED → ACTING、FAILED → COMPLETED）

反思不调 LLM，纯规则评估，零成本。
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── 成本评分阈值（USD） ──
# 单次运行成本超过 _COST_BAD_USD 视为成本失控
_COST_EXCELLENT_USD = 0.05
_COST_GOOD_USD = 0.2
_COST_MODERATE_USD = 0.5
_COST_POOR_USD = 1.0


@dataclass
class RunTrace:
    """Agent 运行轨迹（供反思用）"""
    run_id: str
    task: str
    mode: str
    final_state: str              # completed / failed / max_reached
    iterations: int
    tool_calls: list[dict] = field(default_factory=list)  # [{tool, args, success, result}]
    warnings: list[str] = field(default_factory=list)
    # H12 新增：成本/Token/状态历史（供多维反思 + EvolutionEngine 演化）
    cost_usd: float = 0.0
    total_tokens: int = 0
    state_history: list[dict] = field(default_factory=list)


@dataclass
class ReflectionResult:
    """反思结果"""
    run_id: str
    score: float                  # 综合评分 0-1
    efficiency: float             # 效率评分 0-1
    robustness: float             # 鲁棒性评分 0-1
    safety: float                 # 安全评分 0-1
    completion: float             # 完成度评分 0-1
    cost: float = 1.0             # 成本评分 0-1（H12 新增，默认满分表示无成本数据）
    insights: list[str] = field(default_factory=list)   # 洞察列表
    suggestions: list[str] = field(default_factory=list)  # 改进建议列表
    summary: str = ""             # 一句话总结


class MetaReflection:
    """元反思引擎"""

    def reflect(self, trace: RunTrace) -> ReflectionResult:
        """
        对一次 Agent 运行进行反思

        Args:
            trace: Agent 运行轨迹
        Returns:
            ReflectionResult
        """
        efficiency = self._score_efficiency(trace)
        robustness = self._score_robustness(trace)

        # 从 tool_calls 推断 warnings（如果 result 含拦截/权限字样）
        # routes/agent.py 构建 RunTrace 时未填充 warnings，这里基于工具结果补全
        if not trace.warnings:
            inferred_warnings = []
            for tc in trace.tool_calls:
                result = tc.get("result", "") if isinstance(tc, dict) else ""
                if isinstance(result, str):
                    if "拦截" in result or "权限" in result or "禁止" in result:
                        inferred_warnings.append(
                            f"工具 {tc.get('tool', '?')} 被拦截"
                        )
            trace.warnings = inferred_warnings

        safety = self._score_safety(trace)
        completion = self._score_completion(trace)
        cost = self._score_cost(trace)

        # 综合分（加权平均）— H12 新增 cost 维度，重新分配权重
        # 原: eff 0.25 + rob 0.25 + safety 0.20 + comp 0.30 = 1.0
        # 新: eff 0.22 + rob 0.22 + safety 0.18 + comp 0.28 + cost 0.10 = 1.0
        # cost 权重较低（10%）：避免成本波动过度影响综合分，但仍能反映资源浪费
        score = (
            0.22 * efficiency
            + 0.22 * robustness
            + 0.18 * safety
            + 0.28 * completion
            + 0.10 * cost
        )

        insights = self._gen_insights(trace, efficiency, robustness, safety, completion, cost)
        suggestions = self._gen_suggestions(trace, efficiency, robustness, safety, completion, cost)
        summary = self._gen_summary(trace, score, insights)

        return ReflectionResult(
            run_id=trace.run_id,
            score=round(score, 3),
            efficiency=round(efficiency, 3),
            robustness=round(robustness, 3),
            safety=round(safety, 3),
            completion=round(completion, 3),
            cost=round(cost, 3),
            insights=insights,
            suggestions=suggestions,
            summary=summary,
        )

    # ── 各维度评分 ──

    def _score_efficiency(self, trace: RunTrace) -> float:
        """效率评分：迭代次数越少越好"""
        if trace.iterations <= 0:
            return 0.5
        # 1 轮 = 1.0, 5 轮 = 0.6, 10 轮 = 0.2
        if trace.iterations <= 1:
            return 1.0
        if trace.iterations <= 3:
            return 0.8
        if trace.iterations <= 5:
            return 0.6
        if trace.iterations <= 8:
            return 0.4
        return 0.2

    def _score_robustness(self, trace: RunTrace) -> float:
        """鲁棒性评分：工具失败率越低越好"""
        if not trace.tool_calls:
            return 1.0  # 无工具调用，视为无风险
        failed = sum(1 for tc in trace.tool_calls if not tc.get("success", True))
        fail_rate = failed / len(trace.tool_calls)
        return max(0.0, 1.0 - fail_rate * 1.5)  # 失败率 50% → 0.25 分

    _BLOCK_KEYWORDS = ("拦截", "权限", "禁止")

    def _is_blocked_call(self, tc: dict) -> bool:
        result = tc.get("result", "") if isinstance(tc, dict) else ""
        return isinstance(result, str) and any(
            k in result for k in self._BLOCK_KEYWORDS
        )

    def _score_safety(self, trace: RunTrace) -> float:
        """安全评分（三态）：无拦截 1.0；被拦后改走安全路径完成任务 0.9（期望行为，
        不惩罚正确的安全拦截）；反复撞同一堵墙后恢复 0.7；被拦后未恢复 0.3。

        原实现只要出现拦截就记 0.3，会把"危险命令被正确拦截、代理换安全路径"
        这一理想行为也打低分，反向惩罚安全合规的 run。"""
        blocked_idx = [
            i for i, tc in enumerate(trace.tool_calls)
            if self._is_blocked_call(tc)
        ]
        if not blocked_idx:
            # warnings 可能由外部注入（无 tool_calls 细节时）：仅在明显未善终时惩罚
            warned = any(
                "拦截" in w or "权限" in w for w in trace.warnings
            )
            if not warned:
                return 1.0
            return 0.9 if trace.final_state == "completed" else 0.3
        last_block = blocked_idx[-1]
        recovered = (
            any(tc.get("success", True) for tc in trace.tool_calls[last_block + 1:])
            or trace.final_state == "completed"
        )
        repeated = len(blocked_idx) >= 2
        if not recovered:
            return 0.3
        return 0.7 if repeated else 0.9

    def _score_completion(self, trace: RunTrace) -> float:
        """完成度评分"""
        if trace.final_state == "completed":
            return 1.0
        if trace.final_state == "max_reached":
            return 0.4  # 达到上限但至少运行到底
        if trace.final_state == "failed":
            return 0.1
        return 0.5  # 未知状态

    def _score_cost(self, trace: RunTrace) -> float:
        """成本评分（H12 新增）— 单次运行 USD 成本越低越好

        无成本数据时返回 1.0（视为无成本，不惩罚）
        """
        if trace.cost_usd <= 0:
            return 1.0
        if trace.cost_usd <= _COST_EXCELLENT_USD:
            return 1.0
        if trace.cost_usd <= _COST_GOOD_USD:
            return 0.8
        if trace.cost_usd <= _COST_MODERATE_USD:
            return 0.6
        if trace.cost_usd <= _COST_POOR_USD:
            return 0.4
        return 0.2  # > $1.0 视为成本失控

    # ── 洞察与建议生成 ──

    def _gen_insights(
        self,
        trace: RunTrace,
        efficiency: float,
        robustness: float,
        safety: float,
        completion: float,
        cost: float,
    ) -> list[str]:
        insights: list[str] = []

        if efficiency < 0.5:
            insights.append(
                f"运行效率偏低（{trace.iterations} 轮迭代），可能存在冗余工具调用"
            )

        failed_tools = [
            tc.get("tool", "?") for tc in trace.tool_calls if not tc.get("success", True)
        ]
        if failed_tools:
            unique_failed = list(set(failed_tools))
            insights.append(
                f"工具 {', '.join(unique_failed)} 执行失败，鲁棒性受影响"
            )

        if safety < 0.7:
            insights.append("触发了安全策略拦截且未见恢复，工具调用需更谨慎")
        elif safety < 1.0:
            insights.append("触发安全拦截后改走安全路径并完成，行为符合预期")

        if trace.final_state == "max_reached":
            insights.append("达到最大迭代上限，任务可能过于复杂")

        if not trace.tool_calls and trace.final_state == "completed":
            insights.append("无需工具调用直接完成任务，效率最优")

        # 工具使用分布
        if len(trace.tool_calls) >= 3:
            tool_names = [tc.get("tool", "?") for tc in trace.tool_calls]
            from collections import Counter
            common = Counter(tool_names).most_common(1)[0]
            if common[1] >= 3:
                insights.append(
                    f"工具 {common[0]} 被频繁调用（{common[1]} 次），可考虑批量化"
                )

        # ── H12 新增：成本洞察 ──
        if trace.cost_usd > 0:
            if cost < 0.5:
                insights.append(
                    f"单次运行成本 ${trace.cost_usd:.3f}（tokens={trace.total_tokens}），"
                    f"存在资源浪费"
                )
            elif trace.cost_usd > _COST_GOOD_USD:
                insights.append(
                    f"单次运行成本 ${trace.cost_usd:.3f}（tokens={trace.total_tokens}），"
                    f"可接受但偏高"
                )

        # ── H12 新增：工具调用冲突检测 ──
        conflict_insights = self._detect_tool_conflicts(trace.tool_calls)
        insights.extend(conflict_insights)

        # ── H12 新增：状态转移异常检测 ──
        anomaly_insights = self._detect_state_anomalies(trace.state_history, trace.final_state)
        insights.extend(anomaly_insights)

        return insights

    def _gen_suggestions(
        self,
        trace: RunTrace,
        efficiency: float,
        robustness: float,
        safety: float,
        completion: float,
        cost: float,
    ) -> list[str]:
        suggestions: list[str] = []

        if efficiency < 0.5:
            suggestions.append("考虑拆分任务为更小子任务，或增加 max_iterations")
        if robustness < 0.7:
            suggestions.append("工具调用前应先验证参数，必要时先执行只读命令（ls/cat 等）确认状态")
        if safety < 0.7:
            suggestions.append("避免直接调用高风险工具，先使用低风险工具收集信息")
        if trace.final_state == "max_reached":
            suggestions.append("任务过于复杂，建议用户分解为多步执行")

        # H12 新增：成本相关建议
        if cost < 0.5:
            suggestions.append(
                f"成本偏高（${trace.cost_usd:.3f}），建议减少冗余工具调用、"
                f"避免反复读取大文件、合并相邻的变更操作"
            )

        # 若全部评分都很高，给正向建议
        if all(s >= 0.8 for s in [efficiency, robustness, safety, completion, cost]):
            suggestions.append("本次运行表现优秀，可作为同类任务的范例")

        return suggestions

    # ── H12 新增：工具调用冲突检测 ──

    # 工具调用冲突组合（前者工具, 后者工具）— ssh 模式工具集（shell_exec 等）
    # 无本地文件工具，暂无可检测的冲突模式
    _CONFLICT_PATTERNS: list[tuple[str, str]] = []

    def _detect_tool_conflicts(self, tool_calls: list[dict]) -> list[str]:
        """检测事后工具调用冲突（浪费工作模式）

        仅检测明确的"先做后撤销"模式，避免误报
        """
        if len(tool_calls) < 2:
            return []

        insights: list[str] = []
        # 提取 (tool, path) 序列
        seq: list[tuple[str, str]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tool = tc.get("tool", "")
            args = tc.get("args", {}) or {}
            # 提取路径参数（统一字段名）
            path = (
                args.get("file_path") or args.get("path")
                or args.get("source") or args.get("destination")
                or args.get("target") or ""
            )
            if path:
                try:
                    import os
                    path = os.path.normpath(str(path))
                except Exception:
                    path = str(path)
                seq.append((tool, path))

        # 滑动窗口检测相邻冲突对
        seen_conflicts: set[tuple[str, str, str]] = set()
        for i in range(len(seq) - 1):
            tool_a, path_a = seq[i]
            tool_b, path_b = seq[i + 1]
            for pat_a, pat_b in self._CONFLICT_PATTERNS:
                if tool_a == pat_a and tool_b == pat_b and path_a == path_b:
                    key = (pat_a, pat_b, path_a)
                    if key in seen_conflicts:
                        continue
                    seen_conflicts.add(key)
                    insights.append(
                        f"工具调用冲突：{pat_a}({path_a}) 后紧接 {pat_b}，前者工作被浪费"
                    )
                    break

        return insights

    # ── H12 新增：状态转移异常检测 ──

    # 终态：进入后不应再转移出去
    _TERMINAL_STATES = {"completed", "failed", "max_reached"}

    def _detect_state_anomalies(
        self, state_history: list[dict], final_state: str
    ) -> list[str]:
        """检测状态转移序列中的异常

        异常类型：
        1. 终态后继续转移（如 completed → acting）
        2. FAILED 后又变 COMPLETED（可疑的成功）
        3. 缺少关键中间态（INIT 直接到 COMPLETED，跳过 thinking/acting）
        """
        if not state_history:
            return []

        insights: list[str] = []
        try:
            # state_history 条目格式：{"from": "x", "event": "...", "to": "y"}
            transitions = [
                (str(s.get("from", "")), str(s.get("to", "")))
                for s in state_history
                if isinstance(s, dict)
            ]
        except Exception:
            return []

        if not transitions:
            return []

        # 检测 1：终态后继续转移
        for src, dst in transitions:
            if src in self._TERMINAL_STATES:
                if dst not in self._TERMINAL_STATES:
                    insights.append(
                        f"状态转移异常：终态 {src} 后继续转移到 {dst}（应为终态）"
                    )

        # 检测 2：FAILED → COMPLETED（可疑的成功）
        # 直接查找 transitions 中是否存在 (failed, completed) 转移
        for src, dst in transitions:
            if src == "failed" and dst == "completed":
                insights.append("状态转移异常：FAILED 后又变为 COMPLETED，疑似状态机错误")
                break

        # 检测 3：缺少关键中间态（INIT 直接到 COMPLETED）
        # 仅当迭代数 > 0 但状态序列极短时报告（避免误判简单任务）
        all_states = [transitions[0][0]] + [t[1] for t in transitions]
        if (
            len(all_states) <= 2
            and "completed" in all_states
            and "thinking" not in all_states
            and "acting" not in all_states
            and "planning" not in all_states
        ):
            insights.append(
                "状态转移异常：完成态前缺少 thinking/acting/planning 中间态，"
                "可能跳过了推理或执行阶段"
            )

        return insights

    def _gen_summary(self, trace: RunTrace, score: float, insights: list[str]) -> str:
        """生成一句话总结"""
        level = "优秀" if score >= 0.8 else "良好" if score >= 0.6 else "一般" if score >= 0.4 else "较差"
        insight_part = insights[0] if insights else "无明显问题"
        return f"[{level} score={score:.2f}] {insight_part}"

    # ── 持久化 ──

    def save_reflection(
        self,
        result: ReflectionResult,
        mode: str,
        task: str,
        cost_usd: float = 0.0,
        iterations: int = 0,
        total_tokens: int = 0,
    ) -> Optional[str]:
        """
        将反思结果存入 agent_strategies 表（strategy_type="reflection"）
        返回策略 id；失败返回 None

        H12: 新增 cost_usd / iterations / total_tokens 字段持久化，
        供 EvolutionEngine 读取做多维演化（成本/迭代数/上限到达率）。
        """
        import database as db
        try:
            content = result.summary
            config = {
                "score": result.score,
                "efficiency": result.efficiency,
                "robustness": result.robustness,
                "safety": result.safety,
                "completion": result.completion,
                "cost": result.cost,
                "insights": result.insights,
                "suggestions": result.suggestions,
                "task": task[:200],
                "run_id": result.run_id,
                # H12 新增：供 EvolutionEngine 多维演化
                "cost_usd": round(cost_usd, 4),
                "iterations": iterations,
                "total_tokens": total_tokens,
                "reflected_at": datetime.now(timezone.utc).isoformat(),
            }
            return db.add_agent_strategy(
                mode=mode,
                strategy_type="reflection",
                name=f"reflection-{result.run_id}",
                content=content,
                config=config,
                score=result.score,
                enabled=1,
            )
        except Exception as e:
            logger.warning("保存反思结果失败: %s", e)
            return None

    # ── 加载历史反思（供 AgentEngine 注入系统提示） ──

    def load_recent_reflections(self, mode: str, limit: int = 3) -> list[dict]:
        """
        加载最近的高分反思，作为系统提示的一部分

        仅用于 prompt 注入：低分反思是失败结论，教给模型反而有害。
        演化引擎不要用它（见 load_reflections_for_evolution）。
        """
        import database as db
        try:
            rows = db.get_agent_strategies(
                mode=mode, strategy_type="reflection", limit=limit * 3
            )
            # 过滤 score >= 0.5 且 enabled=1
            valid = [r for r in rows if r.get("score", 0) >= 0.5 and r.get("enabled", 1)]
            # 按 score 降序取 top_k
            valid.sort(key=lambda r: r.get("score", 0), reverse=True)
            return valid[:limit]
        except Exception as e:
            logger.warning("加载反思策略失败: %s", e)
            return []

    def load_reflections_for_evolution(self, mode: str, limit: int = 10) -> list[dict]:
        """取最近 limit 条反思给演化引擎 —— 不做高分过滤、不按分数排序

        演化要的是「最近跑成什么样」，尤其是跑砸的样本。原先演化复用了
        load_recent_reflections（高分 + 按分数降序的注入视图）：
        - avg_score 恒 >= 0.5 → 规则 6「综合分低 → 打开 enable_thinking」永不触发
        - 取的是分数最高的前 N 条，失败运行被排除在样本外，
          于是「修失败」的演化机制对失败运行结构性失明
        """
        import database as db
        try:
            rows = db.get_agent_strategies(
                mode=mode, strategy_type="reflection", limit=limit
            )
            return [r for r in rows if r.get("enabled", 1)]
        except Exception as e:
            logger.warning("加载演化用反思失败: %s", e)
            return []
