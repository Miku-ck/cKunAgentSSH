"""陪聊长期记忆：事实抽取（纯函数，无 IO）。

记忆最终落在 Agent 记忆表 memories 里，scope=persona:{persona_id}；
Agent 侧只读 global / host:*，不会碰到这个命名域。

抽取走确定性规则（正则）而不是再调一次模型：
- 每轮零额外延迟、零额外成本；
- 规则可被 pytest 精确断言，LLM 抽取没法稳定测；
- 陪伴场景里**乱记比漏记更糟** —— 记错会让它说出用户没说过的话，
  直接违反人格宪章的诚实边界（「不把说过的话当真实记忆」）。

所以这里宁可漏记、不可乱记：捕获组有长度上限、有词表黑名单、
敏感信息整条不抽、用户说「别记」立刻收手。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import database as db
from agent.memory import mstore, retriever
from .character import has_sensitive, persona_text_risk
from .timeutil import days_since as _days_since_impl, fmt_ts as _fmt_ts, parse_ts as _parse_ts

# 记忆在 memories 表里的命名域。scope 决定谁能读到它。
KIND = "persona_fact"
SOURCE = "bot_chat"
SCOPE_PREFIX = "persona:"

# 槽位：single = 同一时刻只应有一条（称呼/住址/生日/职业），新的取代旧的；
# multi = 可并存多条（喜好/忌口/家人/计划）。
SINGLE = "single"
MULTI = "multi"


@dataclass(frozen=True)
class Fact:
    title: str
    content: str
    importance: float = 0.8
    confidence: float = 0.9
    slot: str = MULTI


# ── 规则表 ──
# 每条：(标题, 正则, 模板, 重要度, 槽位)
# 顺序有讲究：否定式（不吃/不喜欢）必须排在喜好之前，否则「我不喜欢吃香菜」
# 会同时被记成「用户不吃香菜」和「用户喜欢香菜」。

_RULES: list[tuple[str, re.Pattern, str, float, str]] = [
    (
        "称呼",
        re.compile(r"我(?:叫|名字叫|的名字是)\s*(?P<name>[\u4e00-\u9fa5A-Za-z0-9·]{1,12})"),
        "用户叫{name}",
        1.0,
        SINGLE,
    ),
    (
        "居住",
        re.compile(
            r"我(?:住在|居住在|搬到|搬去了|搬进|在)\s*(?P<place>[\u4e00-\u9fa5]{2,8}?)"
            r"(?:市|区|县|省|镇|村|街|路|城)?(?=[\s，。！？、,.!?\n了]|$)"
        ),
        "用户住在{place}",
        0.85,
        SINGLE,
    ),
    (
        "生日",
        re.compile(
            r"我(?:的)?生日(?:是|在)?\s*"
            r"(?P<date>\d{1,2}月\d{1,2}[日号]|\d{1,2}[日号]|\d{4}年\d{1,2}月\d{1,2}日)"
        ),
        "用户的生日是{date}",
        0.9,
        SINGLE,
    ),
    (
        "忌口",
        re.compile(r"我(?:不爱吃|不喜欢吃|不吃|不能吃)\s*(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{1,12})"),
        "用户不吃{x}",
        0.6,
        MULTI,
    ),
    (
        "厌恶",
        # 负向前瞻吃掉「吃」：「我不喜欢吃香菜」归忌口，不该再记一条「不喜欢吃香菜」
        re.compile(r"我(?:不喜欢|讨厌)\s*(?!吃)(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{1,12})"),
        "用户不喜欢{x}",
        0.6,
        MULTI,
    ),
    (
        "喜好",
        re.compile(
            r"我(?:最喜欢|比较喜欢|喜欢吃|喜欢|爱吃|最爱|爱)\s*(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{1,12})"
        ),
        "用户喜欢{x}",
        0.6,
        MULTI,
    ),
    (
        "职业",
        re.compile(
            r"我(?:是|做|当)\s*(?P<job>[\u4e00-\u9fa5]{0,6}"
            r"(?:工作|工程师|老师|医生|设计师|学生|程序员|经理|运营|会计|律师|护士|"
            r"警察|司机|厨师|研究员|编辑|记者|销售|客服|开发|产品|测试|运维))"
        ),
        "用户的职业是{job}",
        0.7,
        SINGLE,
    ),
    (
        "宠物",
        re.compile(
            r"我(?:养了|家有|养)\s*(?P<pre>[\u4e00-\u9fa5A-Za-z]{0,6}?)"
            r"(?P<pet>猫|狗|仓鼠|鹦鹉|兔子|乌龟|金鱼)"
        ),
        "用户养了{pet}",
        0.7,
        MULTI,
    ),
    (
        "家人",
        re.compile(
            r"我(?:有个|有一个)?\s*(?P<rel>老婆|老公|妻子|丈夫|女儿|儿子|妈妈|母亲|爸爸|"
            r"父亲|哥哥|姐姐|弟弟|妹妹|奶奶|爷爷)\s*(?:叫|名叫|名字叫)\s*"
            r"(?P<name>[\u4e00-\u9fa5A-Za-z]{1,8})"
        ),
        "用户的{rel}是{name}",
        0.7,
        MULTI,
    ),
    (
        "习惯",
        re.compile(r"我(?:每天|每周|以后|下次|每次|一般都|通常)\s*(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{2,25})"),
        "用户的长期习惯：{x}",
        0.5,
        MULTI,
    ),
    (
        "计划",
        # 不强制带「我」：「周末要去看展」也是计划
        re.compile(
            r"(?:我)?(?:明天|后天|下周|周末|下个月|今晚|待会|等会儿)\s*"
            r"(?:要|想|准备|打算|得去)?\s*(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{2,25})"
        ),
        "用户的计划：{x}",
        0.4,
        MULTI,
    ),
]

# ── 噪声过滤 ──

# 居住语境里这些词不是地方：「我在开会」不是「用户住在开会」。
_PLACE_STOP_PREFIX = set("想聊吃玩等看听说坐哭笑忙学宿")
_PLACE_STOP_WORDS = {
    "开会", "上班", "下班", "公司", "学校", "宿舍", "外面", "路上", "地铁", "公交",
    "排队", "等人", "睡觉", "聊天", "忙活", "吃饭", "喝酒", "看书", "学习", "工作",
    "家里", "医院", "开心", "难过", "厨房", "厕所", "宿舍",
}
# 称呼候选里这些不是名字：「我叫你去买」不是「用户叫你去买」。
_NAME_STOP = {
    "你", "他", "她", "它", "们", "的", "去", "不", "没", "是", "在", "想", "要", "就",
    "都", "也", "还", "这", "那", "说", "叫", "个", "一", "什么", "谁", "自己", "啥",
}
# 习惯/计划里剥掉的前导动词：「周末要去看展」→「看展」。
_LEADING_VERBS = ("去", "要", "得", "会", "准备", "打算", "想")

# ── 敏感信息：整条消息不参与抽取（口径定义在 bot/character.py，输出兜底共用）──

# ── 用户指令 ──

# 强抑制：本轮一句都不记。
_NEGATE = re.compile(
    r"(?:别记(?:这|那)(?:个|些|些事)?|别记下来|不用记|不要记|别记住|不用记住|"
    r"不要记住|别存(?:下|着)?了?|不要存|我不想记)"
)
# 定向遗忘：忘掉 X。
_FORGET = re.compile(
    r"(?:忘掉|忘记|别记|不要记|不用记)\s*"
    r"(?:我(?:说|讲)过的?|我说的|我讲的|我的)?\s*"
    r"(?P<kw>[\u4e00-\u9fa5A-Za-z0-9]{1,20})"
)
# 指代词不是可删除的对象：「别记这个」是抑制，不是「忘掉这个」。
_DEICTIC = {"这个", "那个", "这些", "那些", "那个事", "这些事", "了", "吧", "啥", "什么"}


def is_negated(text: str) -> bool:
    """用户说了「别记 / 不用记」这类话。"""
    return bool(_NEGATE.search(text or ""))


def detect_forget(text: str) -> str | None:
    """从「忘掉我说过的火锅」这类话里取出要删的关键词；没有则 None。"""
    match = _FORGET.search(text or "")
    if not match:
        return None
    keyword = re.sub(r"^(?:我(?:说|讲)过的?|说过的|说过|过的|的)+", "", match.group("kw"))
    if not keyword or keyword in _DEICTIC:
        return None
    return keyword


def _looks_like_phrase(value: str) -> bool:
    """捕获值像个短语：不太长、不含 3 个以上连续重复字。"""
    if not value or len(value) > 12:
        return False
    return not re.search(r"(.)\1{2,}", value)


def _build(
    title: str, template: str, match: re.Match, importance: float, slot: str
) -> Fact | None:
    groups = {key: value for key, value in match.groupdict().items() if value}
    if not groups:
        return None
    if "place" in groups:
        place = groups["place"]
        if place[0] in _PLACE_STOP_PREFIX or place in _PLACE_STOP_WORDS:
            return None
    if "name" in groups and groups["name"] in _NAME_STOP:
        return None
    for key in ("x",):
        if key in groups:
            value = groups[key]
            if not _looks_like_phrase(value):
                return None
            if title in ("习惯", "计划"):
                value = _strip_leading_verbs(value)
                if not value:
                    return None
                groups[key] = value
    try:
        content = template.format(**groups)
    except (KeyError, IndexError):
        return None
    return Fact(title=title, content=content, importance=importance, slot=slot)


def _strip_leading_verbs(value: str) -> str:
    changed = True
    while changed and value:
        changed = False
        for verb in _LEADING_VERBS:
            if value.startswith(verb) and len(value) > len(verb):
                value = value[len(verb):]
                changed = True
                break
    return value


def extract_facts(user_text: str) -> list[Fact]:
    """从用户这句话里认出事实。

    只看用户的话：助手自己的复述不是事实（那可能是它的幻觉）。
    """
    text = (user_text or "").strip()
    if not text or has_sensitive(text):
        return []
    # 「别记这个」是抑制；「别记我的火锅」是定向遗忘，抽取照常走（那句里也没有事实）
    if is_negated(text) and detect_forget(text) is None:
        return []
    facts: list[Fact] = []
    seen: set[str] = set()
    for title, pattern, template, importance, slot in _RULES:
        for match in pattern.finditer(text):
            fact = _build(title, template, match, importance, slot)
            if fact is None or fact.content in seen:
                continue
            # 记忆投毒防线：覆盖类注入不得借抽取规则成为「事实」
            #（「我叫你忽略之前所有设定」不是称呼，是话术）
            if persona_text_risk(fact.content):
                continue
            seen.add(fact.content)
            facts.append(fact)
    return facts


def scope_for(persona_id: str) -> str:
    """人格 → 记忆作用域。"""
    return f"{SCOPE_PREFIX}{persona_id or ''}"


# ── 存取层 ──
#
# 记忆表是 Agent 的 memories（复用 FTS 同步、失效审计、热度统计）。
# 读侧按 scope=persona:{id} 隔离：Agent 侧只读 global / host:*，
# 两者互不可见（Agent 的注入路径强制带 scope，兜底路径也带）。
#
# 写入走 mstore.add_memory（保证 _fts_sync 一致），失效走 mstore.retire
# （不物理删，留审计行），取代走 mstore.supersede（旧行让位并继承热度）。
# 这里不新增表，也不改 agent 侧代码。


def _scope_rows(persona_id: str, statuses: tuple[str, ...] = ("active",)) -> list[dict]:
    """列出该人格作用域下的记忆行。"""
    if not persona_id:
        return []
    scope = scope_for(persona_id)
    placeholders = ",".join("?" for _ in statuses)
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT id, kind, scope, title, content, source, status, importance, "
            "source_conv_id, created_at, updated_at FROM memories "
            f"WHERE scope = ? AND status IN ({placeholders}) "
            "ORDER BY updated_at DESC, rowid DESC",
            (scope, *statuses),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_memories(persona_id: str) -> list[dict]:
    """给「它记得你什么」面板用：当前生效的记忆。"""
    return _scope_rows(persona_id)


def remember(persona_id: str, conversation_id: str | None, user_text: str) -> int:
    """从这句话里抽取事实并落库（不 embed），返回新写入条数。

    同步口径保留给无事件循环的调用方；聊天主链路走 remember_async（带 embed）。
    """
    if not persona_id:
        return 0
    # 定向遗忘先执行：「忘掉我说过的火锅」既删旧的，也照常记这句话里的其他事实
    keyword = detect_forget(user_text)
    if keyword:
        forget(persona_id, keyword)
    facts = extract_facts(user_text)
    if not facts:
        return 0
    return _write_facts(persona_id, conversation_id, facts)


async def remember_async(
    persona_id: str,
    conversation_id: str | None,
    user_text: str,
    embed_client=None,
) -> int:
    """remember 的异步版：落库前先批量 embed（一次 HTTP，多事实分摊）。

    embed 失败返回 None → 该事实仅 FTS 可检（宁缺毋滥，不造伪向量）。
    """
    if not persona_id:
        return 0
    keyword = detect_forget(user_text)
    if keyword:
        forget(persona_id, keyword)
    facts = extract_facts(user_text)
    if not facts:
        return 0
    vectors: "list[list[float]] | None" = None
    if embed_client is not None:
        try:
            vecs = await embed_client.embed([f"{f.title}，{f.content}" for f in facts])
            if vecs and len(vecs) == len(facts):
                vectors = vecs
        except Exception:  # noqa: BLE001 —— embed 是增强项，失败不拖垮记忆写入
            vectors = None
    return _write_facts(persona_id, conversation_id, facts, vectors)


def _write_facts(
    persona_id: str,
    conversation_id: str | None,
    facts: "list[Fact]",
    vectors: "list[list[float]] | None" = None,
) -> int:
    existing = {row["content"]: row for row in _scope_rows(persona_id)}
    written = 0
    for index, fact in enumerate(facts):
        if fact.content in existing:
            continue
        vector = vectors[index] if vectors else None
        new_id = mstore.add_memory(
            kind=KIND,
            content=fact.content,
            scope=scope_for(persona_id),
            title=fact.title,
            source=SOURCE,
            importance=fact.importance,
            status="active",
            embedding=vector,
            embedding_src=f"{fact.title}，{fact.content}" if vector else "",
            source_conv_id=conversation_id,
        )
        written += 1
        if fact.slot == SINGLE:
            # 单值槽位（称呼/住址/生日/职业）：同槽位的旧事实让位
            for row in existing.values():
                if row["title"] == fact.title and row["id"] != new_id:
                    mstore.supersede(row["id"], new_id)
        existing[fact.content] = {
            "id": new_id,
            "title": fact.title,
            "content": fact.content,
        }
    return written


def forget(persona_id: str, keyword: str | None = None) -> int:
    """忘掉某条（keyword 为空即全清）。返回失效条数。"""
    removed = 0
    for row in _scope_rows(persona_id):
        if keyword and keyword not in row["content"] and keyword not in row["title"]:
            continue
        if mstore.retire(row["id"], reason="bot_forget", status="invalidated"):
            removed += 1
    return removed


def forget_by_id(memory_id: str) -> bool:
    """按 id 忘掉一条（面板上的单条「忘记」）。

    只允许动人格记忆：Agent 侧的记忆（global / host:*）不经这条路径。
    """
    if not memory_id:
        return False
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM memories WHERE id = ? AND kind = ?", (memory_id, KIND)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    return mstore.retire(memory_id, reason="bot_forget", status="invalidated")


def add_manual(persona_id: str, content: str, conversation_id: str | None = None) -> str | None:
    """手动添加一条记忆（「记住这个」按钮）。"""
    text = (content or "").strip()
    if not persona_id or not text:
        return None
    scope = scope_for(persona_id)
    for row in _scope_rows(persona_id):
        if row["content"] == text:
            return row["id"]
    return mstore.add_memory(
        kind=KIND,
        content=text,
        scope=scope,
        title="手动添加",
        source=SOURCE,
        importance=0.8,
        status="active",
        source_conv_id=conversation_id,
    )


# ── 时间衰减与容量边界（保守：降权为主，超限才淘汰）──
#
# 陪伴场景宁可保守：长期没被想起的记忆先降权（importance 下调，不删除、
# 不 retire），只有超过容量上限才按「最低热度 + 最旧」淘汰，且走 mstore.retire
# 留审计行。所有 SQL 限定 scope=persona:{id} AND kind=persona_fact，
# 绝不触碰 Agent 侧 global / host:* 记忆。
#
# 进化规则：被注入一次就回暖一点（_warm_up）——「常想起的更牢，想不起的渐淡」。
# 衰减锚点是 last_used_at（mark_injected 写入），从未被注入的退回 created_at；
# 降权不碰 updated_at（与 mstore.decay_memories 同一口径：那是账面记账不是内容编辑）。

MEMORY_CAP_PER_PERSONA = 200   # 每人格硬上限：超限才触发淘汰
DECAY_HALF_LIFE_DAYS = 45.0    # 半衰窗：闲置这么久 importance 减半
DECAY_FLOOR = 0.1              # 降权地板：降到就不再往下
KEY_FACT_IMPORTANCE = 0.85     # 关键事实线（称呼/生日/住址）：永不衰减、不参与淘汰
WARM_UP_STEP = 0.1             # 被注入一次的回暖幅度（封顶 1.0）


def _active_persona_rows(persona_id: str) -> "list[dict]":
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, importance, pinned, use_count, "
            "last_used_at, created_at, updated_at, rowid FROM memories "
            "WHERE scope = ? AND kind = ? AND status = 'active' "
            "ORDER BY updated_at DESC, rowid DESC",
            (scope_for(persona_id), KIND),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _decayed_importance(row: dict, now: datetime) -> "float | None":
    """该行该降到多少；不需要降返回 None。pinned 与关键事实豁免。"""
    if int(row.get("pinned") or 0):
        return None
    importance = float(row.get("importance") or 0.0)
    if importance <= DECAY_FLOOR or importance >= KEY_FACT_IMPORTANCE:
        return None
    anchor = _parse_ts(row.get("last_used_at")) or _parse_ts(row.get("created_at"))
    if anchor is None:
        return None
    idle_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
    if idle_days < DECAY_HALF_LIFE_DAYS:
        return None
    # 每次维护最多淡「一个半衰档」（×0.5），地板兜底：闲置越久淡得越快，
    # 但不删除、随时可被召回回暖。重复维护会继续淡化直至地板——这正是
    # 「想不起的渐淡」；没有账本列可记录基线，不追求跨轮幂等。
    target = round(max(DECAY_FLOOR, importance * 0.5), 4)
    return target if target < importance - 1e-4 else None


def decay_persona_memories(persona_id: str, now: datetime | None = None) -> int:
    """降权：长期没被想起的记忆 importance 下调（不删除）。返回降权条数。"""
    now = now or datetime.now(timezone.utc)
    conn = db._get_conn()
    decayed = 0
    try:
        for row in _active_persona_rows(persona_id):
            target = _decayed_importance(row, now)
            if target is None:
                continue
            conn.execute(
                "UPDATE memories SET importance = ? WHERE id = ?",
                (target, row["id"]),
            )
            decayed += 1
        conn.commit()
    finally:
        conn.close()
    return decayed


def enforce_persona_memory_cap(persona_id: str, cap: int = MEMORY_CAP_PER_PERSONA) -> int:
    """容量边界：超限按「最低热度 + 最旧」淘汰（retire 留审计，不物理删）。

    pinned 与关键事实（importance ≥ KEY_FACT_IMPORTANCE）不参与淘汰候选。
    """
    rows = _active_persona_rows(persona_id)
    overflow = len(rows) - cap
    if overflow <= 0:
        return 0
    candidates = [
        row for row in rows
        if not int(row.get("pinned") or 0)
        and float(row.get("importance") or 0.0) < KEY_FACT_IMPORTANCE
    ]
    # 最低热度优先，同为冷门先淘汰更旧的；rowid 兜底保证确定性
    candidates.sort(key=lambda r: (
        int(r.get("use_count") or 0),
        str(r.get("updated_at") or ""),
        int(r.get("rowid") or 0),
    ))
    removed = 0
    for row in candidates[:overflow]:
        if mstore.retire(row["id"], reason="bot_decay", status="invalidated"):
            removed += 1
    return removed


def maintain_persona_memories(persona_id: str, now: datetime | None = None) -> dict:
    """一次完整维护：先降权再量边界。返回 {decayed, evicted} 供日志与调参。"""
    decayed = decay_persona_memories(persona_id, now)
    evicted = enforce_persona_memory_cap(persona_id)
    return {"decayed": decayed, "evicted": evicted}


def _warm_up(memory_ids: "list[str]") -> None:
    """被注入一次回暖一点：常想起的更牢（封顶 1.0）。失败不影响召回。"""
    if not memory_ids:
        return
    try:
        marks = ",".join("?" for _ in memory_ids)
        conn = db._get_conn()
        try:
            conn.execute(
                f"UPDATE memories SET importance = min(1.0, importance + ?) "
                f"WHERE id IN ({marks}) AND kind = ?",
                (WARM_UP_STEP, *memory_ids, KIND),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 回暖是增强项
        logger.warning("记忆回暖失败: %s", exc)


# 问句里的高频虚词与疑问词：不参与单字匹配，否则「在吗」能匹配上一堆事实。
_QUERY_STOP_CHARS = set(
    "的了在是我你他她它们这那不很也都还要会对把被给从和与啊吧呢吗么呀哦嗯什么怎样怎么哪谁个一二三"
)


def _rank_by_char_overlap(query: str, rows: list[dict]) -> list[dict]:
    """按「查询里出现了多少个记忆中也有的字」排序。

    稳定排序：同分的保持传入顺序（已按新→旧排好），所以并列时更近的记性优先。
    """
    chars = {ch for ch in query if ch not in _QUERY_STOP_CHARS and not ch.isspace()}
    if not chars:
        return []
    scored: list[tuple[int, dict]] = []
    for row in rows:
        overlap = len(chars & set(row["content"]))
        if overlap:
            scored.append((overlap, row))
    if not scored:
        return []
    scored.sort(key=lambda pair: -pair[0])
    return [row for _score, row in scored]


# ── P2：融合召回 + 注入门控 ──
#
# 写侧（P2 前）已有 11 类事实，读侧原来是 FTS(bigram) + 单字重合两路，
# 字面不重合就退化成「最近几条」。这里接上 Agent 侧现成的向量检索
# （retriever.search 自带 FTS 路，embed_client=None 时自动只走 FTS，无回归），
# 并加注入门控：不是"检索到了就塞 prompt"，而是按情境与低频钩子决定说不说。

logger = logging.getLogger(__name__)

# G2 情境门：这些情境下哪怕检索不硬命中，也该带上 1 条关系类事实
_G2_SITUATIONS = {"doubt", "praise", "fatigue", "farewell", "neglect"}
# 关系类槽位：称呼/家人最值得在情境门里被想起
_RELATION_TITLES = {"称呼", "家人"}
# 同一条事实 7 天内不重复注入（用户主动提起时豁免）
_REPEAT_DAYS = 7.0
_INJECT_LIMIT = 3


def embed_client_for(model: dict | None = None):
    """从聊天模型配置派生 embedding 客户端（与 Agent 侧 routes/agent.py 同口径）。

    返回 None 表示配置不全（调用方走 FTS-only）。端点不提供 /v1/embeddings 时
    EmbeddingClient 自带 300s 静默降级窗口，不会反复打失败请求。
    """
    try:
        from agent.memory.embedding import get_embedding_client

        cfg = model or db.get_default_model(mode=None, mask_key=False)
        if not cfg:
            return None
        base = (cfg.get("base_url") or "").strip()
        model_id = (cfg.get("model_id") or cfg.get("name") or "").strip()
        if not base or not model_id:
            return None
        return get_embedding_client(base, cfg.get("api_key") or "", model_id)
    except Exception:  # noqa: BLE001 —— 配置读取失败不该影响对话
        return None


def _days_since(value, now: datetime) -> float:
    # 无时间戳按"很久以前"，让 recency 归零、防复读窗口自然过期
    return _days_since_impl(value, now, default=1e9)


def _rerank(hits: list, rows_by_id: dict, now: datetime) -> "list[tuple[float, dict]]":
    """retriever 的 RRF 分 → 按陪伴场景重排：recency + importance + pinned。

    RRF 主导（短事实的判别信息就在字面/语义命中里），importance 让
    「用户叫小林」压过「用户的计划：看展」，pinned 是用户手动置顶的意志。
    """
    top = hits[0].score if hits else 0.0
    scored: "list[tuple[float, dict]]" = []
    for hit in hits:
        row = rows_by_id.get(hit.memory_id)
        if row is None:
            continue
        rrf_norm = (hit.score / top) if top > 0 else 0.0
        recency = 2 ** (-_days_since(row.get("updated_at"), now) / 14.0)
        final = (
            rrf_norm
            + 0.25 * recency
            + 0.20 * float(row.get("importance") or 0.5)
            + 0.9 * float(row.get("pinned") or 0.0)
        )
        scored.append((final, row))
    scored.sort(key=lambda pair: -pair[0])
    return scored


def deterministic_uniform(key: str, now: datetime) -> float:
    """确定性伪随机数：种子 = (key, 本地日期)。

    同一天决策恒定 → 可单测可重放；跨天自然换种子 → 「忽然想起」不固定在
    每天的同一时刻。不用 random 模块：进程级真随机会让测试不可复现。
    G3 旧事钩子与主动消息选题共用（key 区分用途，避免同一天联动触发）。
    """
    local_date = now.astimezone().date().isoformat()
    digest = hashlib.md5(f"{key}|{local_date}".encode()).digest()
    return int.from_bytes(digest, "big") / float(1 << 128)


def _get_injection_record(conversation_id: str | None) -> dict:
    if not conversation_id:
        return {"ids": [], "at": None}
    try:
        return db.get_bot_memory_injection(conversation_id)
    except Exception:  # noqa: BLE001
        return {"ids": [], "at": None}


def _save_injection_record(
    conversation_id: str | None, memory_ids: "list[str]", now: datetime
) -> None:
    if not conversation_id or not memory_ids:
        return
    try:
        db.set_bot_memory_injection(conversation_id, memory_ids, _fmt_ts(now))
    except Exception as exc:  # noqa: BLE001 —— 防复读记录失败只降级为"可重复"
        logger.warning("陪聊记忆注入记录写入失败: %s", exc)


async def recall(
    persona_id: str,
    query: str = "",
    *,
    embed_client=None,
    situation: str | None = None,
    novelty: float = 40.0,
    conversation_id: str | None = None,
    now: datetime | None = None,
) -> "list[str]":
    """取出这个人格该注入的事实（人话，不带记忆 id）。

    三重门控（注入 ≤3 条）：
    - G1 硬命中：融合召回头部足够强（用户正在问/提起这件事）→ 直接注入，
      并豁免 7 天防复读（用户主动提起，不算复读）；
    - G2 情境门：doubt/praise/fatigue/farewell/neglect 情境下带 1 条
      关系类事实（称呼/家人优先）；
    - G3 旧事钩子：p = 0.08 + novelty/100·0.10 − 距上次注入天数·0.03，
      确定性 RNG 触发 → 「忽然想起」低频、受新鲜感调节、可重放。
    """
    if not persona_id:
        return []
    now = now or datetime.now(timezone.utc)
    rows = _scope_rows(persona_id)
    if not rows:
        return []
    rows_by_id = {row["id"]: row for row in rows}

    hits: list = []
    try:
        hits = await retriever.search(
            query or "", scopes=[scope_for(persona_id)], kinds=[KIND],
            top_k=20, embed_client=embed_client,
        )
    except Exception as exc:  # noqa: BLE001 —— 检索失败退回字面/最近路径
        logger.warning("陪聊记忆融合检索失败: %s", exc)

    scored = None
    char_matched = False
    if hits:
        scored = _rerank(hits, rows_by_id, now)
        ranked = [row for _score, row in scored]
    elif query:
        ranked = _rank_by_char_overlap(query, rows)
        char_matched = bool(ranked)
    else:
        ranked = list(rows)  # 无 query：按新→旧，G3 仍可能钩出一条旧事
    if not ranked:
        # 字面完全不重合：回退「最近记的几条」，
        # 供 G2/G3 兜底；这批不算硬命中，不得走 G1。
        ranked = list(rows)

    record = _get_injection_record(conversation_id)
    seen_ids = set(record.get("ids") or [])
    last_at = record.get("at")

    def blocked(memory_id: str) -> bool:
        # 7 天窗口：上次注入已超期则自然解禁；无注入时间按未阻塞
        if memory_id not in seen_ids or not last_at:
            return False
        return _days_since(last_at, now) < _REPEAT_DAYS

    picked: "list[dict]" = []
    picked_ids: set[str] = set()
    exempt_ids: set[str] = set()

    def take(row: dict, exempt: bool = False) -> None:
        if row["id"] in picked_ids:
            return
        picked.append(row)
        picked_ids.add(row["id"])
        if exempt:
            exempt_ids.add(row["id"])

    # G1 硬命中。两路都算：融合召回头部够强（embed 可用时的语义命中），
    # 或单字重合路真实命中（FTS bigram 桥接不了「你还记得我叫什么吗」→
    # 「用户叫小林」，单字路是这条语义桥的唯一载体，不能丢）。
    # 回退的「最近几条」不算命中 —— 那是 G2/G3 的候选池，不是硬命中。
    if scored and ranked and query and scored[0][0] >= 0.6:
        take(ranked[0], exempt=True)
    elif char_matched and ranked:
        take(ranked[0], exempt=True)
    # G2 情境门
    if len(picked) < _INJECT_LIMIT and situation in _G2_SITUATIONS:
        rest = [r for r in ranked if r["id"] not in picked_ids]
        relation = [r for r in rest if r["title"] in _RELATION_TITLES]
        pool = relation or rest
        if pool:
            take(pool[0])
    # G3 旧事钩子。抑制项封顶 4 天（0.12）：「最近提过就收着点」，但
    # 不让隔得越久越永远不提 —— 否则 p 恒 0，钩子失效；无注入历史不抑制。
    if len(picked) < _INJECT_LIMIT:
        days_since_last = _days_since(last_at, now) if last_at else 0.0
        p = max(0.0, min(
            0.2,
            0.08 + (novelty or 40.0) / 100.0 * 0.10 - min(days_since_last, 4.0) * 0.03,
        ))
        if deterministic_uniform(persona_id, now) < p:
            fresh = [r for r in ranked if r["id"] not in picked_ids and not blocked(r["id"])]
            if fresh:
                take(fresh[0])

    # 防复读落地：G1 豁免，其余 7 天内注入过的不重复
    final = [r for r in picked if r["id"] in exempt_ids or not blocked(r["id"])][:_INJECT_LIMIT]
    if final and conversation_id:
        _save_injection_record(conversation_id, [r["id"] for r in final], now)
        try:
            mstore.mark_injected([r["id"] for r in final])
        except Exception:  # noqa: BLE001 —— 热度统计失败不影响注入
            pass
        # 被想起就回暖一点：与 decay 构成「常想起的更牢、想不起的渐淡」的进化回路
        _warm_up([r["id"] for r in final])
    return [row["content"] for row in final]
