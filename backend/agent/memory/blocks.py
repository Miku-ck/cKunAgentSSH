"""记忆块（画像层）：常驻上下文的自编辑小块（spec §4.3 / §7 / §8）。

- 键空间固定：user-preferences / agent-rules / host-profile:{hostId}
- agent 只走 replace（old_string 精确替换，防整块覆盖丢内容）
- 渲染顺序稳定，作为 system prompt 的 [画像] 段常驻
"""
import asyncio
import logging

from ..cost_tracker import estimate_tokens
from . import mstore

logger = logging.getLogger(__name__)

USER_PREFERENCES = "user-preferences"
AGENT_RULES = "agent-rules"
HOST_PREFIX = "host-profile:"

# 注入顺序 = 价值顺序：用户偏好与操作规则是用户/策略意图，主机画像是可重新探测的
# 机器事实 —— 预算不够时先牺牲后者。
_RENDER_ORDER = (USER_PREFERENCES, AGENT_RULES)


def allowed_key(key: str) -> bool:
    return key in (USER_PREFERENCES, AGENT_RULES) or (
        key.startswith(HOST_PREFIX) and len(key) > len(HOST_PREFIX)
    )


def host_key(host_id: str) -> str:
    return f"{HOST_PREFIX}{host_id}"


async def put(key: str, value: str, updated_by: str) -> int:
    if not allowed_key(key):
        raise ValueError(f"非法块键: {key}")
    return await asyncio.to_thread(mstore.put_block, key, value, updated_by)


async def replace(key: str, old: str, new: str, updated_by: str) -> int:
    if not allowed_key(key):
        raise ValueError(f"非法块键: {key}")
    return await asyncio.to_thread(mstore.replace_in_block, key, old, new, updated_by)


# host-profile 块的保留条数：块键是 ssh_session_id，每次连接都是新键，而渲染只读
# 当前主机那一条 ⇒ 其余条目永远读不到。留 50 条与 host_profile._CACHE_MAX 同量级，
# 足够覆盖并发主机，又让这张表不会只增不减。用户写的 user-preferences / agent-rules
# 不在这个前缀下，一条都不会被动（它们是意图，不是可重探的事实）。
HOST_PROFILE_KEEP = 50


async def prune_host_profiles() -> int:
    """丢掉最老的 host-profile 块（含其历史）。返回删除条数。"""
    return await asyncio.to_thread(
        mstore.prune_blocks_by_prefix, HOST_PREFIX, HOST_PROFILE_KEEP
    )


async def render_for_context(host_id: str, budget_tokens: int) -> str:
    """组装 [画像] 段文本；无块返回空串。仅注入当前主机的 host 块。

    budget_tokens 由调用方从 db.get_memory_settings()["blocks_budget_tokens"] 传入
    （写死两处会变成"改画像上限悄悄挤掉检索注入"）。

    超预算时按整块丢弃而不是截字符：块内容是自编辑成的完整句子，
    切一半会改变语义（甚至反转「不要用 X」这类否定式规则）。

    丢弃必须留日志：写入侧的上限是**字符**（`memory_blocks.limit_chars`，默认 2000），
    这本账是**token** 且随模型窗口缩放，两条不同源 ⇒ 一条完全合法的块可以在小窗口模型上
    永远不出现，而用户看到的就是「我写了，它没记住」。实测 925 字符（604 token）的
    user-preferences 在 window=2048（账户 200 token）与 4096（460 token）下整块不出现，
    16384（2162 token）才回来 —— 修复前库里与日志里都没有任何痕迹。
    """
    blocks = await asyncio.to_thread(mstore.list_blocks)
    by_key = {b["key"]: (b["value"] or "").strip() for b in blocks}
    keys = list(_RENDER_ORDER)
    if host_id:
        keys.append(host_key(host_id))

    parts: list[str] = []
    dropped: list[tuple[str, int]] = []
    used = 0
    for key in keys:
        value = by_key.get(key) or ""
        if not value:
            continue
        block_text = f"### {key}\n{value}"
        cost = estimate_tokens(block_text)
        if used + cost > budget_tokens:
            dropped.append((key, cost))
            continue
        parts.append(block_text)
        used += cost
    if dropped:
        logger.warning(
            "画像块超预算被整块丢弃：%s（画像账户 %d token）—— 写入按字符、注入按 token，"
            "换更大的模型窗口或缩短该块才会重新出现",
            ", ".join(f"{k}≈{c}tok" for k, c in dropped), budget_tokens,
        )
    if not parts:
        return ""
    return "[画像] 以下为常驻已知事实，可直接引用\n" + "\n\n".join(parts)
