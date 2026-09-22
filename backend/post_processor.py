"""
单轮对话后处理 — 异步提取记忆、自动生成技能、生成会话标题

重构要点（P0+P1+P2）：
1. 任务解耦：三任务拆为独立 LLM 调用，互不影响
2. 配置化：PostProcessConfig 控制启停/超时/频率/上限
3. 去重 + 质量门：技能 slug 去重 + system_prompt 长度门 + trigger_words 清洗；记忆相似度检查
4. 生命周期统一：PostProcessManager 管理任务，应用关闭时优雅退出
5. prompt 精简：assistant_content/reasoning 截断，降低本地模型负担
6. 超时统一：单 chunk 超时 + 总超时双层保护，内外层语义一致
7. 标题仅首轮调用 LLM：非首轮直接跳过，不浪费 token

设计原则：
- 失败隔离：任一子任务失败不影响其他
- 单次 LLM 调用/子任务：每个子任务独立调用 LLM
- 异步执行：通过 PostProcessManager.submit 启动，不阻塞 SSE 响应
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import database as db
from llm_adapter import chat_stream
from prompts import MODE_PROMPTS

logger = logging.getLogger(__name__)

AUTO_SKILL_CATEGORY = {
    "ssh": "运维脚本",
}

MAX_TITLE_LEN = 20

# 默认会话标题集合（产品改名时只需更新此处，避免硬编码导致逻辑静默失效）
_DEFAULT_TITLES = {"New Chat", "新对话", "New Conversation", ""}


# ──────────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class PostProcessConfig:
    """后处理配置 — 控制启停/超时/频率/上限"""
    enabled: bool = True
    # 子任务独立开关
    generate_title: bool = True
    extract_memory: bool = True
    generate_skill: bool = True
    # 模型选择（空则用主对话模型）
    title_model_id: str = ""        # 标题用轻量模型（推荐 Instruct）
    memory_model_id: str = ""
    skill_model_id: str = ""
    # 单 chunk 超时（秒）—— 等待 LLM 首 token 或后续 token
    chunk_timeout: int = 180
    # 子任务总超时（秒）
    title_total_timeout: int = 30
    memory_total_timeout: int = 120
    skill_total_timeout: int = 150
    # 整体总超时（秒）—— PostProcessManager 外层保护
    total_timeout: int = 300
    # 频率（每 N 轮执行一次）
    memory_interval: int = 1        # 记忆每轮提取
    skill_interval: int = 3         # 技能每 3 轮生成一次
    # 数量上限
    max_skills_per_conversation: int = 5   # 全局 auto 技能上限（防数据库污染，非 per-conversation）
    max_memories_per_turn: int = 3
    # prompt 截断（降本地模型负担）
    max_content_chars: int = 2000
    max_reasoning_chars: int = 500
    # 去重阈值
    memory_similarity_threshold: float = 0.85   # cosine 相似度超过此值视为重复
    skill_min_system_prompt_len: int = 50       # system_prompt 最少字数（质量门）


# 全局配置实例（由 main.py 启动时从 DB 加载，或用默认值）
_config: PostProcessConfig = PostProcessConfig()


def get_post_process_config() -> PostProcessConfig:
    return _config


def set_post_process_config(cfg: PostProcessConfig) -> None:
    global _config
    _config = cfg


# ──────────────────────────────────────────────────────────────────────────────
# 上下文
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class TurnContext:
    """单轮对话上下文 — 传给各子任务"""
    mode: str
    conversation_id: str
    user_message: str
    assistant_content: str
    assistant_reasoning: str
    model_id: str
    turn_index: int = 0     # 当前会话第几轮（0-based）


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────


def _truncate_for_post_process(
    content: str,
    reasoning: str,
    config: PostProcessConfig,
) -> tuple[str, str]:
    """截断 prompt 内容，降低本地模型 prefill 负担"""
    truncated_content = content[:config.max_content_chars]
    if len(content) > config.max_content_chars:
        truncated_content += f"\n...[已截断，原始 {len(content)} 字]"

    # reasoning 只取开头（思考链的核心结论通常在开头）
    truncated_reasoning = reasoning[:config.max_reasoning_chars] if reasoning else ""
    if reasoning and len(reasoning) > config.max_reasoning_chars:
        truncated_reasoning += f"\n...[已截断，原始 {len(reasoning)} 字]"

    return truncated_content, truncated_reasoning


def _resolve_model_id(config_field: str, fallback: str) -> str:
    """解析子任务模型 ID：配置为空则用主对话模型"""
    return config_field.strip() or fallback


def _clean_trigger_words(words) -> list[str]:
    """清洗 trigger_words：去重 + 长度检查 + 特殊字符过滤

    入参是 LLM 产出的 JSON，形状不受控：常把数组写成单个逗号分隔字符串
    （"部署,发布,k8s"）。原实现直接 for w in words 逐字符迭代，全部被
    长度门滤掉 → 技能零触发词 → match_skills 永不命中，自演化技能静默失效。
    """
    if isinstance(words, str):
        words = re.split(r"[,，;；、/|]+", words)
    elif not isinstance(words, (list, tuple)):
        words = []
    seen: set = set()
    result: list[str] = []
    for w in words:
        w = str(w).strip()
        # 长度门：2-20 字符
        if not w or len(w) < 2 or len(w) > 20:
            continue
        # 特殊字符过滤（只允许中英文/数字/连字符）
        if not re.match(r"^[\w\u4e00-\u9fff-]+$", w):
            continue
        # 去重（小写比较）
        key = w.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(w)
    return result[:5]


def _extract_json(text: str) -> str:
    """从文本中提取 JSON 块（支持 markdown 代码块包裹 + 嵌套 JSON）"""
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        if _is_valid_json(candidate):
            return candidate
    result = _extract_outer_json(text)
    if result:
        return result
    return text.strip()


def _is_valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _extract_outer_json(text: str) -> str | None:
    """通过 brace-counting 提取最外层 JSON 对象"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return None


