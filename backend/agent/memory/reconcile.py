"""Reconciler — memory_jobs 的唯一消费端（spec §5）。

四个写入源（对话提取 / agent 工具 / run 收尾 episodic / 迁移回填）全部只投 job，
是否入库、如何入库由这里的一次 LLM 批量裁决决定：
ADD / UPDATE / DELETE / NOOP，每步写 memory_audit 留痕。

失败语义：
- LLM 不可用/超时/JSON 解析失败 → job fail_job()（attempts+1，3 次进 failed），
  候选停留在 pending_reconcile（可检索、注入带 [待确认] 前缀），不硬写。
- embedding 不可用 → 照常入库但无向量（仅 FTS 可检），宁缺毋滥不造伪向量。
"""
from __future__ import annotations

import asyncio
import json
import logging

import database as db

from . import mstore
from .embedding import get_embedding_client
from . import retriever

logger = logging.getLogger(__name__)

# job 类型
JOB_EPISODIC = "episodic_summary"
JOB_CANDIDATE = "candidate_fact"
JOB_AGENT_ADD = "agent_add"
JOB_BACKFILL = "backfill_reconcile"

_CHUNK_TIMEOUT = 30.0
_TOTAL_TIMEOUT = 90.0
_SIMILAR_TOP_K = 8

_RECONCILE_PROMPT = """\
你是记忆管理器。左侧是若干"新记忆候选"（编号 0 起），每个候选附带检索到的\
"已有相关记忆"（带 id）。对每个候选判定一个操作：

- ADD: 与已有记忆都不重复、值得长期保留，直接新增。
- UPDATE: 候选与某条已有记忆讲的是同一事实，但提供了新信息 → 用候选内容\
（可与旧内容合并成一句更准确的话）替换该条。给出 target_id 与 merged_content。
- DELETE: 候选表明某条已有记忆已过时/被否定 → 作废该条。给出 target_id。
- NOOP: 候选与已有记忆完全等价、无增量 → 丢弃候选。给出 target_id（等价的那条）。

约束：
1. 矛盾（同一主题不同取值）必须 UPDATE 或 DELETE，不允许两条矛盾记忆同时存在。
2. 语义重复但措辞不同 → NOOP 或 UPDATE，不新增。
3. 琐碎闲聊、一次性任务细节 → 不保留（候选是待确认旧记忆时判 NOOP 即转正，\
若认为其无长期价值则判 DELETE 并给 target_id=候选自身处理）。

严格输出 JSON，无其他内容：
{"decisions":[{"index":0,"op":"ADD","target_id":null,"merged_content":null,"reason":"一句话理由"}]}
decisions 数量必须等于候选数量且 index 连续。
"""


def format_episodic(task: str, tool_calls: list, final_content: str, iterations: int) -> str:
    """从 Agent 运行轨迹构建 episodic 摘要（纯规则，迁移自 v1 MemoryManager）。"""
    parts = [f"任务: {task.strip()[:200]}"]
    if iterations:
        parts.append(f"迭代次数: {iterations}")
    if tool_calls:
        tool_names = [tc.get("tool", "?") for tc in tool_calls]
        parts.append(f"使用工具: {', '.join(tool_names[:8])}")
    if final_content:
        parts.append(f"结果: {final_content.strip().replace(chr(10), ' ')[:300]}")
    return " | ".join(parts)


def _default_model_cfg() -> dict:
    return (
        db.get_default_model(mode="ssh", mask_key=False)
        or db.get_default_model(mask_key=False)
        or {}
    )


async def _call_llm(prompt: str, candidates_block: str, model_cfg: dict) -> "list[dict] | None":
    from post_processor import _call_llm_with_timeout, _extract_json  # 延迟导入避免环

    model_id = model_cfg.get("id") or model_cfg.get("model_id") or ""
    messages = [
        {"role": "system", "content": "你是精确的记忆对账引擎，只输出 JSON。"},
        {"role": "user", "content": prompt + "\n\n" + candidates_block},
    ]
    text = await _call_llm_with_timeout(
        messages=messages, model_cfg=model_cfg, model_id=model_id,
        temperature=0.1, max_tokens=1024,
        chunk_timeout=_CHUNK_TIMEOUT, total_timeout=_TOTAL_TIMEOUT,
        task_name="reconcile",
    )
    if not text:
        return None
    try:
        raw = _extract_json(text)
        decisions = json.loads(raw).get("decisions")
        if isinstance(decisions, list):
            return decisions
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning("[reconcile] LLM 输出解析失败: %s, raw=%s", e, text[:200])
    return None


async def _similar_existing(cand: dict, embed_client) -> list:
    """候选的对照面：同 scope 语义域内 top-K 已有记忆（排除候选自身）。"""
    query = f"{cand.get('title', '')} {cand.get('content', '')}"
    hits = await retriever.search(
        query,
        scopes=[cand.get("scope") or "global"],
        kinds=["semantic", "procedural"],
        top_k=_SIMILAR_TOP_K,
        embed_client=embed_client,
        exclude_ids=([cand["id"]] if cand.get("id") else []),
        statuses=("active",),
    )
    return hits


