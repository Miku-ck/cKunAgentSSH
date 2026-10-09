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
# 无 LLM 时的纯阈值去重：余弦 ≥ 此值或归一化文本全等 → 判等价重复丢弃。
# 0.95 是"近似同一句"的经验值；阈值以下宁可能误留（ADD）也不误杀新事实。
_DEDUP_COSINE = 0.95

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


def format_episodic(task: str, tool_calls: list, final_content: str, iterations: int,
                    folded_summary: str = "") -> str:
    """从 Agent 运行轨迹构建 episodic 摘要（纯规则，迁移自 v1 MemoryManager）。

    folded_summary = run 内被压缩掉的早期上下文摘要（spec §8）：不带它，长 run 的
    经验只剩最后几轮，后台 LLM 改写再怎么发挥也无米下锅。
    """
    parts = [f"任务: {task.strip()[:200]}"]
    if iterations:
        parts.append(f"迭代次数: {iterations}")
    if tool_calls:
        tool_names = [tc.get("tool", "?") for tc in tool_calls]
        parts.append(f"使用工具: {', '.join(tool_names[:8])}")
    if folded_summary:
        parts.append(f"早期过程: {folded_summary.strip().replace(chr(10), ' ')[:400]}")
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


async def _target_actionable(memory_id: str) -> bool:
    """裁决点名的 target 能否被对账触碰：行存在、仍 active、且没被用户钉住。

    不给 mstore.retire/supersede 加 pinned/存在性条件：用户在界面上对自己的钉住行
    做删除/取代是显式意图，那是另一条通道；本模块的契约是「自动对账不碰 pinned」，
    并且不能让一个不存在的 id 走进「add_memory 已 commit、supersede 才抛错」的位置。
    """
    row = await asyncio.to_thread(mstore.get_memory, memory_id)
    if not row:
        logger.warning(
            "[reconcile] 裁决 target_id=%s 在库里不存在，按缺 target 同款降级", memory_id)
        return False
    if row.get("status") != "active":
        logger.warning(
            "[reconcile] 裁决 target_id=%s 状态为 %s（非 active），按缺 target 同款降级",
            memory_id, row.get("status"))
        return False
    if row.get("pinned"):
        logger.info(
            "[reconcile] target_id=%s 被用户钉住，对账路径不触碰，按缺 target 同款降级",
            memory_id)
        return False
    return True


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


def _llm_enabled() -> bool:
    """对账是否允许调模型（设置关掉后退化为纯阈值去重）"""
    import database as db
    try:
        return bool(db.get_memory_settings().get("reconcile_llm_enabled", True))
    except Exception:
        return True


async def process_jobs(*, max_jobs: int = 20, embed_client=None, model_cfg=None,
                       llm_enabled: "bool | None" = None) -> dict:
    """消费一批 job。由意识 tick 与 trigger_now 调用。

    llm_enabled=None 时读设置；显式传入（测试/手动触发）优先。
    """
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
    if llm_enabled is None:
        llm_enabled = await asyncio.to_thread(_llm_enabled)
    # 关掉对账 LLM = 既不调去重裁决，也不做 episodic 改写：一条设置管两处，
    # 否则"省 token"的开关只关掉一半，后台照样每轮打模型。
    effective_cfg = model_cfg if llm_enabled else {}

    counters = {"ADD": 0, "UPDATE": 0, "DELETE": 0, "NOOP": 0, "episodic": 0}
    failed = 0
    for job in jobs:
        try:
            c = await _process_one(job, embed_client, effective_cfg, llm_enabled=llm_enabled)
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


