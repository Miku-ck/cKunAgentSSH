"""澄清策略的唯一权限模式写入点。

放在独立模块而不是 reins/core.py：避免修改所有拦截器共享的热路径，也避免
`reins -> clarification -> policy -> core` 的循环导入。MODE_KEY 在函数内延迟
导入，模块顶层不依赖 harness。
"""

from __future__ import annotations

from agent.clarification_policy import (
    POLICY_BLOCKED,
    POLICY_DOWNGRADE_NORMAL,
    POLICY_NONE,
    POLICY_READONLY_FALLBACK,
)


def apply_clarification_mode(ctx, policy) -> bool:
    """按策略收紧运行模式；返回是否真实改写。

    只写一次 ctx.metadata[MODE_KEY]；原模式更严格时不动，也不写 false。
    """
    action = getattr(policy, "action", policy)
    action = str(action or POLICY_NONE)

    if action in (POLICY_NONE, POLICY_BLOCKED):
        return False

    from agent.harness.reins.core import MODE_KEY

    current = str((getattr(ctx, "metadata", {}) or {}).get(MODE_KEY) or "normal").lower()
    target = current
    if action == POLICY_DOWNGRADE_NORMAL:
        if current == "auto":
            target = "normal"
    elif action == POLICY_READONLY_FALLBACK:
        if current in ("auto", "normal"):
            target = "plan"
    else:
        return False

    if target == current:
        return False

    meta = ctx.metadata
    meta["permission_mode_original"] = current
    meta["permission_mode_forced_by"] = "clarification_policy"
    meta[MODE_KEY] = target
    return True
