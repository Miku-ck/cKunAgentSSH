"""
max_tokens 安全封顶工具 — 解耦 routes/chat 与 routes/agent 的跨路由依赖。

vLLM 的 max_tokens 是「输出」预算，但模型上下文窗口是「输入+输出」总和。
若用户配置 max_tokens = 上下文窗口（如 16384），加上 prompt 就会越界触发 vLLM 400。
这里按 cost_tracker.estimate_tokens 估算 prompt token，自动下调 max_tokens。

对于支持 enable_thinking 的模型（如 Qwen3），推理内容和最终输出共享输出预算，
因此需要确保 reasoning_tokens + max_output_tokens + prompt_tokens 不超过上下文窗口。
"""

from agent.cost_tracker import estimate_tokens


def safe_max_tokens(
    configured: int | None,
    prompt_text: str,
    max_output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> int | None:
    """
    根据输入大小智能封顶 max_tokens，避免触发 vLLM 400。

    估算说明（审查 M-16 修复）：prompt token 估算改用 cost_tracker.estimate_tokens
    （中文 ~1.5 字符/token、英文 ~4 字符/token 的混合启发式）。
    旧实现按 1.5 token/**字符**估算，比真实值高 2~6 倍，长中文对话下会把
    输出预算压到 256 token 导致回答被强制截断。
    额外留 1024 token 给 vLLM 内部 / system 指令 / tool schema。
    max_output_tokens 优先：用户在模型配置里设了 max_output_tokens（如 4096），
    以该值作为更精确的上限。

    reasoning_tokens：推理/思考内容的独立预算。当模型启用 enable_thinking 时，
    推理内容会消耗输出预算，因此需要从可用输出空间中扣除。

    额外保护：max_output_tokens 不能接近 configured（上下文窗口），
    否则加上 prompt 后必然越界。当 max_output_tokens >= configured * 0.8 时，
    视为用户误配，降级使用合理上限。
    """
    if not configured or configured <= 0:
        return None  # 不传该参数

    # 用户配置的 max_output_tokens 优先作为绝对上限
    # 但若 max_output_tokens 接近 configured（上下文窗口），视为误配：
    # 上下文 16384 + prompt 1428 → 输出设 16384 必然越界
    user_max = max_output_tokens if (max_output_tokens and max_output_tokens > 0) else None
    if user_max is not None and user_max >= configured * 0.8:
        # 误配降级：max_output_tokens 过大（如 = 上下文窗口），改用合理上限
        # 对于 1M 上下文，允许最大 131072 输出；对于小上下文，使用 8192
        user_max = min(131072, max(8192, configured // 4))

    # 推理 token 预算：从可用输出空间中预留
    # 当 enable_thinking=True 时，推理内容会消耗输出预算
    reasoning_budget = reasoning_tokens if (reasoning_tokens and reasoning_tokens > 0) else 0

    # 绝对上限：用户配置值、上下文窗口的 1/2、1/4（下限 8192）、131072（取最小）
    # 1M 上下文时允许 131072 输出，16K 上下文时允许 8192 输出
    # （审查修复：旧公式 min(cfg//2, max(8192, cfg//4)) 对 1M 给出 262144，与注释及
    #   上方 user_max 降级分支的 131072 上限不一致，此处对齐为 131072）
    context_cap = min(configured // 2, max(8192, configured // 4), 131072)
    absolute_max = min(user_max if user_max else context_cap, context_cap)

    # 估算 prompt 占用的 token（中英混合启发式，见 estimate_tokens 文档）
    estimated_prompt_tokens = max(1, estimate_tokens(prompt_text or ""))
    # 留出 1024 token 给 vLLM 内部 / system 指令 / tool schema 余量
    safe_max = max(256, configured - estimated_prompt_tokens - 1024 - reasoning_budget)
    return min(configured, absolute_max, safe_max)
