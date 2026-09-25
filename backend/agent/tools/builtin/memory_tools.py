"""记忆自编辑工具（spec §7）— Agent 主动读/写/作废记忆与画像块。

四个工具与预检索注入是**双通道**：注入负责"被动想起来"，工具负责
"主动查历史 / 显式记住 / 纠错"。全部 risk_level=safe（只动自己的记忆库，
不碰目标机），因此无需人工审批。

写路径一律走 job 队列（memory_add → agent_add job），让 Agent 的新增也要过
Reconciler 对账（去重/矛盾消解），而不是直接落库绕过一致性约束；
唯一的例外是 block_update —— 画像块是常驻的小块，语义就是"当场改这几行"，
直写并留版本历史。
"""
from __future__ import annotations

import asyncio
import logging

from agent.memory import blocks as memory_blocks
from agent.memory import mstore
from agent.memory import reconcile as memory_reconcile
from agent.memory import retriever as memory_retriever
from agent.tools import BaseTool, ToolMeta, ToolResult

logger = logging.getLogger(__name__)

# 单次 memory_search 返回上限：记忆行是常驻上下文里最贵的一段文本
_MAX_TOP_K = 10
_KNOWN_KINDS = ("semantic", "episodic", "procedural")
# 写入侧真实限制常量。审查R2修复：schema 描述曾写死"≤200 字/标题≤40 字"，
# 与实现（≤500 拒绝、标题截 80）脱节——描述与常量必须同源，改上限只改这里。
_MAX_CONTENT_CHARS = 500
_MAX_TITLE_CHARS = 80


def _host_scopes(context) -> list[str]:
    """检索作用域：全局 + 当前主机（多主机场景下别把 A 机经验当 B 机事实）"""
    return memory_retriever.context_scopes(getattr(context, "ssh_session_id", ""))


async def _embed_client():
    """从默认模型配置取 embedding 客户端；取不到就返回 None（向量路自动降级）"""
    import database as db
    cfg = await asyncio.to_thread(db.get_default_model, "ssh", False) or \
        await asyncio.to_thread(db.get_default_model, None, False) or {}
    base_url = cfg.get("base_url", "")
    if not base_url:
        return None
    from agent.memory.embedding import get_embedding_client
    return get_embedding_client(
        base_url,
        cfg.get("api_key", ""),
        cfg.get("model_id") or cfg.get("name", ""),
    )


class MemorySearchTool(BaseTool):
    name = "memory_search"
    description = (
        "检索历史记忆（语义事实 / 运行经历 / 沉淀的操作规则）。"
        "当任务可能涉及用户偏好、主机特性、以往踩过的坑或其他主机上的先例时先调用本工具，"
        "再决定行动方案。返回空结果表示没有相关记忆，按通用最佳实践处理即可。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索语句（自然语言，含关键实体名）"},
            "kinds": {
                "type": "array",
                "items": {"type": "string", "enum": list(_KNOWN_KINDS)},
                "description": "限定记忆类型，缺省查全部",
            },
            "top_k": {"type": "integer", "minimum": 1, "maximum": _MAX_TOP_K,
                      "description": f"返回条数上限（默认 5，最大 {_MAX_TOP_K}）"},
        },
        "required": ["query"],
    }
    meta = ToolMeta(risk_level="safe", idempotent=True, timeout=15.0)

    async def execute(self, context, **kwargs) -> ToolResult:
        query = (kwargs.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, output="", error="query 不能为空")
        raw_kinds = kwargs.get("kinds") or []
        if isinstance(raw_kinds, str):
            raw_kinds = [raw_kinds]
        kinds = [k for k in raw_kinds if k in _KNOWN_KINDS] or None
        try:
            top_k = max(1, min(int(kwargs.get("top_k") or 5), _MAX_TOP_K))
        except (TypeError, ValueError):
            top_k = 5

        try:
            embed_client = await _embed_client()
            hits = await memory_retriever.search(
                query, scopes=_host_scopes(context), kinds=kinds,
                top_k=top_k, embed_client=embed_client,
            )
        except Exception as e:  # 检索是增强信息，失败不该让工具调用报错中断推理
            logger.warning("memory_search 检索失败: %s", e, exc_info=True)
            return ToolResult(success=True, output="（记忆检索暂不可用，按通用实践处理）")

        if not hits:
            return ToolResult(success=True, output="（无相关记忆）", priority="low")
        rows = await asyncio.to_thread(mstore.get_many, [h.memory_id for h in hits])
        by_id = {r["id"]: r for r in rows}
        lines = [mstore.render_line(by_id[h.memory_id]) for h in hits if h.memory_id in by_id]
        return ToolResult(success=True, output="\n".join(lines) or "（无相关记忆）")