async def _process_one(job: dict, embed_client, model_cfg: dict, *, llm_enabled: bool = True) -> dict:
    payload = json.loads(job.get("payload") or "{}")
    kind = job["kind"]
    if kind == JOB_EPISODIC:
        return await _write_episodic(payload, job["id"], embed_client, model_cfg)
    if kind == JOB_AGENT_ADD:
        payload.setdefault("source", "agent_tool")
        return await _reconcile_candidates([payload], job["id"], embed_client, model_cfg,
                                           priority_add=True, llm_enabled=llm_enabled)
    if kind == JOB_CANDIDATE:
        items = [
            {**it, "conv_id": payload.get("conv_id"), "run_id": payload.get("run_id"),
             "source": "chat_extract"}
            for it in payload.get("items", []) if (it.get("content") or "").strip()
        ]
        if not items:
            return {}
        return await _reconcile_candidates(items, job["id"], embed_client, model_cfg,
                                           llm_enabled=llm_enabled)
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
        return await _reconcile_candidates(cands, job["id"], embed_client, model_cfg,
                                           llm_enabled=llm_enabled)
    raise ValueError(f"未知 job kind: {kind}")


async def _write_episodic(payload: dict, job_id: str, embed_client, model_cfg: dict) -> dict:
    """episodic 是事实记录不参与对账（spec §5 表），直接入 active。

    快路径已在 run 收尾用规则拼出结构化轨迹（format_episodic）；这里在有 LLM 时
    再把它改写成一句可读的语义摘要，让"何时/做了什么/结果"更像自然语言经验。
    改写失败（超时/空返回）保留规则版，绝不因质量优化把 job 打成失败——轨迹已在。
    """
    content = (payload.get("content") or "").strip()
    if not content:
        return {}
    if model_cfg:
        content = await _rewrite_episodic(content, model_cfg) or content
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


_EPISODIC_REWRITE_PROMPT = """\
把下面这段 Agent 运行轨迹压成 1-2 句中文经验记录，保留关键动作与结果，\
去掉迭代次数/工具清单等噪声字段。只输出摘要文本本身，不要前缀、不要引号。
"""


async def _rewrite_episodic(content: str, model_cfg: dict) -> str:
    try:
        from post_processor import _call_llm_with_timeout  # 延迟导入避免环
        model_id = model_cfg.get("id") or model_cfg.get("model_id") or ""
        text = await _call_llm_with_timeout(
            messages=[
                {"role": "system", "content": "你是运行日志摘要器。"},
                {"role": "user", "content": _EPISODIC_REWRITE_PROMPT + "\n\n" + content},
            ],
            model_cfg=model_cfg, model_id=model_id,
            temperature=0.2, max_tokens=256,
            chunk_timeout=_CHUNK_TIMEOUT, total_timeout=_TOTAL_TIMEOUT,
            task_name="episodic_rewrite",
        )
    except Exception as e:
        logger.warning("[reconcile] episodic 改写失败，保留规则版: %s", e)
        return ""
    text = (text or "").strip()
    # 改写结果异常膨胀（模型跑偏/复读）时丢弃，用规则版
    if len(text) < 8 or len(text) > len(content) * 2:
        return ""
    return text


async def _embed(embed_client, content: str):
    if embed_client is None:
        return None, ""
    vec = await embed_client.embed_one(content)
    model = getattr(embed_client, "model", "") or ""
    return (vec, f"api:{model}") if vec else (None, "")


def _norm_content(text: str) -> str:
    return "".join((text or "").split()).lower()


def _threshold_decisions(cands: list, similars: list) -> "list[dict]":
    """LLM 关掉时的降级裁决：全等/近全等 → NOOP（等价重复），否则 ADD。

    只做"防重复入库"这一件保守的事：不做 UPDATE/DELETE 合并（那需要语义理解，
    宁可留给下次开 LLM 的对账，也不在无模型时误作废记忆）。
    """
    decisions = []
    for i, (cand, hits) in enumerate(zip(cands, similars)):
        dup = None
        cand_norm = _norm_content(cand.get("content"))
        for h in hits:
            if cand_norm and cand_norm == _norm_content(h.content):
                dup = h
                break
            if h.relevance is not None and h.relevance >= _DEDUP_COSINE:
                dup = h
                break
        if dup is not None and not cand.get("id"):
            decisions.append({"index": i, "op": "NOOP", "target_id": dup.memory_id,
                              "reason": "阈值去重（无对账 LLM）"})
        else:
            # 迁移行无 LLM 可裁决时按 ADD 语义"转正"（沿用原 id，不重复入库）。
            decisions.append({"index": i, "op": "ADD", "reason": "无对账 LLM，直接保留"})
    return decisions


