"""
EvolutionEngine — 策略演化 + 超参数调优

基于 MetaReflection 的反思结果，演化 Agent 的运行参数和策略:
  1. 超参数调优: max_iterations / temperature / max_tokens
  2. 工具偏好: 基于成功率调整工具调用顺序偏好
  3. 提示词策略: 根据反思建议调整系统提示

演化算法（轻量级，无 LLM 调用）:
  - 收集近 N 次同 mode 的反思结果
  - 计算各参数的平均得分
  - 根据得分趋势调整参数（性能好保持，性能差微调）
  - 将调整后的策略存入 agent_strategies 表（strategy_type="evolved_config"）

演化策略（多维指标综合）:
  - 完成度 < 0.5 → 增加 max_iterations
  - 效率 > 0.8 → 减少 max_iterations（节约资源）
  - 鲁棒性 < 0.6 → 降低 temperature（减少随机性）
  - 效率低 + 鲁棒性好 → 提高 temperature（鼓励探索）
  - 成本高（>0.5 USD 或 >10k tokens/次）→ 降低 max_iterations + 收紧 temperature
  - 上限到达率 > 50% → 增加 max_iterations（任务普遍偏复杂）
  - 综合分 < 0.5 → 启用 thinking 增强推理
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import json

logger = logging.getLogger(__name__)

# 默认 AgentConfig 参数 — 直接复用 contracts.AgentConfig 的默认值，避免硬编码副本漂移
# （原注释"与 engine.py 保持一致"引用的 engine.py 已删除）
from .harness.contracts import AgentConfig
_DEFAULT_CONFIG = {
    "max_iterations": AgentConfig.__dataclass_fields__["max_iterations"].default,
    "temperature": AgentConfig.__dataclass_fields__["temperature"].default,
    "max_tokens": AgentConfig.__dataclass_fields__["max_tokens"].default,
    "enable_thinking": AgentConfig.__dataclass_fields__["enable_thinking"].default,
    "enable_tools": AgentConfig.__dataclass_fields__["enable_tools"].default,
}

# ── 演化边界保护 ──
# 防止演化算法把参数调到极端值，导致 Agent 行为失常
_MAX_ITER_BOUNDS = (5, 300)        # max_iterations 边界（与设置面板 10-300 一致）
_TEMPERATURE_BOUNDS = (0.1, 1.5)   # temperature 边界（放宽，允许低温高确定性）
# 单次演化调整幅度（避免震荡）
_MAX_ITER_STEP = 5
_TEMP_STEP = 0.1
# 成本阈值（USD）：单次运行超过此值视为高成本
_HIGH_COST_USD = 0.5
# 上限到达率阈值：>50% 的运行达到 max_iterations 视为任务普遍偏复杂
_HIGH_MAX_REACHED_RATE = 0.5


@dataclass
class EvolvedConfig:
    """演化后的配置"""
    max_iterations: int = 50
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    enable_thinking: bool = False
    enable_tools: bool = True
    # 演化元信息
    evolved_from_runs: int = 0       # 基于多少次运行演化
    avg_score: float = 0.0           # 平均得分
    adjustments: list[str] = field(default_factory=list)  # 调整说明
    # 新增多维指标（供调试/审计，不直接驱动演化）
    avg_cost_usd: float = 0.0        # 平均成本（USD）
    avg_iterations: float = 0.0      # 平均迭代数
    max_reached_rate: float = 0.0    # 上限到达率（0-1）


class EvolutionEngine:
    """策略演化引擎"""

    def evolve(
        self,
        mode: str,
        recent_reflections: list[dict],
        current_config: dict | None = None,
    ) -> EvolvedConfig:
        """
        根据近期反思结果演化配置

        Args:
            mode: 当前模式
            recent_reflections: 近期反思策略列表（来自 agent_strategies 表）
            current_config: 当前生效的配置（默认使用 _DEFAULT_CONFIG）
        Returns:
            EvolvedConfig
        """
        base = dict(_DEFAULT_CONFIG)
        if current_config:
            base.update(current_config)

        cfg = EvolvedConfig(
            max_iterations=base.get("max_iterations", 50),
            temperature=base.get("temperature", 0.7),
            max_tokens=base.get("max_tokens"),
            enable_thinking=base.get("enable_thinking", False),
            enable_tools=base.get("enable_tools", True),
        )

        if not recent_reflections:
            return cfg

        # ── 解析反思结果，统计多维指标 ──
        scores: list[float] = []
        efficiencies: list[float] = []
        robustnesses: list[float] = []
        completions: list[float] = []
        # 新增维度
        cost_usds: list[float] = []           # 单次运行成本（USD）
        iterations_list: list[int] = []        # 单次运行迭代数
        max_reached_count = 0                  # 达到上限的次数

        for r in recent_reflections:
            try:
                cfg_data = r.get("config", "{}")
                if isinstance(cfg_data, str):
                    cfg_data = json.loads(cfg_data)
                scores.append(float(r.get("score", 0.5)))
                efficiencies.append(float(cfg_data.get("efficiency", 0.5)))
                robustnesses.append(float(cfg_data.get("robustness", 0.5)))
                completions.append(float(cfg_data.get("completion", 0.5)))
                # 新增维度：成本/迭代数/上限到达
                cost = cfg_data.get("cost_usd") or cfg_data.get("cost", 0.0)
                cost_usds.append(float(cost) if cost is not None else 0.0)
                iters = cfg_data.get("iterations")
                if iters is not None:
                    iterations_list.append(int(iters))
                # 上限到达判定：completion == 0.4 对应 max_reached（见 reflection.py _score_completion）
                if cfg_data.get("completion") is not None:
                    if abs(float(cfg_data.get("completion")) - 0.4) < 0.01:
                        max_reached_count += 1
            except Exception:
                continue

        if not scores:
            return cfg

        n = len(scores)
        cfg.evolved_from_runs = n
        avg_score = sum(scores) / n
        cfg.avg_score = round(avg_score, 3)
        avg_eff = sum(efficiencies) / n
        avg_rob = sum(robustnesses) / n
        avg_comp = sum(completions) / n
        # 新增指标平均
        cfg.avg_cost_usd = round(sum(cost_usds) / n, 4) if cost_usds else 0.0
        cfg.avg_iterations = round(sum(iterations_list) / len(iterations_list), 1) if iterations_list else 0.0
        cfg.max_reached_rate = round(max_reached_count / n, 2) if n > 0 else 0.0

        # ── 演化规则（按优先级：成本 > 上限到达 > 完成度 > 效率 > 鲁棒性 > 综合分）──

        # 1. 成本约束（最高优先级）：单次运行成本过高时，收紧资源
        if cfg.avg_cost_usd > _HIGH_COST_USD and cfg.max_iterations > _MAX_ITER_BOUNDS[0]:
            old = cfg.max_iterations
            cfg.max_iterations = max(_MAX_ITER_BOUNDS[0], cfg.max_iterations - _MAX_ITER_STEP)
            # 同时收紧 temperature（高成本通常意味着过度探索）
            old_t = cfg.temperature
            cfg.temperature = round(max(_TEMPERATURE_BOUNDS[0], cfg.temperature - _TEMP_STEP), 2)
            cfg.adjustments.append(
                f"max_iterations {old} → {cfg.max_iterations}（成本高 ${cfg.avg_cost_usd:.3f}）"
            )
            if cfg.temperature != old_t:
                cfg.adjustments.append(
                    f"temperature {old_t} → {cfg.temperature}（成本高，收紧探索）"
                )

        # 2. 上限到达率：>50% 运行达到 max_iterations 说明任务普遍偏复杂
        elif cfg.max_reached_rate > _HIGH_MAX_REACHED_RATE and cfg.max_iterations < _MAX_ITER_BOUNDS[1]:
            old = cfg.max_iterations
            cfg.max_iterations = min(_MAX_ITER_BOUNDS[1], cfg.max_iterations + _MAX_ITER_STEP)
            cfg.adjustments.append(
                f"max_iterations {old} → {cfg.max_iterations}（上限到达率 {cfg.max_reached_rate:.0%}）"
            )

        # 3. 完成度低 → 增加迭代上限（原规则，但收紧步长避免震荡）
        elif avg_comp < 0.5 and cfg.max_iterations < _MAX_ITER_BOUNDS[1]:
            old = cfg.max_iterations
            cfg.max_iterations = min(_MAX_ITER_BOUNDS[1], cfg.max_iterations + _MAX_ITER_STEP)
            cfg.adjustments.append(
                f"max_iterations {old} → {cfg.max_iterations}（完成度低 {avg_comp:.2f}）"
            )

        # 4. 效率高 → 减少迭代上限节约资源（原规则）
        elif avg_eff > 0.8 and cfg.max_iterations > _MAX_ITER_BOUNDS[0]:
            old = cfg.max_iterations
            cfg.max_iterations = max(_MAX_ITER_BOUNDS[0], cfg.max_iterations - 2)
            cfg.adjustments.append(
                f"max_iterations {old} → {cfg.max_iterations}（效率高 {avg_eff:.2f}）"
            )

        # 5. temperature 调整（原规则，但收紧步长避免震荡）
        # 仅当上一轮未因成本调整 temperature 时才执行
        temp_already_adjusted = any("temperature" in adj for adj in cfg.adjustments)
        if not temp_already_adjusted:
            if avg_rob < 0.6 and cfg.temperature > _TEMPERATURE_BOUNDS[0]:
                old = cfg.temperature
                cfg.temperature = round(max(_TEMPERATURE_BOUNDS[0], cfg.temperature - _TEMP_STEP * 1.5), 2)
                cfg.adjustments.append(
                    f"temperature {old} → {cfg.temperature}（鲁棒性差 {avg_rob:.2f}）"
                )
            elif avg_eff < 0.5 and avg_rob > 0.7 and cfg.temperature < _TEMPERATURE_BOUNDS[1]:
                old = cfg.temperature
                cfg.temperature = round(min(_TEMPERATURE_BOUNDS[1], cfg.temperature + _TEMP_STEP), 2)
                cfg.adjustments.append(
                    f"temperature {old} → {cfg.temperature}（鼓励探索）"
                )

        # 6. enable_thinking 启用条件（原规则保留）
        if avg_score < 0.5 and not cfg.enable_thinking:
            cfg.enable_thinking = True
            cfg.adjustments.append(
                f"enable_thinking False → True（综合分低 {avg_score:.2f}，增强推理）"
            )

        return cfg

    def save_evolved_config(self, cfg: EvolvedConfig, mode: str) -> Optional[str]:
        """将演化后的配置存入 agent_strategies 表"""
        import database as db
        try:
            content = (
                f"演化配置 (基于 {cfg.evolved_from_runs} 次运行, "
                f"avg_score={cfg.avg_score}): "
                f"max_iter={cfg.max_iterations}, temp={cfg.temperature}, "
                f"thinking={cfg.enable_thinking}"
            )
            config_data = {
                "max_iterations": cfg.max_iterations,
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
                "enable_thinking": cfg.enable_thinking,
                "enable_tools": cfg.enable_tools,
                "evolved_from_runs": cfg.evolved_from_runs,
                "avg_score": cfg.avg_score,
                "adjustments": cfg.adjustments,
                "evolved_at": datetime.now(timezone.utc).isoformat(),
            }
            return db.add_agent_strategy(
                mode=mode,
                strategy_type="evolved_config",
                name=f"evolved-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                content=content,
                config=config_data,
                score=cfg.avg_score,
                enabled=1,
            )
        except Exception as e:
            logger.warning("保存演化配置失败: %s", e)
            return None

    def load_latest_evolved_config(self, mode: str) -> Optional[dict]:
        """加载最新的演化配置（enabled=1 且 score 最高）"""
        import database as db
        try:
            rows = db.get_agent_strategies(
                mode=mode, strategy_type="evolved_config", limit=10
            )
            valid = [r for r in rows if r.get("enabled", 1)]
            if not valid:
                return None
            valid.sort(key=lambda r: r.get("score", 0), reverse=True)
            top = valid[0]
            cfg_data = top.get("config", "{}")
            if isinstance(cfg_data, str):
                cfg_data = json.loads(cfg_data)
            return cfg_data
        except Exception as e:
            logger.warning("加载演化配置失败: %s", e)
            return None