def _cand_block_for_prompt(cands: list, similars: list) -> str:
    parts = []
    for i, (c, hits) in enumerate(zip(cands, similars)):
        lines = [f"候选{i} (id={c.get('id') or '新建'}): [{c['kind']}/{c['scope']}] "
                 f"{c.get('title', '')}: {c['content']}"]
        if hits:
            lines.append("已有相关记忆:")
            lines.extend(f"  - id={h.memory_id}: {h.title or h.content[:60]}: {h.content}"
                         for h in hits[:_SIMILAR_TOP_K])
        else:
            lines.append("已有相关记忆: 无")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def process_jobs(*, max_jobs: int = 20, embed_client=None, model_cfg=None) -> dict:
    """消费一批 job。由意识 tick 与 trigger_now 调用。"""
    jobs = await asyncio.to_thread(mstore.next_jobs, max_jobs)
    if not jobs:
        return {"processed": 0, "applied": {}, "failed": 0}
    if model_cfg is None:
        model_cfg = await asyncio.to_thread(_default_model_cfg)
    if embed_client is None and model_cfg:
        embed_client = get_embedding_client(
            model_cfg.get("base_url", ""),
            model_cfg.get("api_key", ""),
            model_cfg.get("model_id") or model_cfg.get("name", ""),
        )

    counters = {"ADD": 0, "UPDATE": 0, "DELETE": 0, "NOOP": 0, "episodic": 0}
    failed = 0
    for job in jobs:
        try:
            c = await _process_one(job, embed_client, model_cfg)
            for k, v in c.items():
                counters[k] = counters.get(k, 0) + v
            await asyncio.to_thread(mstore.finish_job, job["id"])
        except Exception as e:
            failed += 1
            logger.warning("[reconcile] job %s(%s) 失败: %s", job["id"], job["kind"], e)
            await asyncio.to_thread(mstore.fail_job, job["id"], str(e))
    return {
        "processed": len(jobs),
        "applied": counters,
        "failed": failed,
    }


async def _process_one(job: dict, embed_client, model_cfg: dict) -> dict:
    payload = json.loads(job.get("payload") or "{}")
    kind = job["kind"]
    if kind == JOB_EPISODIC:
        return await _write_episodic(payload, job["id"], embed_client)
    if kind == JOB_AGENT_ADD:
        payload.setdefault("source", "agent_tool")
        return await _reconcile_candidates([payload], job["id"], embed_client, model_cfg,
                                           priority_add=True)
    if kind == JOB_CANDIDATE:
        items = [
            {**it, "conv_id": payload.get("conv_id"), "run_id": payload.get("run_id"),
             "source": "chat_extract"}
            for it in payload.get("items", []) if (it.get("content") or "").strip()
        ]
        if not items:
            return {}
        return await _reconcile_candidates(items, job["id"], embed_client, model_cfg)
    if kind == JOB_BACKFILL:
        rows = await asyncio.to_thread(mstore.get_many, payload.get("ids", []))
        cands = [
            {"id": r["id"], "title": r.get("title") or "", "content": r["content"],
             "kind": r["kind"], "scope": r["scope"], "importance": r["importance"],
             "source": "migration"}
            for r in rows if r["status"] == "pending_reconcile"
        ]
        if not cands:
            return {}
        return await _reconcile_candidates(cands, job["id"], embed_client, model_cfg)
    raise ValueError(f"未知 job kind: {kind}")


async def _write_episodic(payload: dict, job_id: str, embed_client) -> dict:
    """episodic 是事实记录不参与对账（spec §5 表），直接入 active。"""
    content = (payload.get("content") or "").strip()
    if not content:
        return {}
    emb, emb_src = await _embed(embed_client, content)
    await asyncio.to_thread(
        mstore.add_memory,
        kind="episodic", content=content,
        title=payload.get("title", ""), scope=payload.get("scope") or "global",
        source=payload.get("source") or "run_summary",
        importance=float(payload.get("importance", 0.4)),
        embedding=emb, embedding_src=emb_src,
        source_conv_id=payload.get("conv_id"), agent_run_id=payload.get("run_id"),
    )
    return {"episodic": 1}


async def _embed(embed_client, content: str):
    if embed_client is None:
        return None, ""
    vec = await embed_client.embed_one(content)
    model = getattr(embed_client, "model", "") or ""
    return (vec, f"api:{model}") if vec else (None, "")