def _is_skill_like(obj: dict) -> bool:
    """粗略判定一个对象是否技能对象（只做字段存在性判断，不校验值）。"""
    return any(k in obj for k in ("name", "slug", "system_prompt"))


def _closed_object_at(text: str, start: int):
    """text[start] 必为 '{'。若能自闭合则返回 (start, end)，否则返回 None。

    逐字符跳过字符串进出与转义，避免把字符串里的花括号误当结构符。
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (start, i)
    return None


def _collect_closed_json_objects(text: str) -> list[str]:
    """扫描文本，摘出每个能自闭合的对象字面量片段。

    外层对象因截断（深度永不归零）无法闭合时，这里仍能逐个抢救到内层
    已完整闭合的技能对象（如 $$skills[0]$$ 完整、$$skills[1]$$ 中途被截断时，
    前者会被捞回）。
    """
    collected: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if ch == "{":
            closed = _closed_object_at(text, i)
            if closed is not None:
                start, end = closed
                collected.append(text[start:end + 1])
                i = end + 1
                continue
        i += 1
    return collected


def _try_repair_truncated(candidate: str) -> list[dict]:
    """截断容错修复：在候选尾部补闭合符后尝试解析，成功即返回 skills。

    截断点常在字符串中途，故先补一个闭合引号（若需要），再按常见嵌套
    「技能对象 -> skills 数组 -> 外层对象」及 trigger_words 数组被截断的
    组合逐层补 ] / }。保守：任一组合能通过 json.loads 才算数，否则返回空，
    宁可不救也不把垃圾字段当技能。
    """
    # 去重、有序的后缀候选（含「先引号后结构」与纯结构两类，嵌套混合覆盖））
    closings = list(dict.fromkeys((
        '"',           # 仅闭合被截断的字符串
        '"}',          # + 技能对象
        '"}]',         # + 技能对象 + skills 数组
        '"}]}',        # + 技能对象 + skills 数组 + 外层对象（最常见形态）
        '"]}',         # 技能对象内 trigger_words 数组被截断
        '"]}]}',
        '"]}]',        # 纯结构：trigger_words 数组 + skills 数组（外两层另补）
        '"]',
        '"}',
        '}',
        ']}',
        '}]',
        '}]}',
        ']',
        ']}',
        '}]',
        '}]}',
        ']}]',
        ']}]}',
    )))
    for tail in closings:
        try:
            parsed = json.loads(candidate + tail)
        except (json.JSONDecodeError, ValueError):
            continue
        skills = _llm_list(parsed, "skills")
        if skills:
            return skills
        if isinstance(parsed, dict) and _is_skill_like(parsed):
            return [parsed]
    return []


def _parse_skills_lenient(full_text: str) -> list[dict]:
    """钝化解析技能 JSON：LLM 输出被截断在字符串中途时仍尽量抢救技能。

    本地小模型写长 system_prompt 常因 max_tokens 不足被切在
    `"system_prompt": "# ...` 中间，正常 json.loads 会抛 Unterminated string，
    于是整条技能（含已完整的 name/slug/description）被整条丢弃。此函数三步兜底。
    """
    # 第一步：候选上正常解析
    candidate = _extract_json(full_text)
    try:
        result = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        result = None
    if isinstance(result, dict) and isinstance(result.get("skills"), list):
        return result["skills"]

    # 第二步：brace 计数逐一摘出可自闭合的 {...} 技能对象（截断也能救回已完整的）
    skills: list[dict] = []
    for frag in _collect_closed_json_objects(candidate):
        try:
            obj = json.loads(frag)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and _is_skill_like(obj):
            skills.append(obj)

    # 第三步：截断字符串的保守修复（name/slug/description 完整 + 仅 system_prompt 被截断）
    if not skills:
        skills = _try_repair_truncated(candidate)

    return skills


async def _call_llm_with_timeout(
    messages: list[dict],
    model_cfg: dict,
    model_id: str,
    temperature: float,
    max_tokens: int,
    chunk_timeout: float,
    total_timeout: float,
    task_name: str,
) -> Optional[str]:
    """带双层超时的 LLM 调用

    chunk_timeout: 单 chunk 间隔超时
    total_timeout: 总超时（整个 LLM 调用不能超过此值）

    超时/异常后显式 aclose() generator，避免 httpx stream 连接泄漏。
    """
    full_text = ""
    chunk_iter = chat_stream(
        base_url=model_cfg["base_url"],
        api_key=model_cfg["api_key"],
        model=model_cfg.get("model_id") or model_id,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=False,
    )

    deadline = asyncio.get_event_loop().time() + total_timeout
    try:
        done = False
        while not done:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                logger.warning("[post_process:%s] 总超时 %ss", task_name, total_timeout)
                return None
            remaining = deadline - now
            # 单 chunk 超时不能超过剩余时间
            chunk_wait = min(chunk_timeout, remaining)
            try:
                chunk = await asyncio.wait_for(chunk_iter.__anext__(), timeout=chunk_wait)
                full_text += chunk.content
            except StopAsyncIteration:
                done = True
            except asyncio.TimeoutError:
                logger.warning("[post_process:%s] 单 chunk 超时 %ss", task_name, chunk_timeout)
                return None
    except Exception:
        logger.exception("[post_process:%s] LLM 调用异常", task_name)
        return None
    finally:
        # 显式关闭 async generator，释放 httpx stream 连接
        try:
            await chunk_iter.aclose()
        except Exception:
            pass

    if not full_text or not full_text.strip():
        logger.warning("[post_process:%s] LLM 返回空内容", task_name)
        return None
    return full_text


def _fallback_title(user_message: str, max_len: int = MAX_TITLE_LEN) -> str:
    """LLM 不可用时，从用户首条消息中提取一个简短标题（核心摘要）。"""
    if not user_message:
        return ""
    # 移除常见 markdown 标记和代码块，保留可读文本
    text = re.sub(r"```[\s\S]*?```", "", user_message)
    text = re.sub(r"[`#*\[\](){}<>|\\]", "", text)
    text = text.replace("\n", " ").strip()
    # 去掉开头的非语义字符
    text = re.sub(r"^[^\w\u4e00-\u9fff]+", "", text)
    if not text:
        return ""
    # 优先在标题长度附近断句，保持语义完整
    # 审查修复：旧 `range(max_len, upper + 1)` 在 max_len < len(text) ≤ max_len+10 时
    # i 可达 len(text) → text[i] IndexError，回退标题路径直接崩掉
    upper = min(len(text), max_len + 10)
    for i in range(max_len, upper):
        if text[i] in "。，！？,.;；":
            return text[:i]
    return text[:max_len]


# ──────────────────────────────────────────────────────────────────────────────
# 子任务 1：标题生成（仅首轮调用 LLM）
# ──────────────────────────────────────────────────────────────────────────────


_TITLE_PROMPT_TEMPLATE = """\
请基于以下用户问题，生成一个 {max_len} 字以内的中文会话标题。
仅输出标题文本，不要包含引号、标点或其他说明。