class MemoryAddTool(BaseTool):
    name = "memory_add"
    description = (
        "登记一条值得长期保留的记忆（用户偏好、主机特性、可复用的操作规则）。"
        "提交后进入后台对账队列（自动与已有记忆去重、消解矛盾），本轮不立即生效。"
        "若只是本次任务的一次性信息，不要调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description":
                        f"记忆正文（一句话、可独立理解，≤{_MAX_CONTENT_CHARS} 字）"},
            "title": {"type": "string", "description":
                      f"短标题（可选，超 {_MAX_TITLE_CHARS} 字自动截断）"},
            "kind": {"type": "string", "enum": ["semantic", "procedural"],
                     "description": "semantic=事实/偏好；procedural=操作规则/经验教训。"
                                     "episodic 由系统自动记录，不接受工具写入"},
            "scope": {"type": "string", "description":
                      "global（跨主机通用）或 host:{ssh_session_id}（仅当前主机）。默认 global"},
            "importance": {"type": "number", "minimum": 0, "maximum": 1,
                           "description": "重要度 0~1，默认 0.6"},
        },
        "required": ["content", "kind"],
    }
    meta = ToolMeta(risk_level="safe", timeout=10.0)

    async def execute(self, context, **kwargs) -> ToolResult:
        content = (kwargs.get("content") or "").strip()
        if not content:
            return ToolResult(success=False, output="", error="content 不能为空")
        if len(content) > _MAX_CONTENT_CHARS:
            return ToolResult(success=False, output="",
                              error=f"content 需 ≤{_MAX_CONTENT_CHARS} 字，请压缩成一条可独立理解的事实")
        kind = kwargs.get("kind") or "semantic"
        if kind not in ("semantic", "procedural"):
            return ToolResult(success=False, output="",
                              error=f"kind 仅支持 semantic/procedural，收到 {kind}")
        scope = (kwargs.get("scope") or "global").strip()
        if scope != "global" and not scope.startswith("host:"):
            return ToolResult(success=False, output="",
                              error="scope 需为 global 或 host:{ssh_session_id}")
        sid = (getattr(context, "ssh_session_id", "") or "").strip()
        if scope.startswith("host:") and scope != f"host:{sid}":
            return ToolResult(success=False, output="",
                              error="只能写入全局或当前主机的作用域，不能代其他主机记事实")
        try:
            importance = float(kwargs.get("importance") if kwargs.get("importance") is not None else 0.6)
        except (TypeError, ValueError):
            importance = 0.6
        importance = max(0.0, min(1.0, importance))

        item = {
            "content": content,
            "title": (kwargs.get("title") or "")[:_MAX_TITLE_CHARS],
            "kind": kind,
            "scope": scope,
            "importance": importance,
        }
        job_id = await asyncio.to_thread(
            mstore.enqueue_job, memory_reconcile.JOB_AGENT_ADD,
            {**item, "conv_id": getattr(context, "conversation_id", "") or "",
             "run_id": getattr(context, "agent_run_id", "") or ""},
            1,
        )
        return ToolResult(
            success=True,
            output=f"已提交记忆对账队列（{job_id}），后台处理后可被检索",
            priority="low",
        )