async def _reconcile_candidates(
    cands: list, job_id: str, embed_client, model_cfg: dict, *, priority_add: bool = False,
) -> dict:
    similars = await asyncio.gather(
        *(_similar_existing(c, embed_client) for c in cands),
        return_exceptions=True,
    )
    similars = [[] if isinstance(s, BaseException) else s for s in similars]

    decisions = None
    if model_cfg:
        decisions = await _call_llm(_RECONCILE_PROMPT, _cand_block_for_prompt(cands, similars),
                                    model_cfg)
    if decisions is None:
        # 无模型/LLM 失败：agent 工具是用户显式意图，直写；其余留给下个 tick 重试
        if priority_add:
            decisions = [{"index": i, "op": "ADD"} for i in range(len(cands))]
        else:
            raise RuntimeError("LLM 对账不可用，job 待重试")

    counters: dict = {}
    by_index = {}
    for d in decisions:
        if isinstance(d, dict) and isinstance(d.get("index"), int):
            by_index[d["index"]] = d
    for i, cand in enumerate(cands):
        d = by_index.get(i) or {"op": "NOOP" if not priority_add else "ADD"}
        op = str(d.get("op", "NOOP")).upper()
        counters[op] = counters.get(op, 0) + 1
        await _apply(cand, op, d, similars[i], job_id, embed_client)
    return counters


async def _apply(cand: dict, op: str, decision: dict, hits: list, job_id: str,
                 embed_client) -> None:
    """把一条裁决落到 memories + memory_audit。"""
    reason = str(decision.get("reason") or "")[:300]
    target = decision.get("target_id") or (hits[0].memory_id if hits else None)
    emb, emb_src = await _embed(embed_client, cand["content"])

    if op == "ADD":
        if cand.get("id"):  # 迁移行转正（沿用原 id 与热度）
            await asyncio.to_thread(mstore.adopt, cand["id"], content=cand["content"],
                                    title=cand.get("title", ""), embedding=emb,
                                    embedding_src=emb_src)
            mid = cand["id"]
        else:
            mid = await asyncio.to_thread(
                mstore.add_memory,
                kind=cand.get("kind") or "semantic",
                content=cand["content"], title=cand.get("title", ""),
                scope=cand.get("scope") or "global", source=cand.get("source") or "chat_extract",
                importance=float(cand.get("importance", 0.6)),
                embedding=emb, embedding_src=emb_src,
                source_conv_id=cand.get("conv_id"), agent_run_id=cand.get("run_id"),
            )
        await asyncio.to_thread(mstore.add_audit, mid, "ADD", new=cand["content"],
                                reason=reason, job_id=job_id)
    elif op == "UPDATE" and target and target != cand.get("id"):
        content = str(decision.get("merged_content") or cand["content"])
        if cand.get("id"):
            mid = cand["id"]
            await asyncio.to_thread(mstore.adopt, mid, content=content,
                                    title=cand.get("title", ""), embedding=emb,
                                    embedding_src=emb_src)
        else:
            mid = await asyncio.to_thread(
                mstore.add_memory,
                kind=cand.get("kind") or "semantic",
                content=content, title=cand.get("title", ""),
                scope=cand.get("scope") or "global", source=cand.get("source") or "chat_extract",
                importance=float(cand.get("importance", 0.6)),
                embedding=emb, embedding_src=emb_src,
                source_conv_id=cand.get("conv_id"), agent_run_id=cand.get("run_id"),
            )
        await asyncio.to_thread(mstore.supersede, target, mid)
        old_content = next((h.content for h in hits if h.memory_id == target), None)
        await asyncio.to_thread(mstore.add_audit, mid, "UPDATE", old=old_content,
                                new=content, reason=f"supersedes {target}: {reason}",
                                job_id=job_id)
    elif op == "DELETE" and target:
        ok = await asyncio.to_thread(mstore.retire, target, reason=reason or "reconcile DELETE",
                                     job_id=job_id)
        if ok and cand.get("id") and target != cand["id"]:
            # 矛盾裁决：旧的作废，候选（承载新事实的迁移行）转正
            await asyncio.to_thread(mstore.adopt, cand["id"], content=cand["content"],
                                    title=cand.get("title", ""), embedding=emb,
                                    embedding_src=emb_src)
        elif ok and target == cand.get("id"):
            pass  # LLM 让候选自我作废：已被 retire 命中
    else:  # NOOP / 参数不足的降级
        if cand.get("id"):
            # 待确认旧记忆：NOOP=认可其存在（等价重复只保留双方各自身份，由 DELETE 处理矛盾）
            await asyncio.to_thread(mstore.adopt, cand["id"], content=cand["content"],
                                    title=cand.get("title", ""), embedding=emb,
                                    embedding_src=emb_src)
            await asyncio.to_thread(mstore.add_audit, cand["id"], "NOOP",
                                    new=cand["content"], reason=reason, job_id=job_id)
        else:
            await asyncio.to_thread(mstore.add_audit, target or "-", "NOOP",
                                    new=cand["content"],
                                    reason=f"candidate dropped as duplicate: {reason}",
                                    job_id=job_id)