用户问题：{user_message}
"""


async def _generate_title(ctx: TurnContext, config: PostProcessConfig) -> None:
    """生成会话标题 — 标题仍是默认值时调用 LLM（幂等）

    M5 修复：原实现有「仅首轮」（turn_index == 0）门槛，但首轮失败（user 消息已入库）
    后次轮 turn_index=1 → 门槛永久关闭，会话名停在 "New Chat" 且永远补不回来。
    下方「标题仍是默认值」检查已保证幂等（已生成标题则不覆盖、用户自定义标题不动），
    故去掉首轮门槛，让失败/跳过的轮次能补生成标题。
    """
    if not config.generate_title:
        return

    # 检查当前标题是否仍是默认值
    # D 修复（事件循环阻塞）：db 同步调用放线程池
    try:
        conv = await asyncio.to_thread(db.get_conversation, ctx.conversation_id)
        # title 可能是 NULL：.get("title", "") 只对「键缺失」兜底，NULL 列返回 None，
        # 而 None 不在 _DEFAULT_TITLES 里 → 判成「已有自定义标题」永久早退，
        # 这条会话再也补不上标题（db.try_set_default_conversation_title 是显式处理
        # title IS NULL 的，两边口径要对齐）。"" 在集合内，所以归一到 "" 即正确。
        if not conv or (conv.get("title") or "") not in _DEFAULT_TITLES:
            return
    except Exception:
        logger.exception("[post_process:title] 查询会话失败")
        return

    # 解析模型
    title_model = _resolve_model_id(config.title_model_id, ctx.model_id)
    model_cfg = await asyncio.to_thread(db.get_model_config, title_model, mask_key=False)
    if not model_cfg:
        logger.warning("[post_process:title] unknown model_id: %s", title_model)
        # 模型不可用也尽量给出规则摘要，避免标题始终为 New Chat
        title = _fallback_title(ctx.user_message)
        if title:
            try:
                # C-review MINOR-1：条件更新 — 窗口期内用户已改名则不覆盖
                applied = await asyncio.to_thread(
                    db.try_set_default_conversation_title,
                    ctx.conversation_id, title, tuple(_DEFAULT_TITLES))
                if applied:
                    logger.info("[post_process:title] 已使用 fallback 更新标题: %s", title)
            except Exception:
                logger.exception("[post_process:title] fallback 更新标题失败")
        return

    # 标题只需用户问题，不需要 assistant 内容（降负担）
    messages = [
        {"role": "system", "content": "你是会话标题生成器，输出简洁的中文标题。"},
        {"role": "user", "content": _TITLE_PROMPT_TEMPLATE.format(
            max_len=MAX_TITLE_LEN, user_message=ctx.user_message[:500],
        )},
    ]

    full_text = await _call_llm_with_timeout(
        messages=messages,
        model_cfg=model_cfg,
        model_id=title_model,
        temperature=0.3,
        max_tokens=50,   # 标题只需 50 token
        chunk_timeout=config.chunk_timeout,
        total_timeout=config.title_total_timeout,
        task_name="title",
    )
    if not full_text:
        # LLM 失败时降级为本地规则摘要，避免会话名始终为 New Chat
        title = _fallback_title(ctx.user_message)
        if title:
            try:
                # C-review MINOR-1：条件更新 — LLM 窗口期内用户已改名则不覆盖
                applied = await asyncio.to_thread(
                    db.try_set_default_conversation_title,
                    ctx.conversation_id, title, tuple(_DEFAULT_TITLES))
                if applied:
                    logger.info("[post_process:title] 已使用 fallback 更新标题: %s", title)
                else:
                    logger.info("[post_process:title] 标题已在窗口期内被用户修改，放弃覆盖")
            except Exception:
                logger.exception("[post_process:title] fallback 更新标题失败")
        return

    title = full_text.strip().strip('"\'').strip()[:MAX_TITLE_LEN]
    if not title:
        title = _fallback_title(ctx.user_message)
        if not title:
            return

    try:
        # C-review MINOR-1：check-then-act 竞态 — 标题 LLM 调用长达 30s，
        # 窗口期内用户改的自定义标题会被无条件覆盖。改为条件 UPDATE
        # （仅当前标题仍是默认值时写入），检查与写入同语句原子完成
        applied = await asyncio.to_thread(
            db.try_set_default_conversation_title,
            ctx.conversation_id, title, tuple(_DEFAULT_TITLES))
        if applied:
            logger.info("[post_process:title] 已更新标题: %s", title)
        else:
            logger.info("[post_process:title] 标题已在 LLM 窗口期内被用户修改，放弃覆盖")
    except Exception:
        logger.exception("[post_process:title] 更新标题失败")


# ──────────────────────────────────────────────────────────────────────────────
# 子任务 2：记忆提取（带相似度去重）
# ──────────────────────────────────────────────────────────────────────────────


_MEMORY_PROMPT_TEMPLATE = """\
请分析以下 AI 助手与用户的单轮对话，提取关键记忆（最多 {max_count} 条）。
仅提取值得长期记住的信息：用户偏好、重要事实、可复用上下文经验。