async def _reconcile_candidates(
    cands: list, job_id: str, embed_client, model_cfg: dict, *, priority_add: bool = False,
    llm_enabled: bool = True,
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
        if not llm_enabled:
            decisions = _threshold_decisions(cands, similars)
        # 无模型/LLM 失败：agent 工具是用户显式意图，直写；其余留给下个 tick 重试
        elif priority_add:
            decisions = [{"index": i, "op": "ADD"} for i in range(len(cands))]
        else:
            raise RuntimeError("LLM 对账不可用，job 待重试")

    counters: dict = {}
    by_index = {}
    for d in decisions:
        if isinstance(d, dict) and isinstance(d.get("index"), int):
            by_index[d["index"]] = d
    applied: dict = {}          # 归一内容 -> 本批已写入的 memory_id
    for i, cand in enumerate(cands):
        d = by_index.get(i) or {"op": "NOOP" if not priority_add else "ADD"}
        op = normalize_op(d.get("op"))
        # LLM 若违反 prompt 约束（§44-47：UPDATE/DELETE 必须给 target_id）漏给目标，
        # 绝不能用相似度首位 hits[0] 兜底——那会 supersede/retire 一条"恰好检索得最靠前、
        # 却并非该候选所指"的无关记忆，静默毁数据。按本模块"宁可误留不误杀"降级：
        # UPDATE 无 target → ADD（保留候选增量，不动既有行）；DELETE 无 target → NOOP
        # （丢弃候选，不作废任何行）。降级后的 op 才计入 counters，令统计与实际效果一致。
        if op in ("UPDATE", "DELETE"):
            target_id = d.get("target_id")
            # S3-01 / S3-04 修复：「给了 target」不等于「target 可用」。两种不可用都按
            # 本模块既有的「缺 target」同款降级（UPDATE→ADD / DELETE→NOOP）：
            # ① 行不存在或已非 active —— supersede 会在 add_memory 已 commit 之后抛
            #    ValueError（`_apply` 的顺序），留下 active 孤儿行 + 零条审计，job 留在
            #    pending 被反复重放（LEGACY #3 的确定性触发路径，UI 点「重试」还会把
            #    attempts 归零、每点一次再叠 3 份重复）；
            # ② 行被用户钉住 —— spec 明令对账路径不碰 pinned，而 retire 的 WHERE 只有
            #    状态守卫、supersede 是裸 `id=?`，_similar_existing 也只看 active。
            # 注：战役报告建议的另一半（target 必须属于**本候选**的对照面）没有采纳 ——
            # 它与既有测试 test_delete_invalidates_not_physical 锁定的契约冲突（该用例
            # 的 target 由调用方直接点名、不经检索）。要不要收紧到「只认 prompt 里
            # 展示过的 id」是一个真实的产品裁决点，不是可以顺手改掉的实现细节。
            if target_id and not await _target_actionable(target_id):
                target_id = None
            if not target_id:
                op = "ADD" if op == "UPDATE" else "NOOP"
                # 清掉裁决里的 target_id：_apply 还会读它，留着等于把刚拒掉的
                # 那个 id 又交给下游（NOOP 分支拿它写审计标签）
                d = {**d, "target_id": None}
        norm = _norm_content(cand.get("content"))
        if op in ("ADD", "UPDATE") and norm and norm in applied:
            # 同批兄弟候选已经写过同一内容了。`_similar_existing` 是在这个循环**之前**
            # 一次性 gather 的，批内成员彼此看不见 ⇒ 一批 4 条相同内容落 4 行同文（实测），
            # 而抽取器就是按批发多条（post_processor.py:667）—— 最常见的重复来源恰好是
            # 这一批自己。降级为 NOOP：宁可误留不误杀，也不落第二行同文。
            op = "NOOP"
            d = {**d, "target_id": applied[norm],
                 "reason": "同批重复（已由更早的候选写入）"}
        counters[op] = counters.get(op, 0) + 1
        touched = await _apply(cand, op, d, similars[i], job_id, embed_client)
        # DELETE 的 touched 是被作废的既有行，与候选正文无关，不能当成"本批已写入的内容"
        if touched and norm and op != "DELETE" and norm not in applied:
            applied[norm] = touched
    return counters


KNOWN_OPS: frozenset[str] = frozenset({"ADD", "UPDATE", "DELETE", "NOOP"})


def normalize_op(raw: object) -> str:
    """把模型给的 op 收进四个已知值，未知一律按 NOOP 记账。

    `_apply` 的分支是 if ADD / elif UPDATE+target / elif DELETE+target / else NOOP，
    所以未知 op（实测模型会回 `"MERGE"`）执行的其实是 NOOP 分支；而计数原先直接
    `counters[op] += 1`，摘要里就多出一笔从未发生过的动作 —— job 统计与 UI 显示的效果
    和实际落库不一致。计数必须与真正执行的分支同源，而不是与模型的措辞同源。
    """
    op = str(raw or "NOOP").strip().upper()
    return op if op in KNOWN_OPS else "NOOP"


async def _apply(cand: dict, op: str, decision: dict, hits: list, job_id: str,
                 embed_client) -> None:
    """把一条裁决落到 memories + memory_audit。

    返回本次实际动到的 memory_id（没动到任何行则 None）—— 调用方要用它在**批内**建立
    「归一内容 -> 已写入行」的映射：`_similar_existing` 只看库里的既有行，看不见同批兄弟。
    """
    reason = str(decision.get("reason") or "")[:300]
    # 只认裁决自己点名的 target。原先这里还兜到 `hits[0].memory_id`（相似度首位）——
    # 上面 379-383 的注释正是禁止这么做的：拿检索最靠前的那条去 supersede/retire 等于
    # 静默毁掉一条并非候选所指的无关记忆。今天它碰不到后果（读 target 的两个分支都被
    # 上游的 target_id 校验挡过），但"死而不僵"的兜底会被下一个新增分支顺手接走，
    # 删掉比留着安全。
    target = decision.get("target_id")
    # 审查R2修复：嵌入必须按最终入库的 content 计算 —— UPDATE 分支存的是
    # merged_content，原实现却恒按 cand["content"] 算向量，向量与正文永久错位，
    # 语义检索按旧文本命中新正文。判据与下方 elif 条件一致；去重 NOOP 分支
    # （无 id 不入库）本就不消费该向量，行为不变。
    content = cand["content"]
    if op == "UPDATE" and target and target != cand.get("id"):
        content = str(decision.get("merged_content") or cand["content"])
    emb, emb_src = await _embed(embed_client, content)

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
        return mid
    elif op == "UPDATE" and target and target != cand.get("id"):
        # content 已在函数入口按最终入库文本确定（含 merged_content），向量同源
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
        return mid
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
        return target if ok else None
    else:  # NOOP / 参数不足的降级
        if cand.get("id"):
            # 待确认旧记忆：NOOP=认可其存在（等价重复只保留双方各自身份，由 DELETE 处理矛盾）
            await asyncio.to_thread(mstore.adopt, cand["id"], content=cand["content"],
                                    title=cand.get("title", ""), embedding=emb,
                                    embedding_src=emb_src)
            await asyncio.to_thread(mstore.add_audit, cand["id"], "NOOP",
                                    new=cand["content"], reason=reason, job_id=job_id)
            return cand["id"]
        else:
            await asyncio.to_thread(mstore.add_audit, target or "-", "NOOP",
                                    new=cand["content"],
                                    reason=f"candidate dropped as duplicate: {reason}",
                                    job_id=job_id)
            return None
