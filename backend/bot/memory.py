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

import re
from dataclasses import dataclass

import database as db
from agent.memory import mstore

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
        0.9,
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
        0.85,
        MULTI,
    ),
    (
        "厌恶",
        # 负向前瞻吃掉「吃」：「我不喜欢吃香菜」归忌口，不该再记一条「不喜欢吃香菜」
        re.compile(r"我(?:不喜欢|讨厌)\s*(?!吃)(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{1,12})"),
        "用户不喜欢{x}",
        0.85,
        MULTI,
    ),
    (
        "喜好",
        re.compile(
            r"我(?:最喜欢|比较喜欢|喜欢吃|喜欢|爱吃|最爱|爱)\s*(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{1,12})"
        ),
        "用户喜欢{x}",
        0.8,
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
        0.8,
        SINGLE,
    ),
    (
        "宠物",
        re.compile(
            r"我(?:养了|家有|养)\s*(?P<pre>[\u4e00-\u9fa5A-Za-z]{0,6}?)"
            r"(?P<pet>猫|狗|仓鼠|鹦鹉|兔子|乌龟|金鱼)"
        ),
        "用户养了{pet}",
        0.8,
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
        0.85,
        MULTI,
    ),
    (
        "习惯",
        re.compile(r"我(?:每天|每周|以后|下次|每次|一般都|通常)\s*(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{2,25})"),
        "用户的长期习惯：{x}",
        0.8,
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
        0.8,
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

# ── 敏感信息：整条消息不参与抽取（不脱敏后抽，那等于存了一份残缺事实）──

_SENSITIVE = (
    re.compile(r"\d{17}[\dXx]"),                                  # 身份证
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),                        # 银行卡
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),                      # 手机号
    re.compile(
        r"(密码|口令|api[\s_-]?key|apikey|token|secret|私钥|密钥)\s*"
        r"(?:是|为|[:：=＝])?\s*[^\s，。！？]{3,}",
        re.IGNORECASE,
    ),
    re.compile(r"(门牌号|门牌|具体地址)\s*[:：]?\s*\d+"),
)

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


def has_sensitive(text: str) -> bool:
    """消息里含敏感信息时整条不抽。"""
    return any(pattern.search(text or "") for pattern in _SENSITIVE)


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
    """从这句话里抽取事实并落库，返回新写入条数。

    失败一律不让陪聊受影响：调用方负责兜底，这里只做纯业务。
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

    existing = {row["content"]: row for row in _scope_rows(persona_id)}
    written = 0
    for fact in facts:
        if fact.content in existing:
            continue
        new_id = mstore.add_memory(
            kind=KIND,
            content=fact.content,
            scope=scope_for(persona_id),
            title=fact.title,
            source=SOURCE,
            importance=fact.importance,
            status="active",
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


def clear_all(persona_id: str) -> int:
    """全部忘记。"""
    return forget(persona_id)


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


def recall_sync(persona_id: str, query: str = "", limit: int = 3) -> list[str]:
    """取出这个人格该注入的事实（人话，不带记忆 id）。

    两路召回：
    1. FTS —— 记忆表按中文 bigram 建索引，能命中「小林」这类字面重合；
    2. 单字重合度 —— bigram 桥接不了「你还记得我叫什么吗」→「用户叫小林」
       （一个问句与一条事实之间没有任何两字重合），所以再按单字重合排一次。

    两路都没命中才退回「最近记下的几条」：陪伴场景里「它确实记得」
    比相关性更重要（用户问的是别的事时，它仍该显得记得你）。
    """
    if not persona_id:
        return []
    rows = _scope_rows(persona_id)
    if not rows:
        return []
    if query:
        scope = scope_for(persona_id)
        hits = mstore.fts_hits(query, 20, [scope], [KIND], ("active",))
        if hits:
            order = {mid: rank for rank, (mid, _score) in enumerate(hits)}
            ranked = [row for row in rows if row["id"] in order]
            ranked.sort(key=lambda row: order[row["id"]])
            return [row["content"] for row in ranked[:limit]]
        ranked = _rank_by_char_overlap(query, rows)
        if ranked:
            return [row["content"] for row in ranked[:limit]]
    return [row["content"] for row in rows[:limit]]


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