每条记忆输出一个 JSON 对象：
- title: 不超过 20 字的短标题
- content: 一句话表达的记忆内容
- kind: "semantic"（稳定事实/偏好）或 "procedural"（可复用的操作经验/教训）
- scope: "global"（与具体主机无关时）

请严格按以下 JSON 格式输出，不要包含其他内容：
{{
  "memories": [
    {{"title": "...", "content": "...", "kind": "semantic", "scope": "global"}}
  ]
}}

用户消息：
{user_message}

助手回复：
{assistant_content}
"""


async def _extract_memories(ctx: TurnContext, config: PostProcessConfig) -> None:
    """提取记忆 + 相似度去重"""
    if not config.extract_memory:
        return
    # 频率控制
    if ctx.turn_index % config.memory_interval != 0:
        return

    memory_model = _resolve_model_id(config.memory_model_id, ctx.model_id)
    model_cfg = await asyncio.to_thread(db.get_model_config, memory_model, mask_key=False)
    if not model_cfg:
        logger.warning("[post_process:memory] unknown model_id: %s", memory_model)
        return

    # 截断 prompt
    truncated_content, _ = _truncate_for_post_process(
        ctx.assistant_content, ctx.assistant_reasoning, config,
    )

    messages = [
        {"role": "system", "content": MODE_PROMPTS.get(ctx.mode, MODE_PROMPTS["ssh"])},
        {"role": "user", "content": _MEMORY_PROMPT_TEMPLATE.format(
            max_count=config.max_memories_per_turn,
            user_message=ctx.user_message[:1000],
            assistant_content=truncated_content,
        )},
    ]

    full_text = await _call_llm_with_timeout(
        messages=messages,
        model_cfg=model_cfg,
        model_id=memory_model,
        temperature=0.3,
        max_tokens=512,
        chunk_timeout=config.chunk_timeout,
        total_timeout=config.memory_total_timeout,
        task_name="memory",
    )
    if not full_text:
        return

    # 解析 JSON
    try:
        raw = _extract_json(full_text)
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("[post_process:memory] JSON 解析失败: %s, raw=%s", e, full_text[:200])
        return

    memories = _llm_list(result, "memories")[:config.max_memories_per_turn]
    if not memories:
        return

    # v2：本地不再做相似度去重，提取产物整批投 candidate_fact job，
    # 是否入库/合并/作废由 Reconciler 在后台统一裁决（spec §5：唯一写入口）。
    items = []
    for mem in memories:
        if isinstance(mem, str):
            mem = {"content": mem}
        content = (mem.get("content") or "").strip()
        if not content:
            continue
        items.append({
            "title": (mem.get("title") or "").strip()[:60],
            "content": content,
            "kind": mem.get("kind") if mem.get("kind") in ("semantic", "procedural") else "semantic",
            "scope": "global",
            "importance": 0.6,
        })
    if not items:
        return
    try:
        from agent.memory import mstore
        from agent.memory.reconcile import JOB_CANDIDATE
        await asyncio.to_thread(
            mstore.enqueue_job, JOB_CANDIDATE,
            {"items": items, "conv_id": ctx.conversation_id, "run_id": None},
            5,
        )
        logger.info("[post_process:memory] 投递 %d 条候选记忆 job", len(items))
    except Exception as e:
        logger.warning("[post_process:memory] 投递候选 job 失败: %s", e)


async def _is_memory_duplicate(
    mem_mgr,
    content: str,
    mode: str,
    threshold: float,
) -> bool:
    """检查记忆是否与已有记忆相似"""
    try:
        # 用 MemoryManager.retrieve 检索（内部对 semantic 用 cosine 相似度）。
        # P2 拆双阈值后必须显式关掉 episodic 层：episodic 走 Jaccard、默认阈值
        # 0.15，一条不相干的运行总结会被判成"重复"而吞掉新记忆；去重本就只该
        # 与 semantic 层比对。
        results = await mem_mgr.retrieve(
            query=content, mode=mode, top_k=3, threshold=threshold,
            episodic_threshold=1.1,  # Jaccard ∈ [0,1]，1.1 = episodic 层不可达
        )
        return any(r.memory.type == "semantic" for r in results)
    except Exception:
        # 检索失败时不阻断保存（fail-open）
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 子任务 3：技能生成（带 slug 去重 + 质量门）
# ──────────────────────────────────────────────────────────────────────────────


_SKILL_PROMPT_TEMPLATE = """\
请分析以下 AI 助手与用户的单轮对话，判断是否可沉淀为技能。
仅当对话中包含可复用的操作步骤、模板、规则、脚本或最佳实践时，才生成技能；否则返回空数组。