class MemoryInvalidateTool(BaseTool):
    name = "memory_invalidate"
    description = (
        "作废一条已证实错误或过时的记忆（保留审计痕迹，不物理删除）。"
        "memory_id 取自上下文中的 [mem:xxx] 标记或 memory_search 结果。"
        "若只是需要更新内容，改用 block_update 或直接 memory_add 让对账消解。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "记忆 id（[mem:xxx] 中的 xxx）"},
            "reason": {"type": "string", "description": "作废原因（一句话）"},
        },
        "required": ["memory_id", "reason"],
    }
    meta = ToolMeta(risk_level="safe", timeout=10.0)

    async def execute(self, context, **kwargs) -> ToolResult:
        memory_id = (kwargs.get("memory_id") or "").strip()
        reason = (kwargs.get("reason") or "").strip()
        if not memory_id or not reason:
            return ToolResult(success=False, output="", error="memory_id 与 reason 均不能为空")
        row = await asyncio.to_thread(mstore.get_memory, memory_id)
        if not row:
            return ToolResult(success=False, output="", error=f"记忆不存在: {memory_id}")
        if row.get("status") not in ("active", "pending_reconcile"):
            return ToolResult(success=True,
                              output=f"记忆 {memory_id} 已处于 {row.get('status')} 状态",
                              priority="low")
        ok = await asyncio.to_thread(
            mstore.retire, memory_id, reason=f"agent: {reason[:200]}",
        )
        if not ok:
            return ToolResult(success=False, output="", error="作废失败（记忆状态已变更），请重试")
        title = row.get("title") or (row.get("content") or "")[:40]
        return ToolResult(success=True, output=f"已作废记忆 [{title}]", priority="low")


class BlockUpdateTool(BaseTool):
    name = "block_update"
    description = (
        "更新常驻画像块（用户偏好 user-preferences / 操作规则 agent-rules / "
        "主机画像 host-profile:{ssh_session_id}）。用 old_string 精确定位块内已有的那一行/那段，"
        "替换为 new_string；old_string 传空表示在该块末尾追加。"
        "用户给出长期偏好、或确认了主机的持久特性时用本工具，一次性信息不要写块。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description":
                    "user-preferences | agent-rules | host-profile:{ssh_session_id}"},
            "old_string": {"type": "string", "description": "块内需要被替换的原文（须唯一）；空串=追加"},
            "new_string": {"type": "string", "description": "替换后的文本；空串=删除该行"},
        },
        "required": ["key", "new_string"],
    }
    meta = ToolMeta(risk_level="safe", timeout=10.0)

    async def execute(self, context, **kwargs) -> ToolResult:
        key = (kwargs.get("key") or "").strip()
        new = str(kwargs.get("new_string") or "")
        old = str(kwargs.get("old_string") or "")
        sid = (getattr(context, "ssh_session_id", "") or "").strip()
        if key.startswith("host:") and not key.startswith(memory_blocks.HOST_PREFIX):
            key = memory_blocks.HOST_PREFIX + key[len("host:"):]
        if not memory_blocks.allowed_key(key):
            return ToolResult(
                success=False, output="",
                error=f"非法块键: {key}（允许 user-preferences / agent-rules / "
                      f"host-profile:{sid or '<ssh_session_id>'}）")
        if key.startswith(memory_blocks.HOST_PREFIX) and sid and not key.endswith(sid):
            return ToolResult(success=False, output="",
                              error="只能修改当前主机的画像块")
        try:
            version = await memory_blocks.replace(key, old, new, updated_by="agent")
        except ValueError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:
            logger.warning("block_update 失败: %s", e, exc_info=True)
            return ToolResult(success=False, output="", error=f"块更新失败: {e}")
        return ToolResult(success=True, output=f"块 {key} 已更新至版本 {version}", priority="low")
