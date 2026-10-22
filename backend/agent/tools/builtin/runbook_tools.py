"""Runbook 知识库工具 — 把「怎么修好的」沉淀成可检索的操作指南（方案 Phase 1.1）。

复用 memories 表（kind='runbook'），不建独立数据模型与 CRUD 页面（Spec/Goal 教训：
上下文型实体不入库建页）。两个工具：
- runbook_save  ：成功解决非平凡问题后，把「症状→定位过程→修复命令→验证方式」
                  直接写入记忆库（kind='runbook'，importance=0.7，source='agent'）
- runbook_search：动手排障前检索历史 runbook 与巡检异常报告
                  （kind='runbook' / 'inspection_report'，后者由定时巡检沉淀）

与 memory_add 的路径差异：memory_add 走后台对账队列（去重/矛盾消解面向零散
事实），runbook 是 Agent 明确意图的完整文档写入，直接落库（同 block_update 的
例外逻辑）；同名同作用域的 runbook 重复保存时走 supersede 让旧行让位并继承
热度，保证同一问题只留一份最新指南。
"""
from __future__ import annotations

import asyncio
import logging
import threading

from agent.memory import mstore
from agent.memory import retriever as memory_retriever
from agent.tools import BaseTool, ToolMeta, ToolResult
# 复用 memory_tools 的作用域与 embedding 客户端 helper：检索口径（global + 当前主机）
# 与向量路降级行为必须与 memory_search 完全一致，各抄一份必然漂移。
from agent.tools.builtin.memory_tools import _embed_client as _get_embed_client
from agent.tools.builtin.memory_tools import _host_scopes

logger = logging.getLogger(__name__)

# 写入侧限制（schema 描述与常量同源，改上限只改这里）
_MAX_CONTENT_CHARS = 4000   # runbook 是完整指南，远宽于零散事实的 500 上限
_MAX_TITLE_CHARS = 80
_MAX_TAGS = 8
_MAX_TAG_CHARS = 30
# 检索输出每条正文截断长度：runbook 全文可能上千字，检索阶段只需要够判断
# 相关性的摘要；模型需要全文时走 file/memory 渠道按 mem:id 取
_SEARCH_SNIPPET_CHARS = 400
# 检索 kind 域：runbook 之外，巡检异常报告（Phase 3 沉淀）是同类「经验来源」，
# 一并覆盖；此时该 kind 尚无数据，过滤后为空属正常
_SEARCH_KINDS = ("runbook", "inspection_report")
_RUNBOOK_IMPORTANCE = 0.7  # 高于事实记忆默认（0.5/0.6）：runbook 是操作指南

# 同名顶替互斥锁：add→查旧→顶替是三步非事务序列，两个并发同名保存（同轮
# 并行工具调用 / 两个会话同时沉淀）会互相顶替成环 —— 双双 superseded、
# 零 active，沉淀凭空丢失。sqlite 层不感知这个业务约束，锁只能在调用方收口。
# 用 threading.Lock 而非 asyncio.Lock：工具是进程级单例，asyncio.Lock 跨
# 事件循环复用会炸（测试与生产各有各的 loop）；临界区全是本地 sqlite
# 毫秒级读写，同步执行对事件循环的阻塞无感。
_RUNBOOK_SAVE_LOCK = threading.Lock()