每个技能必须包含：
- name: 技能名称（简洁）
- slug: 英文 slug（kebab-case）
- description: 技能描述
- system_prompt: 技能完整内容（至少 50 字，包含可复用的步骤/规则）
- trigger_words: 3-5 个触发词，当用户消息包含这些词时自动激活

请严格按以下 JSON 格式输出，不要包含其他内容：
{{
  "skills": [
    {{
      "name": "技能名称",
      "slug": "skill-slug",
      "description": "技能描述",
      "system_prompt": "技能内容",
      "trigger_words": ["触发词1", "触发词2"]
    }}
  ]
}}

用户消息：
{user_message}

助手回复：
{assistant_content}
"""


async def _generate_skill(ctx: TurnContext, config: PostProcessConfig) -> None:
    """生成技能 + slug 去重 + 质量门"""
    if not config.generate_skill:
        return
    # 频率控制
    if ctx.turn_index % config.skill_interval != 0:
        return

    # 数量上限检查（全局 auto 技能上限，防数据库污染）
    try:
        existing_count = await asyncio.to_thread(_count_auto_skills)
        if existing_count >= config.max_skills_per_conversation:
            logger.info(
                "[post_process:skill] 全局已有 %d 个 auto 技能，达上限 %d",
                existing_count, config.max_skills_per_conversation,
            )
            return
    except Exception:
        # 数不出来就按「已达上限」处理：这道保护的唯一目的就是防 auto 技能无限堆积，
        # 查询失败（锁库等）后继续生成等于保护在最容易出问题时形同虚设。
        # 原先 _count_auto_skills 内部把异常吞成 0，既要不到保护也让这个分支不可达。
        logger.warning("[post_process:skill] 查询技能数量失败，跳过本轮生成")
        return

    skill_model = _resolve_model_id(config.skill_model_id, ctx.model_id)
    model_cfg = await asyncio.to_thread(db.get_model_config, skill_model, mask_key=False)
    if not model_cfg:
        logger.warning("[post_process:skill] unknown model_id: %s", skill_model)
        return

    # 截断 prompt
    truncated_content, truncated_reasoning = _truncate_for_post_process(
        ctx.assistant_content, ctx.assistant_reasoning, config,
    )

    messages = [
        {"role": "system", "content": MODE_PROMPTS.get(ctx.mode, MODE_PROMPTS["ssh"])},
        {"role": "user", "content": _SKILL_PROMPT_TEMPLATE.format(
            user_message=ctx.user_message[:1000],
            assistant_content=truncated_content + (
                f"\n\n[思考过程]\n{truncated_reasoning}" if truncated_reasoning else ""
            ),
        )},
    ]

    full_text = await _call_llm_with_timeout(
        messages=messages,
        model_cfg=model_cfg,
        model_id=skill_model,
        temperature=0.3,
        # 技能 system_prompt 可能写得很长，1024 会在中段就把输出切在字符串里，
        # 导致整个 JSON 解析失败、整条技能连同已生成的 name/slug 一起丢弃。
        max_tokens=2048,
        chunk_timeout=config.chunk_timeout,
        total_timeout=config.skill_total_timeout,
        task_name="skill",
    )
    if not full_text:
        return

    # 解析 JSON：正常路径之外，LLM 输出常因 max_tokens 不足被切在字符串中途，
    # 此时走 _parse_skills_lenient 钝化抢救，尽量不整条丢弃已完整的技能对象。
    result = None
    try:
        raw = _extract_json(full_text)
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("[post_process:skill] JSON 解析失败: %s, raw=%s", e, full_text[:200])

    skills = _llm_list(result, "skills")
    if not skills:
        rescued = _parse_skills_lenient(full_text)
        if rescued:
            skills = rescued
            logger.info(
                "[post_process:skill] 钝化解析救回 %d 条技能（LLM 输出可能被截断）",
                len(rescued),
            )
    created_count = 0
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        created = await asyncio.to_thread(
            _create_auto_skill_with_quality_gate, skill, ctx.mode, config
        )
        if created:
            created_count += 1

    if created_count > 0:
        logger.info("[post_process:skill] 创建 %d 个技能", created_count)


def _count_auto_skills() -> int:
    """统计全局自动生成技能总数

    注：skills 表无 source_conv_id 字段，无法按会话统计。
    此处用全局上限保护，避免 auto 技能无限累积污染数据库。
    max_skills_per_conversation 实际语义为"全局 auto 技能上限"。
    如需真正的 per-conversation 限制，需在 skills 表新增 source_conv_id 字段。

    查询失败向上抛：调用方要按「数不出来 = 不放行」处理（见 _generate_skill）。
    """
    # 审查 MINOR 修复：COUNT 查询替代 list_skills(limit=9999) 全量加载
    # （list_skills 会拉取所有技能的 system_prompt 全文，仅为数一下行数）
    return db.count_skills_by_source("auto")


def _llm_list(result, key: str) -> list:
    """取 LLM JSON 结果中的数组字段，形状异常时归一为空列表。

    两处坑：① 模型常直接回一个顶层数组而非包一层对象，此时 result 是 list，
    调 .get 直接 AttributeError；② 键存在但值为 null 时 get 返回 None（默认值
    只对缺失键生效），后续切片/迭代 TypeError。原实现两种都靠外层 except 兜掉，
    代价是整轮记忆/技能提取静默丢失。
    """
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = result.get(key)
    else:
        items = None
    return items if isinstance(items, list) else []


def _llm_text(value) -> str:
    """LLM 产出的字符串字段归一：非字符串（null/数组/数字）一律按缺失处理。

    原实现 skill_dict.get("name","").strip() —— 键存在但值为 null 时 get 返回
    None（默认值只对缺失键生效），AttributeError 被外层 except 吞掉，整条技能
    连同其它合法字段一起丢失。这里不做 str() 强转：把 ["a","b"] 变成
    "['a', 'b']" 会伪造出一段可通过长度门的 system_prompt，不如判定为缺失。
    """
    return value.strip() if isinstance(value, str) else ""


def _create_auto_skill_with_quality_gate(
    skill_dict: dict,
    mode: str,
    config: PostProcessConfig,
) -> Optional[dict]:
    """创建技能 — 带 slug 去重 + 质量门"""
    try:
        name = _llm_text(skill_dict.get("name"))
        slug = _llm_text(skill_dict.get("slug"))
        if not name:
            return None
        if not slug:
            slug = name.lower().replace(" ", "-").replace("_", "-")
        # slug 规范化：只保留 kebab-case 字符
        slug = re.sub(r"[^a-z0-9-]", "", slug.lower())
        if not slug:
            return None

        # slug 去重
        if db.get_skill_by_slug(slug):
            logger.info("[post_process:skill] 跳过重复 slug: %s", slug)
            return None

        system_prompt = _llm_text(skill_dict.get("system_prompt"))
        # 质量门：system_prompt 至少 N 字
        if len(system_prompt) < config.skill_min_system_prompt_len:
            logger.info(
                "[post_process:skill] 跳过低质量技能（system_prompt 过短）: %s", slug,
            )
            return None

        description = _llm_text(skill_dict.get("description"))
        trigger_words = _clean_trigger_words(skill_dict.get("trigger_words", []))

        return db.create_skill(
            name=name,
            slug=slug,
            system_prompt=system_prompt,
            description=description,
            category=AUTO_SKILL_CATEGORY.get(mode, "其他"),
            source="auto",
            is_builtin=0,
            enabled=0,
            trigger_words=trigger_words,
        )
    except Exception:
        logger.exception("[post_process:skill] create_auto_skill failed")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────────────


async def post_process_after_turn(
    mode: str,
    conversation_id: str,
    user_message: str,
    assistant_content: str,
    assistant_reasoning: str = "",
    model_id: Optional[str] = None,
    turn_index: int = 0,
) -> None:
    """单轮对话完成后的统一后处理入口

    Args:
        turn_index: 当前会话第几轮（0-based），用于频率控制
    """
    config = get_post_process_config()
    if not config.enabled:
        return

    # model_id 未指定时从数据库取默认模型
    if model_id is None:
        try:
            default_cfg = await asyncio.to_thread(db.get_default_model)
            if default_cfg:
                model_id = default_cfg.get("model_id") or default_cfg.get("id", "Qwen3-VL")
            else:
                model_id = "Qwen3-VL"
        except Exception:
            model_id = "Qwen3-VL"

    # 构建 TurnContext
    ctx = TurnContext(
        mode=mode,
        conversation_id=conversation_id,
        user_message=user_message,
        assistant_content=assistant_content,
        assistant_reasoning=assistant_reasoning,
        model_id=model_id,
        turn_index=turn_index,
    )

    # 三任务并发执行，互不影响
    tasks = [
        _generate_title(ctx, config),
        _extract_memories(ctx, config),
        _generate_skill(ctx, config),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 记录失败（但不影响主流程）
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            task_names = ["title", "memory", "skill"]
            logger.exception(
                "[post_process:%s] 子任务异常",
                task_names[i] if i < len(task_names) else f"unknown_{i}",
                exc_info=result,
            )


# ──────────────────────────────────────────────────────────────────────────────
# 生命周期管理
# ──────────────────────────────────────────────────────────────────────────────


class PostProcessManager:
    """统一管理 post_process 任务生命周期

    用法：
        manager = PostProcessManager()
        await manager.submit(ctx)  # 提交任务
        await manager.shutdown()   # 应用关闭时调用
    """

    def __init__(self, config: Optional[PostProcessConfig] = None) -> None:
        self.config = config or get_post_process_config()
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def submit(
        self,
        mode: str,
        conversation_id: str,
        user_message: str,
        assistant_content: str,
        assistant_reasoning: str = "",
        model_id: Optional[str] = None,
        turn_index: int = 0,
    ) -> str:
        """提交后处理任务，返回 task_id"""
        # 每次提交取当前配置：self.config 是单例构造时的快照，而设置面板的 PUT 只
        # 换模块级 _config（set_post_process_config），不会重建这个常驻单例 ——
        # 用户关掉"后处理"后，enabled 判定仍按旧值放行任务。
        self.config = get_post_process_config()
        if not self.config.enabled:
            return ""

        import uuid
        task_id = uuid.uuid4().hex

        # 修复竞态条件：create_task + add_done_callback + 存储必须在同一个锁内完成
        # 否则任务可能在锁外完成，_discard 移除占位后 task 引用丢失，shutdown() 无法等待
        async with self._lock:
            # 占位 None 防止 callback 先触发时 _discard 找不到 key
            self._tasks[task_id] = None  # type: ignore[assignment]
            task = asyncio.create_task(self._run_with_timeout(
                mode, conversation_id, user_message,
                assistant_content, assistant_reasoning,
                model_id, turn_index,
            ))
            task.add_done_callback(lambda _: self._discard(task_id))
            self._tasks[task_id] = task
        return task_id

    def _discard(self, task_id: str) -> None:
        """从 _tasks 中移除已完成的任务"""
        # 同步回调，不能 await
        # 修复：使用 call_soon_threadsafe 确保线程安全（虽然 asyncio 是单线程的，
        # 但 add_done_callback 可能在其他线程的任务完成时被调用）
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(self._tasks.pop, task_id, None)
        except RuntimeError:
            # 无运行中的事件循环，直接操作
            self._tasks.pop(task_id, None)

    async def _run_with_timeout(
        self,
        mode: str,
        conversation_id: str,
        user_message: str,
        assistant_content: str,
        assistant_reasoning: str,
        model_id: Optional[str],
        turn_index: int,
    ) -> None:
        """带总超时的执行"""
        try:
            await asyncio.wait_for(
                post_process_after_turn(
                    mode=mode,
                    conversation_id=conversation_id,
                    user_message=user_message,
                    assistant_content=assistant_content,
                    assistant_reasoning=assistant_reasoning,
                    model_id=model_id,
                    turn_index=turn_index,
                ),
                timeout=self.config.total_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[post_process] 整体超时 %ss: conversation=%s",
                self.config.total_timeout, conversation_id,
            )
        except asyncio.CancelledError:
            logger.info("[post_process] 任务被取消: conversation=%s", conversation_id)
            raise
        except Exception:
            logger.exception("[post_process] 失败: conversation=%s", conversation_id)

    async def shutdown(self, timeout: float = 10.0) -> None:
        """应用关闭时调用 — 等待所有任务完成，超时后强制取消"""
        async with self._lock:
            pending = [t for t in self._tasks.values() if not t.done()]

        if not pending:
            return

        logger.info("[post_process] 等待 %d 个任务完成（最多 %ss）", len(pending), timeout)
        try:
            await asyncio.wait(pending, timeout=timeout)
        except Exception:
            pass

        # 超时后强制取消
        cancelled: list = []
        for task in pending:
            if not task.done():
                task.cancel()
                cancelled.append(task)
                logger.warning("[post_process] 强制取消任务")
        # 审查 MINOR 修复：等待取消真正完成，避免事件循环带着未清理完的任务关闭
        # （DB 写入可能处于半截状态）
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)


# 全局 Manager 实例（由 main.py 启动时创建）
_manager: Optional[PostProcessManager] = None


def get_post_process_manager() -> PostProcessManager:
    """获取全局 PostProcessManager 实例"""
    global _manager
    if _manager is None:
        _manager = PostProcessManager()
    return _manager


def init_post_process_manager(config: Optional[PostProcessConfig] = None) -> PostProcessManager:
    """初始化全局 PostProcessManager（由 main.py 调用）"""
    global _manager
    _manager = PostProcessManager(config)
    return _manager


def load_post_process_config_from_db() -> PostProcessConfig:
    """从数据库加载后处理配置，若无记录则返回默认配置"""
    from database import get_app_setting

    def _bool(key: str, default: bool) -> bool:
        v = get_app_setting(key)
        if v is None:
            return default
        return v.lower() == "true"

    return PostProcessConfig(
        enabled=_bool("post_process.enabled", True),
        generate_title=_bool("post_process.generate_title", True),
        extract_memory=_bool("post_process.extract_memory", True),
        generate_skill=_bool("post_process.generate_skill", True),
    )
