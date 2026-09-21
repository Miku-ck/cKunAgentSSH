"""
成本追踪器 — Agent 运行过程的 token 与成本统计

职责:
  1. 累计每次 LLM 调用的 prompt_tokens / completion_tokens
  2. 根据模型定价计算 cost_usd
  3. 累计工具调用次数和耗时
  4. 提供 snapshot 供 SSE 实时上报

定价模型（OpenAI 兼容，单位 USD per 1M tokens）:
  - 默认 fallback: input=$0.5/1M, output=$1.5/1M
  - 内置常见模型定价（gpt-4o, gpt-4-turbo, claude-3.5-sonnet 等）
  - 通过 model_id 前缀/包含关键词匹配
"""
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── 模型定价表（USD per 1M tokens）──
# 格式: (关键词匹配, input_price, output_price)
# 按从上到下顺序匹配，第一个命中的生效
_MODEL_PRICING: list[tuple[str, float, float]] = [
    # OpenAI GPT-4o 系列
    ("gpt-4o-mini",       0.15,  0.60),
    ("gpt-4o",            2.50, 10.00),
    # GPT-4 Turbo
    ("gpt-4-turbo",      10.00, 30.00),
    ("gpt-4",            30.00, 60.00),
    # GPT-3.5
    ("gpt-3.5",           0.50,  1.50),
    # Claude 3.5
    ("claude-3-5-sonnet", 3.00, 15.00),
    ("claude-3-5-haiku",  0.80,  4.00),
    ("claude-3-opus",    15.00, 75.00),
    # Claude 3
    ("claude-3-sonnet",   3.00, 15.00),
    ("claude-3-haiku",    0.25,  1.25),
    # Qwen
    ("qwen-max",          2.50, 10.00),
    ("qwen-plus",         0.40,  1.20),
    ("qwen-turbo",        0.05,  0.20),
    # DeepSeek
    ("deepseek",          0.14,  0.28),
]

# 未知模型 fallback
_DEFAULT_PRICING: tuple[float, float] = (0.50, 1.50)


def get_model_pricing(model_id: str) -> tuple[float, float]:
    """
    根据模型 ID 获取定价 (input_price_per_1m, output_price_per_1m)

    Args:
        model_id: 模型标识符（如 "gpt-4o-mini", "Qwen/Qwen3-VL-8B-Instruct"）
    Returns:
        (input_price, output_price) USD per 1M tokens
    """
    if not model_id:
        return _DEFAULT_PRICING

    model_lower = model_id.lower()
    for keyword, in_price, out_price in _MODEL_PRICING:
        start = model_lower.find(keyword)
        while start != -1:
            end = start + len(keyword)
            # 命中位置后面不能紧跟字母/数字/点，否则那是另一个型号：
            # 裸 "gpt-4"（$30/$60）曾子串命中 "gpt-4.1"、"gpt-45"、"gpt-4o"，
            # 成本被放大十几倍 —— 默认 $2 的熔断会在真实花费几毛钱时掐断 run。
            # "-" 仍算边界（"gpt-4-1106-preview"、"deepseek-chat" 归属本行）。
            if end == len(model_lower) or not (model_lower[end].isalnum() or model_lower[end] == "."):
                return in_price, out_price
            start = model_lower.find(keyword, start + 1)
    # 默认 fallback
    return _DEFAULT_PRICING


def calc_cost(prompt_tokens: int, completion_tokens: int, model_id: str) -> float:
    """
    计算 USD 成本

    Args:
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        model_id: 模型 ID
    Returns:
        cost in USD
    """
    in_price, out_price = get_model_pricing(model_id)
    cost = (prompt_tokens / 1_000_000) * in_price + (completion_tokens / 1_000_000) * out_price
    return round(cost, 6)


@dataclass
class CostSnapshot:
    """成本快照（供 SSE 上报）"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    tool_call_count: int = 0
    llm_call_count: int = 0
    duration_ms: int = 0
    iterations: int = 0
    # H4 修复：补齐 tool_duration_ms 字段
    tool_duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "tool_call_count": self.tool_call_count,
            "llm_call_count": self.llm_call_count,
            "duration_ms": self.duration_ms,
            "iterations": self.iterations,
            "tool_duration_ms": self.tool_duration_ms,
        }


class CostTracker:
    """Agent 运行成本追踪器"""

    def __init__(self, model_id: str = ""):
        self.model_id = model_id
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tool_call_count = 0
        self.llm_call_count = 0
        self.start_time = time.monotonic()
        # 工具耗时累计（ms）
        self.tool_duration_ms = 0

    def record_llm_call(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记录一次 LLM 调用的 token 用量"""
        if prompt_tokens > 0:
            self.prompt_tokens += prompt_tokens
        if completion_tokens > 0:
            self.completion_tokens += completion_tokens
        self.llm_call_count += 1

    def get_total_cost(self) -> float:
        """计算当前累计成本（USD）"""
        return calc_cost(self.prompt_tokens, self.completion_tokens, self.model_id)

    def get_total_tokens(self) -> int:
        """总 token 数"""
        return self.prompt_tokens + self.completion_tokens

    def get_duration_ms(self) -> int:
        """总耗时（ms）"""
        return int((time.monotonic() - self.start_time) * 1000)

    def snapshot(self, iterations: int = 0) -> CostSnapshot:
        """生成当前快照"""
        return CostSnapshot(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.get_total_tokens(),
            cost_usd=self.get_total_cost(),
            tool_call_count=self.tool_call_count,
            llm_call_count=self.llm_call_count,
            duration_ms=self.get_duration_ms(),
            iterations=iterations,
            tool_duration_ms=self.tool_duration_ms,  # H4 修复：补齐字段
        )


# ── Token 估算（无 usage 字段时的兜底） ──

def estimate_tokens(text: str) -> int:
    """
    估算文本 token 数（粗略规则）
    - 英文: ~4 字符/token
    - 中文: ~1.5 字符/token
    - 混合取折中
    """
    if not text:
        return 0
    # 简单启发式：统计中文字符数和总字符数
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    # 中文 1.5 字符/token，其他 4 字符/token
    return int(chinese_chars / 1.5 + other_chars / 4)