def _normalize_tags(raw) -> list:
    """tags 槽位归一：接受 list[str] 或逗号/空白分隔串，去空、去重、截断。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace("，", ",").replace(",", " ").split()
    elif isinstance(raw, (list, tuple)):
        parts = [str(t) for t in raw]
    else:
        parts = [str(raw)]
    out: list = []
    for p in parts:
        p = p.strip().lstrip("#")
        if p and p not in out:
            out.append(p[:_MAX_TAG_CHARS])
        if len(out) >= _MAX_TAGS:
            break
    return out


class RunbookSaveTool(BaseTool):
    name = "runbook_save"
    description = (
        "把一次成功排障沉淀为 runbook 操作指南：症状 → 定位过程（用了什么只读观测、"
        "关键发现）→ 修复命令（逐条，含风险说明）→ 验证方式。"
        "完成一个非平凡问题的修复后主动调用（一眼就能修的简单操作不要存）；"
        "同名 runbook 再次保存会自动顶替旧版本。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": f"指南短标题（≤{_MAX_TITLE_CHARS} 字，"
                                     "如「nginx 502 上游超时排查」）"},
            "content": {"type": "string",
                        "description": f"指南正文（≤{_MAX_CONTENT_CHARS} 字）："
                                       "按 症状/定位/修复/验证 四段组织"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "关键词标签（可选，如 [\"nginx\",\"502\",\"反向代理\"]），参与检索命中",
            },
            "scope": {"type": "string",
                      "description": "global（默认，跨主机通用经验）或 host:{ssh_session_id}"
                                      "（仅当前主机、随会话生命周期）。指南类内容建议 global，"
                                      "主机细节写进正文即可"},
        },
        "required": ["title", "content"],
    }
    meta = ToolMeta(risk_level="safe", timeout=10.0)

    async def execute(self, context, **kwargs) -> ToolResult:
        title = (kwargs.get("title") or "").strip()
        content = (kwargs.get("content") or "").strip()
        if not title or not content:
            return ToolResult(success=False, output="", error="title 与 content 均不能为空")
        title = title[:_MAX_TITLE_CHARS]
        if len(content) > _MAX_CONTENT_CHARS:
            return ToolResult(
                success=False, output="",
                error=f"content 需 ≤{_MAX_CONTENT_CHARS} 字，请压缩到关键步骤",
            )
        tags = _normalize_tags(kwargs.get("tags"))
        # 标签进正文首行：FTS 对 title+content 分词，标签随正文参与检索命中
        final_content = f"[标签] {' '.join(tags)}\n{content}" if tags else content

        sid = (getattr(context, "ssh_session_id", "") or "").strip()
        # 默认 global（与 memory_add 默认一致）：会话 ID（ssh-N）跨进程回收且复用，
        # host:{sid} 作用域对持久知识不可达、跨重启还会串机；runbook 是要长期
        # 积累的指南，主机细节写进正文即可
        scope = (kwargs.get("scope") or "").strip() or "global"
        if scope != "global" and not scope.startswith("host:"):
            return ToolResult(success=False, output="",
                              error="scope 需为 global 或 host:{ssh_session_id}")
        if scope.startswith("host:") and scope != f"host:{sid}":
            return ToolResult(success=False, output="",
                              error="只能写入全局或当前主机的作用域，不能代其他主机沉淀经验")

        # 临界区整体持锁同步执行：add→查旧→顶替三步必须原子，见锁定义处注释
        with _RUNBOOK_SAVE_LOCK:
            try:
                new_id = mstore.add_memory(
                    kind="runbook",
                    content=final_content,
                    scope=scope,
                    title=title,
                    source="agent",
                    importance=_RUNBOOK_IMPORTANCE,
                    agent_run_id=getattr(context, "agent_run_id", "") or None,
                )
                # 同名同作用域的旧版 runbook 让位（继承热度）：同一问题的指南只留
                # 最新一份。list_memories 的 q 是 LIKE 粗筛，落库后必须用精确相等
                # 收口；新行自身也在命中里，先排除再顶替。limit 取 50：runbook
                # 积累多了之后，10 条 LIKE 命中可能被其它含同子串标题的行挤占，
                # 漏掉真正同名的旧版造成重复沉淀。
                olds = [
                    r for r in mstore.list_memories(
                        kind="runbook", status="active", q=title, limit=50)
                    if r["id"] != new_id and r["title"] == title and r["scope"] == scope
                ]
                for old in olds:
                    mstore.supersede(old["id"], new_id)
            except Exception as e:
                logger.warning("runbook_save 失败: %s", e, exc_info=True)
                return ToolResult(success=False, output="", error=f"runbook 保存失败: {e}")
        note = f"（已顶替旧版 {len(olds)} 份）" if olds else ""
        return ToolResult(success=True, output=f"runbook 已沉淀{note}：{title}", priority="low")


class RunbookSearchTool(BaseTool):
    name = "runbook_search"
    description = (
        "检索沉淀的运维经验：历史 runbook 操作指南 + 历史巡检异常报告。"
        "动手排障或变更前先检索是否有同类问题的历史处理经验，命中时优先参考经验行事。"
        "返回空表示没有沉淀过相关经验，按通用最佳实践处理。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "检索语句（自然语言，含关键实体名，如「nginx 502 反向代理」）"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10,
                      "description": "返回条数上限（默认 5，最大 10）"},
        },
        "required": ["query"],
    }
    meta = ToolMeta(risk_level="safe", idempotent=True, timeout=15.0)

    async def execute(self, context, **kwargs) -> ToolResult:
        query = (kwargs.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, output="", error="query 不能为空")
        try:
            top_k = max(1, min(int(kwargs.get("top_k") or 5), 10))
        except (TypeError, ValueError):
            top_k = 5

        try:
            embed_client = await _get_embed_client()
            hits = await memory_retriever.search(
                query, scopes=_host_scopes(context), kinds=list(_SEARCH_KINDS),
                top_k=top_k, embed_client=embed_client,
            )
        except Exception as e:  # 检索是增强信息，失败不中断推理（与 memory_search 同口径）
            logger.warning("runbook_search 检索失败: %s", e, exc_info=True)
            return ToolResult(success=True, output="（runbook 检索暂不可用，按通用实践处理）")

        if not hits:
            return ToolResult(success=True, output="（无相关 runbook / 巡检异常记录）",
                              priority="low")
        lines = []
        for h in hits:
            title = h.title or h.content[:24]
            snippet = h.content[:_SEARCH_SNIPPET_CHARS]
            if len(h.content) > _SEARCH_SNIPPET_CHARS:
                snippet += "…"
            lines.append(f"[mem:{h.memory_id}] ({h.kind}/{h.scope}) {title}: {snippet}")
        return ToolResult(success=True, output="\n".join(lines))
