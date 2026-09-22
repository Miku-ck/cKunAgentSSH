"""记忆块（画像层）：常驻上下文的自编辑小块（spec §4.3 / §7 / §8）。

- 键空间固定：user-preferences / agent-rules / host-profile:{hostId}
- agent 只走 replace（old_string 精确替换，防整块覆盖丢内容）
- 渲染顺序稳定，作为 bridle 的 [画像] 段无条件注入
"""
import asyncio

from . import mstore

USER_PREFERENCES = "user-preferences"
AGENT_RULES = "agent-rules"
HOST_PREFIX = "host-profile:"
_DEFAULT_LIMIT = 2000


def allowed_key(key: str) -> bool:
    return key in (USER_PREFERENCES, AGENT_RULES) or (
        key.startswith(HOST_PREFIX) and len(key) > len(HOST_PREFIX)
    )


def host_key(host_id: str) -> str:
    return f"{HOST_PREFIX}{host_id}"


async def get(key: str):
    return await asyncio.to_thread(mstore.get_block, key)


async def put(key: str, value: str, updated_by: str) -> int:
    if not allowed_key(key):
        raise ValueError(f"非法块键: {key}")
    return await asyncio.to_thread(mstore.put_block, key, value, updated_by)


async def replace(key: str, old: str, new: str, updated_by: str) -> int:
    if not allowed_key(key):
        raise ValueError(f"非法块键: {key}")
    return await asyncio.to_thread(mstore.replace_in_block, key, old, new, updated_by)


async def render_for_context(host_id: str = "") -> str:
    """组装 [画像] 段文本；无块返回空串。仅注入当前主机的 host 块。"""
    blocks = await asyncio.to_thread(mstore.list_blocks)
    wanted = {USER_PREFERENCES, AGENT_RULES}
    if host_id:
        wanted.add(host_key(host_id))
    parts = [f"### {b['key']}\n{b['value']}" for b in blocks if b["key"] in wanted and b["value"].strip()]
    return "\n\n".join(parts)
