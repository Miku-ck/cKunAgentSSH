# 计划到期回溯 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让陪聊人格在用户说过的计划到期后，主动问一句「后来怎么样了」，依据全部来自用户亲口说过的话。

**Architecture:** 抽取层保留「计划」规则的时间词（`Fact.when` / `Fact.subject`），`bot/plan.py` 把时间词解析成本地时区窗口并落 `bot_plan_hooks` 表；`proactive.pick_topic` 多一个 `plan_due` 高优选题来源；`_deliver` 成功后把钩子置 `asked`（只问一次）。

**Tech Stack:** Python 3.11、SQLite（`database.py` 的 `_maybe_ddl` 建表脚本）、FastAPI 后端、pytest。

**Spec:** [docs/superpowers/specs/2026-10-31-plan-followup-design.md](../specs/2026-10-31-plan-followup-design.md)

## Global Constraints

- 时间戳落库一律走 `bot/timeutil.py` 的 `fmt_ts`（`"%Y-%m-%d %H:%M:%S"`，**UTC 口径**），读取走 `parse_ts`（返回 UTC aware datetime）。内部计算用 aware datetime，跨时区一律先 `astimezone(timezone.utc)` 再 `fmt_ts`。
- **窗口计算用本地时区**：`plan_window` 的 `base` 由调用方传 `datetime.now().astimezone()`，函数内部不改时区，只做日历运算。
- DDL 在 `database.py` 的 `_maybe_ddl("""…""")` 字符串内新增，同时 `_SCHEMA_VERSION` 5 → 6，并更新 `tests/test_startup_critical_path.py` 的 `_SCHEMA_SOURCE_FINGERPRINT`。
- 不引入任何第三方依赖。
- 每个改动的模块注释用 `spec 2026-10-31-plan-followup` 标签串联。
- 测试命令统一在 `backend/` 下执行：`python -m pytest tests/xxx.py -v`。
- Lint：`python -m ruff check --select F,E9,E7 <改动的文件>`。
- **提交纪律**：只 `git add` 本任务涉及的文件。工作区有 10 处与本设计无关的 WIP 改动（`chat_service.py`、`git-repos.ts` 等），一律不碰。

---

### Task 1: 时间窗口纯函数 `plan_window`

**Files:**
- Create: `backend/bot/plan.py`
- Test: `backend/tests/test_bot_plan.py`

**Interfaces:**
- Produces:
  - `PLAN_FOLLOWUP_H: float = 36.0`
  - `PLAN_NEAR_WINDOW_H: float = 4.0`
  - `plan_window(when_text: str, base: datetime) -> tuple[datetime, datetime] | None` —— 返回左闭右开的本地时区窗口，未知时间词返回 `None`

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_bot_plan.py`：

```python
"""计划时间窗：把「周末要去看展」里的时间词解析成可判定的窗口。

纯函数 + 固定基准时刻，全部可重放。
"""
from datetime import datetime, timedelta, timezone

from bot import plan as plan_mod

# 本地时区用固定 +08:00 模拟，避免测试机时区差异
CST = timezone(timedelta(hours=8))
# 2026-10-28 是周三
WED = datetime(2026, 10, 28, 20, 0, tzinfo=CST)


def _window(text, base=WED):
    return plan_mod.plan_window(text, base)


def test_tomorrow_is_the_whole_next_day():
    start, end = _window("明天")
    assert start == datetime(2026, 10, 29, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 10, 30, 0, 0, tzinfo=CST)


def test_day_after_tomorrow():
    start, end = _window("后天")
    assert start == datetime(2026, 10, 30, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 10, 31, 0, 0, tzinfo=CST)


def test_tonight_starts_at_18_and_ends_at_midnight():
    start, end = _window("今晚")
    assert start == datetime(2026, 10, 28, 18, 0, tzinfo=CST)
    assert end == datetime(2026, 10, 29, 0, 0, tzinfo=CST)


def test_later_is_a_four_hour_window_from_now():
    start, end = _window("待会")
    assert start == WED
    assert end == WED + timedelta(hours=4)


def test_weekend_from_a_weekday_is_saturday_to_monday():
    start, end = _window("周末")
    assert start == datetime(2026, 10, 31, 0, 0, tzinfo=CST)   # 周六
    assert end == datetime(2026, 11, 2, 0, 0, tzinfo=CST)     # 周一 00:00


def test_weekend_on_saturday_still_covers_sunday():
    start, end = _window("周末", datetime(2026, 10, 31, 9, 0, tzinfo=CST))
    assert start == datetime(2026, 10, 31, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 11, 2, 0, 0, tzinfo=CST)


def test_weekend_on_sunday_collapses_to_today():
    """周日说「周末」，事情还在进行中，窗口只剩今天 —— 不能提前判成结束。"""
    start, end = _window("周末", datetime(2026, 11, 1, 15, 0, tzinfo=CST))
    assert start == datetime(2026, 11, 1, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 11, 2, 0, 0, tzinfo=CST)


def test_next_week_is_a_full_seven_days():
    start, end = _window("下周")
    assert start == datetime(2026, 11, 2, 0, 0, tzinfo=CST)
    assert end == datetime(2026, 11, 9, 0, 0, tzinfo=CST)


def test_next_month_crosses_the_year_boundary():
    start, end = _window("下个月", datetime(2026, 12, 15, 10, 0, tzinfo=CST))
    assert start == datetime(2027, 1, 1, 0, 0, tzinfo=CST)
    assert end == datetime(2027, 2, 1, 0, 0, tzinfo=CST)


def test_unknown_or_empty_when_returns_none():
    assert _window("") is None
    assert _window("   ") is None
    assert _window("明年") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_bot_plan.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'bot.plan'`

- [ ] **Step 3: 写最小实现**

新建 `backend/bot/plan.py`：

```python
"""计划钩子：把「周末要去看展」落成可判定的到期窗口。

spec 2026-10-31-plan-followup

纯函数区（`plan_window`）不碰 IO：给定时间词与基准时刻，返回左闭右开的本地
时区窗口。IO 区（`record_hooks`）只做一次索引查询 + 一次写库，异常一律吞掉 ——
钩子是增强项，不能拖垮记忆写入与聊天主链路。

时区口径：窗口按 `base` 自带的时区做日历运算，落库前统一转 UTC（`fmt_ts`）。
解析不出的时间词一律不建钩子：宁可不问，不能问错。
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 窗口结束后多久之内还可以问
PLAN_FOLLOWUP_H = 36.0
# 「待会 / 等会儿」的窗口长度
PLAN_NEAR_WINDOW_H = 4.0


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def plan_window(when_text: str, base: datetime) -> "tuple[datetime, datetime] | None":
    """时间词 → 本地时区窗口 [start, end)。未知时间词返回 None。

    base 必须 aware：窗口用它的时区做日历运算，不做任何时区换算。
    """
    text = (when_text or "").strip()
    if not text:
        return None
    today = _day_start(base)

    if text in ("待会", "等会儿"):
        return base, base + timedelta(hours=PLAN_NEAR_WINDOW_H)
    if text == "今晚":
        return today.replace(hour=18), today + timedelta(days=1)
    if text == "明天":
        return today + timedelta(days=1), today + timedelta(days=2)
    if text == "后天":
        return today + timedelta(days=2), today + timedelta(days=3)
    if text == "周末":
        # 周一=0 … 周六=5、周日=6。周末中说「周末」指当下这个周末。
        weekday = base.weekday()
        if weekday >= 5:
            end = today + timedelta(days=2 if weekday == 5 else 1)
            return today, end
        saturday = today + timedelta(days=5 - weekday)
        return saturday, saturday + timedelta(days=2)
    if text == "下周":
        monday = today - timedelta(days=base.weekday()) + timedelta(days=7)
        return monday, monday + timedelta(days=7)
    if text == "下个月":
        year, month = base.year, base.month + 1
        if month > 12:
            year, month = year + 1, 1
        start = datetime(year, month, 1, tzinfo=base.tzinfo)
        return start, start + timedelta(days=calendar.monthrange(year, month)[1])
    return None
```

> 本步**不 import `timezone`**：`plan_window` 只做日历运算，Task 4 的 `record_hooks` 落库时才用到它（届时再补导入，否则 F401）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_bot_plan.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
cd d:/workspace/cKunAgentSSH
git add backend/bot/plan.py backend/tests/test_bot_plan.py
git commit -m "feat(bot): 计划时间词解析成到期窗口（spec 2026-10-31-plan-followup）"
```

---

### Task 2: 抽取层保留时间词与主题

**Files:**
- Modify: `backend/bot/memory.py:40-46`（`Fact` 定义）
- Modify: `backend/bot/memory.py:145-155`（「计划」规则）
- Modify: `backend/bot/memory.py:217-243`（`_build`）
- Test: `backend/tests/test_bot_memory.py`

**Interfaces:**
- Produces: `Fact.when: str`、`Fact.subject: str`（其余规则为空串）；`extract_facts("周末要去看展")` 返回 `Fact(title="计划", content="用户的计划：看展", when="周末", subject="看展")`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_bot_memory.py` 末尾追加：

```python
def test_plan_fact_keeps_the_time_word_and_subject():
    """计划的时间词是钩子的唯一依据，抽取时不能丢。"""
    facts = mem.extract_facts("周末要去看展")
    plan_facts = [f for f in facts if f.title == "计划"]
    assert len(plan_facts) == 1
    fact = plan_facts[0]
    assert fact.content == "用户的计划：看展", "正文保持原样，时间词不进记忆"
    assert fact.when == "周末"
    assert fact.subject == "看展"


def test_plan_subject_strips_leading_verbs():
    facts = mem.extract_facts("明天准备去交房租")
    fact = [f for f in facts if f.title == "计划"][0]
    assert fact.subject == "交房租"


def test_non_plan_facts_have_no_when_or_subject():
    facts = mem.extract_facts("我叫小林")
    fact = [f for f in facts if f.title == "称呼"][0]
    assert fact.when == ""
    assert fact.subject == ""
```

> 若 `test_bot_memory.py` 顶部未导入 `from bot import memory as mem`，先补这一行（检查现有 import 别名后再写，保持与文件内既有别名一致）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_bot_memory.py -k "plan_fact_keeps or plan_subject or non_plan_facts" -v`
Expected: FAIL —— `AttributeError: 'Fact' object has no attribute 'when'`

- [ ] **Step 3: 实现**

`Fact` 增加两个字段：

```python
@dataclass(frozen=True)
class Fact:
    title: str
    content: str
    importance: float = 0.8
    confidence: float = 0.9
    slot: str = MULTI
    when: str = ""      # 计划的时间词（「周末」）；其余规则为空
    subject: str = ""   # 计划正文（「看展」）；其余规则为空
```

「计划」规则把时间词变成命名组：

```python
    (
        "计划",
        # 不强制带「我」：「周末要去看展」也是计划。
        # 时间词单独成组：钩子要靠它算到期窗口，content 里不带。
        re.compile(
            r"(?:我)?(?P<when>明天|后天|下周|周末|下个月|今晚|待会|等会儿)\s*"
            r"(?:要|想|准备|打算|得去)?\s*(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{2,25})"
        ),
        "用户的计划：{x}",
        0.4,
        MULTI,
    ),
```

`_build` 收尾处带上两个字段：

```python
    try:
        content = template.format(**groups)
    except (KeyError, IndexError):
        return None
    # 计划的正文单独留一份：钩子要独立于记忆存活，不能靠切 content 前缀取
    subject = groups.get("x", "") if title == "计划" else ""
    return Fact(
        title=title,
        content=content,
        importance=importance,
        slot=slot,
        when=groups.get("when", ""),
        subject=subject,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_bot_memory.py -v`
Expected: 全部 passed（含原有用例，确认没有回归）

- [ ] **Step 5: 提交**

```bash
cd d:/workspace/cKunAgentSSH
git add backend/bot/memory.py backend/tests/test_bot_memory.py
git commit -m "feat(bot): 计划抽取保留时间词与主题（spec 2026-10-31-plan-followup）"
```

---

### Task 3: 钩子表、数据访问与结构版本

**Files:**
- Modify: `backend/database.py:98`（`_SCHEMA_VERSION` 5 → 6）
- Modify: `backend/database.py`（`_maybe_ddl` 内 `bot_proactive_log` 之后追加建表）
- Modify: `backend/database.py`（`add_bot_proactive_log` 附近追加 4 个函数）
- Modify: `backend/tests/test_startup_critical_path.py:33`（指纹常量）
- Test: `backend/tests/test_bot_plan.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `db.add_plan_hook(persona_id, conversation_id, memory_id, plan_text, when_text, due_start, due_end, created_at) -> str`
  - `db.due_plan_hook(persona_id, now_iso, earliest_iso) -> Optional[dict]`
  - `db.mark_plan_asked(hook_id, asked_at) -> bool`
  - `db.expire_plan_hooks(cutoff_iso) -> int`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_bot_plan.py` 追加：

```python
import database as db


def _persona():
    return db.create_bot_persona(name="晚风")


def test_add_plan_hook_supersedes_the_same_text(base_db):
    """同一件事改了时间：旧钩子让位，新的才是对的。"""
    persona = _persona()
    first = db.add_plan_hook(
        persona["id"], None, None, "看展", "周末",
        "2026-10-31 00:00:00", "2026-11-02 00:00:00", "2026-10-28 12:00:00",
    )
    second = db.add_plan_hook(
        persona["id"], None, None, "看展", "下周",
        "2026-11-02 00:00:00", "2026-11-09 00:00:00", "2026-10-30 12:00:00",
    )
    assert first != second
    row = db.due_plan_hook(persona["id"], "2026-11-10 00:00:00", "2026-11-01 00:00:00")
    assert row is not None and row["id"] == second


def test_due_plan_hook_respects_window_and_followup_horizon(base_db):
    persona = _persona()
    db.add_plan_hook(
        persona["id"], None, None, "看展", "周末",
        "2026-10-31 00:00:00", "2026-11-02 00:00:00", "2026-10-28 12:00:00",
    )
    # 窗口还没结束
    assert db.due_plan_hook(persona["id"], "2026-11-01 23:00:00", "2026-10-31 00:00:00") is None
    # 窗口结束后 12h：可回溯
    assert db.due_plan_hook(persona["id"], "2026-11-02 12:00:00", "2026-10-31 00:00:00")
    # 超出回溯期（36h 前的边界之外）
    assert db.due_plan_hook(persona["id"], "2026-11-04 12:00:00", "2026-11-03 00:00:00") is None


def test_mark_plan_asked_is_one_way_and_one_shot(base_db):
    persona = _persona()
    hook = db.add_plan_hook(
        persona["id"], None, None, "看展", "周末",
        "2026-10-31 00:00:00", "2026-11-02 00:00:00", "2026-10-28 12:00:00",
    )
    assert db.mark_plan_asked(hook, "2026-11-02 01:00:00") is True
    assert db.mark_plan_asked(hook, "2026-11-02 02:00:00") is False, "只问一次"
    assert db.due_plan_hook(persona["id"], "2026-11-02 01:00:00", "2026-10-31 00:00:00") is None


def test_expire_plan_hooks_only_touches_overdue_pending(base_db):
    persona = _persona()
    stale = db.add_plan_hook(
        persona["id"], None, None, "看展", "今晚",
        "2026-10-28 18:00:00", "2026-10-29 00:00:00", "2026-10-28 12:00:00",
    )
    fresh = db.add_plan_hook(
        persona["id"], None, None, "交房租", "下个月",
        "2026-11-01 00:00:00", "2026-12-01 00:00:00", "2026-10-28 12:00:00",
    )
    assert db.expire_plan_hooks("2026-10-30 00:00:00") == 1
    assert db.due_plan_hook(persona["id"], "2026-10-30 01:00:00", "2026-10-29 00:00:00") is None
    assert db.due_plan_hook(persona["id"], "2026-12-02 00:00:00", "2026-11-30 00:00:00")["id"] == fresh
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_bot_plan.py -k "plan_hook or expire" -v`
Expected: FAIL —— `AttributeError: module 'database' has no attribute 'add_plan_hook'`

- [ ] **Step 3: 改 DDL 与结构版本**

`database.py` 第 98 行：

```python
_SCHEMA_VERSION = 6
```

在 `_maybe_ddl` 字符串内、`bot_proactive_log` 的索引之后追加：

```sql
            -- 计划钩子：用户说过的「周末要去看展」，到期后主动问一句。
            -- 独立于记忆行存活：计划记忆 importance 只有 0.4，是维护首批淘汰对象。
            CREATE TABLE IF NOT EXISTS bot_plan_hooks (
                id              TEXT PRIMARY KEY,
                persona_id      TEXT NOT NULL,
                conversation_id TEXT,
                memory_id       TEXT,
                plan_text       TEXT NOT NULL,
                when_text       TEXT NOT NULL,
                due_start       TEXT NOT NULL,
                due_end         TEXT NOT NULL,
                state           TEXT NOT NULL DEFAULT 'pending',
                asked_at        TEXT,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_bot_plan_due
                ON bot_plan_hooks(state, due_end);
            CREATE INDEX IF NOT EXISTS ix_bot_plan_persona
                ON bot_plan_hooks(persona_id, state, plan_text);
```

- [ ] **Step 4: 加数据访问函数**

在 `database.py` 的 `add_bot_proactive_log` 之前插入：

```python
# ── 计划钩子（spec 2026-10-31-plan-followup）──
#
# 状态四态：pending → asked（问出去了）/ superseded（同一件事改了时间）/ expired
# （超窗没来得及问）。时间戳一律 UTC 字符串，可按字典序比较。


def add_plan_hook(
    persona_id: str,
    conversation_id: Optional[str],
    memory_id: Optional[str],
    plan_text: str,
    when_text: str,
    due_start: str,
    due_end: str,
    created_at: str,
) -> str:
    """写一条计划钩子；同人格同文本的 pending 先作废 —— 新时间才是对的。

    作废与插入放在同一个事务里：两次 commit 之间断掉会丢一条已经改期的计划。
    """
    hook_id = _new_id()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE bot_plan_hooks SET state = 'superseded' "
            "WHERE persona_id = ? AND plan_text = ? AND state = 'pending'",
            (persona_id, plan_text),
        )
        conn.execute(
            "INSERT INTO bot_plan_hooks (id, persona_id, conversation_id, memory_id, "
            "plan_text, when_text, due_start, due_end, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,'pending',?)",
            (
                hook_id, persona_id, conversation_id, memory_id, plan_text,
                when_text, due_start, due_end, created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return hook_id


def due_plan_hook(persona_id: str, now_iso: str, earliest_iso: str) -> Optional[dict]:
    """可回溯的钩子：窗口已结束（due_end <= now）且没超过回溯期，最早到期优先。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bot_plan_hooks "
            "WHERE persona_id = ? AND state = 'pending' "
            "AND due_end <= ? AND due_end >= ? "
            "ORDER BY due_end ASC LIMIT 1",
            (persona_id, now_iso, earliest_iso),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_plan_asked(hook_id: str, asked_at: str) -> bool:
    """把钩子标记为已问。只允许 pending → asked，重复调用返回 False。"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE bot_plan_hooks SET state = 'asked', asked_at = ? "
            "WHERE id = ? AND state = 'pending'",
            (asked_at, hook_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def expire_plan_hooks(cutoff_iso: str) -> int:
    """超窗没问出去的 pending 置 expired（保留审计行，不物理删）。"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE bot_plan_hooks SET state = 'expired' "
            "WHERE state = 'pending' AND due_end < ?",
            (cutoff_iso,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
```

- [ ] **Step 5: 更新结构指纹**

Run: `cd backend && python -m pytest tests/test_startup_critical_path.py::test_schema_version_bumped_with_ddl_changes -v`
Expected: FAIL，断言消息里给出实测指纹，形如 `_SCHEMA_SOURCE_FINGERPRINT = 'xxxxxxxxxxxxxxxx'`。把该 16 位值填进 `tests/test_startup_critical_path.py` 第 33 行。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_bot_plan.py tests/test_startup_critical_path.py -v`
Expected: 全部 passed

- [ ] **Step 7: 提交**

```bash
cd d:/workspace/cKunAgentSSH
git add backend/database.py backend/tests/test_bot_plan.py backend/tests/test_startup_critical_path.py
git commit -m "feat(bot): 计划钩子表与结构版本 6（spec 2026-10-31-plan-followup）"
```

---

### Task 4: 钩子写入并接入记忆链路

**Files:**
- Modify: `backend/bot/plan.py`（追加 `record_hooks`）
- Modify: `backend/bot/memory.py:343-369`（`remember_async`）
- Test: `backend/tests/test_bot_plan.py`

**Interfaces:**
- Consumes: `mem.Fact.when` / `Fact.subject`（Task 2）、`db.add_plan_hook`（Task 3）
- Produces: `plan.record_hooks(persona_id, conversation_id, facts, base) -> int`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_bot_plan.py` 追加：

```python
from bot import memory as mem


def test_remember_async_creates_a_hook_even_when_the_memory_was_deduped(base_db):
    """同一件事换个时间说：记忆被 content 去重跳过，钩子必须更新。"""
    persona = _persona()

    async def _run():
        await mem.remember_async(persona["id"], None, "周末要去看展")
        await mem.remember_async(persona["id"], None, "下周要去看展")

    asyncio.run(_run())

    rows = db._get_conn().execute(
        "SELECT * FROM bot_plan_hooks WHERE persona_id = ? ORDER BY rowid",
        (persona["id"],),
    ).fetchall()
    states = [dict(row)["state"] for row in rows]
    assert states == ["superseded", "pending"], "旧时间失效，新时间生效"
    assert dict(rows[-1])["when_text"] == "下周"


def test_record_hooks_skips_unknown_when_without_raising(base_db):
    persona = _persona()
    fact = mem.Fact(
        title="计划", content="用户的计划：看展", when="明年", subject="看展"
    )
    assert plan_mod.record_hooks(
        persona["id"], None, [fact], datetime(2026, 10, 28, 20, 0, tzinfo=CST)
    ) == 0


def test_record_hooks_stores_utc_timestamps(base_db):
    """落库一律 UTC：本地 20:00 (+08:00) 就是 12:00Z。"""
    persona = _persona()
    fact = mem.Fact(
        title="计划", content="用户的计划：看展", when="明天", subject="看展"
    )
    plan_mod.record_hooks(
        persona["id"], None, [fact], datetime(2026, 10, 28, 20, 0, tzinfo=CST)
    )
    row = db._get_conn().execute(
        "SELECT due_start, due_end FROM bot_plan_hooks WHERE persona_id = ?",
        (persona["id"],),
    ).fetchone()
    assert row["due_start"] == "2026-10-28 16:00:00"  # 本地 10-29 00:00
    assert row["due_end"] == "2026-10-29 16:00:00"
```

> 文件顶部需要 `import asyncio`。`remember_async` 内部用真实 `datetime.now().astimezone()`，所以断言只看状态与 `when_text`，不看具体时刻；查询用 `ORDER BY rowid`（同一秒内两次写入，`created_at` 排序不稳定）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_bot_plan.py -k "record_hooks or deduped" -v`
Expected: FAIL —— `AttributeError: module 'bot.plan' has no attribute 'record_hooks'`

- [ ] **Step 3: 实现 `record_hooks`**

`backend/bot/plan.py` 顶部补 `from datetime import timezone`，文件末尾追加：

```python
def record_hooks(
    persona_id: str,
    conversation_id: "str | None",
    facts,
    base: datetime,
) -> int:
    """把抽取到的计划落成钩子。返回新建条数。

    只认 `title == "计划"` 且时间词、主题都在的 Fact。任何异常只记 warning：
    钩子是增强项，不能拖垮记忆写入与聊天主链路。
    """
    if not persona_id or not facts:
        return 0
    created_at = _fmt_ts(base.astimezone(timezone.utc))
    written = 0
    for fact in facts:
        if fact.title != "计划" or not fact.when or not fact.subject:
            continue
        try:
            window = plan_window(fact.when, base)
            if window is None:
                logger.debug("计划时间词无法解析，不建钩子: %r", fact.when)
                continue
            start, end = window
            db.add_plan_hook(
                persona_id,
                conversation_id,
                None,
                fact.subject,
                fact.when,
                _fmt_ts(start.astimezone(timezone.utc)),
                _fmt_ts(end.astimezone(timezone.utc)),
                created_at,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 —— 钩子失败不阻断记忆写入
            logger.warning("计划钩子写入失败: %s", exc)
    return written
```

`plan.py` 顶部补三行（Task 1 故意没引入，否则 F401）：

```python
import database as db
from datetime import timezone
from .timeutil import fmt_ts as _fmt_ts
```

合并后的导入区：

```python
import calendar
import logging
from datetime import datetime, timedelta, timezone

import database as db

from .timeutil import fmt_ts as _fmt_ts

logger = logging.getLogger(__name__)
```

- [ ] **Step 4: 接入 `remember_async`**

`backend/bot/memory.py` 顶部导入区加：

```python
from . import plan as plan_mod
```

`remember_async` 中 `facts = extract_facts(user_text)` 之后、`if not facts` 之前插入：

```python
    # 钩子与记忆去重解耦：同一件事换个时间说，记忆会被 content 去重跳过，
    # 但到期窗口必须更新（spec 2026-10-31-plan-followup §4）。
    plan_mod.record_hooks(
        persona_id, conversation_id, facts, datetime.now().astimezone()
    )
```

`memory.py` 已有 `from datetime import datetime, timezone`（第 22 行），无需新增导入。

> `remember()`（同步版）不接钩子：它只给无事件循环的旧调用方用，主链路走 `remember_async`。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_bot_plan.py tests/test_bot_memory.py -v`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
cd d:/workspace/cKunAgentSSH
git add backend/bot/plan.py backend/bot/memory.py backend/tests/test_bot_plan.py
git commit -m "feat(bot): 计划钩子随记忆抽取落库（spec 2026-10-31-plan-followup）"
```

---

### Task 5: 选题器新增 `plan_due` 来源

**Files:**
- Modify: `backend/bot/proactive.py:356-380`（`pick_topic`）
- Test: `backend/tests/test_bot_proactive.py`

**Interfaces:**
- Consumes: `db.due_plan_hook`（Task 3）、`plan.PLAN_FOLLOWUP_H`（Task 1）
- Produces:
  - `Topic(NamedTuple)`: `describe: str`、`source: str`、`plan_id: str = ""`
  - `plan_due_topic(persona_id: str, now: datetime) -> Topic | None`
  - `pick_topic(...) -> Topic | None`（返回值由 `tuple` 变 `Topic`，下标访问与解包保持兼容）

- [ ] **Step 1: 先修既有断言，再写新测试**

`pick_topic` 的返回值从二元组升级成 `Topic(NamedTuple)`，而 NamedTuple 按值比较带长度：
`Topic("a", "b", "") == ("a", "b")` 是 **False**。所以先改这一条既有用例
（`backend/tests/test_bot_proactive.py` 约 797 行）：

```python
# 改前
    assert topic == ("他上次聊到很累，问问他现在缓过来没有", "fatigue_care")
# 改后
    assert topic == pro.Topic("他上次聊到很累，问问他现在缓过来没有", "fatigue_care")
```

其余用例（`topic[0]` / `topic[1]` / `first == second`）不需改动。

然后在 `backend/tests/test_bot_proactive.py` 追加：

```python
# ── 选题器：计划回溯（spec 2026-10-31-plan-followup）──

def _add_due_hook(persona_id: str, plan_text: str = "看展") -> str:
    return db.add_plan_hook(
        persona_id, None, None, plan_text, "周末",
        "2026-10-31 00:00:00", "2026-11-02 00:00:00", "2026-10-28 12:00:00",
    )


NOW_PLAN = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_pick_topic_uses_a_due_plan(base_db):
    persona, _conv = _persona_conv()
    hook_id = _add_due_hook(persona["id"])

    topic = pro.pick_topic(persona["id"], {}, now=NOW_PLAN)

    assert topic is not None
    assert topic.source == "plan_due"
    assert topic.plan_id == hook_id
    assert "看展" in topic.describe
    assert topic[0] == topic.describe, "下标访问必须继续可用"


def test_fatigue_care_outranks_a_due_plan(base_db):
    persona, _conv = _persona_conv()
    _add_due_hook(persona["id"])

    topic = pro.pick_topic(persona["id"], {"last_event_kind": "fatigue"}, now=NOW_PLAN)

    assert topic.source == "fatigue_care"


> **所有 `pick_topic` 用例必须传 `now=NOW_PLAN`**：钩子窗口是固定日期（10-31~11-02），用真实时钟跑会因未到期而拿不到 `plan_due`。`mem.add_manual` 写入的 `updated_at` 是真实时间，距 `NOW_PLAN` 不足 3 天，仍属 fresh 窗口——所以两条来源同时存在，优先级比对才有意义。

```python
def test_plan_due_outranks_fresh_fact(base_db):
    persona, _conv = _persona_conv()
    _add_due_hook(persona["id"])
    mem.add_manual(persona["id"], "用户喜欢喝美式")

    topic = pro.pick_topic(persona["id"], {}, now=NOW_PLAN)

    assert topic.source == "plan_due"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_bot_proactive.py -k "due_plan or plan_due or fatigue_care_outranks" -v`
Expected: FAIL —— `AssertionError` 或 `'str' object has no attribute 'source'`

- [ ] **Step 3: 实现**

`backend/bot/proactive.py` 顶部导入区加：

```python
from typing import NamedTuple

from bot import plan as plan_mod
from bot.plan import PLAN_FOLLOWUP_H
```

在「选题器」小节开头定义：

```python
class Topic(NamedTuple):
    """选题：给 _compose 的描述 + 来源标记 + 计划钩子 id（仅 plan_due 非空）。"""

    describe: str
    source: str
    plan_id: str = ""


def plan_due_topic(persona_id: str, now: datetime) -> "Topic | None":
    """到期可回溯的计划选题：窗口结束后 36h 内，最早到期的那件事先问。

    文案不出现时间词、不出现具体日期：相对时间在几天后才说出口是错乱的。
    """
    row = db.due_plan_hook(
        persona_id,
        _fmt_ts(now),
        _fmt_ts(now - timedelta(hours=PLAN_FOLLOWUP_H)),
    )
    if not row:
        return None
    return Topic(
        f"他之前提过：{row['plan_text']}，问问后来怎么样了",
        "plan_due",
        row["id"],
    )
```

`pick_topic` 改为：

```python
def pick_topic(
    persona_id: str, state: dict, *, now: "datetime | None" = None
) -> "Topic | None":
    """主动消息的选题器：同一输入永远同一选题，可重放可单测。

    优先级即产品判断：
    1. fatigue_care —— 上一轮停在他喊累：人还没缓过来就别聊别的；
    2. plan_due —— 他说过的事到期了（窗口结束后 36h 内）：最具体的开口依据；
    3. fresh_fact —— 近 3 天新记的事实；
    4. old_memory —— 旧事钩子，确定性 RNG 低频翻一件，演出「忽然想起」。
    """
    now = now or datetime.now(timezone.utc)
    if (state.get("last_event_kind") or "") == "fatigue":
        return Topic("他上次聊到很累，问问他现在缓过来没有", "fatigue_care")
    due = plan_due_topic(persona_id, now)
    if due is not None:
        return due
    rows = memory_mod.list_memories(persona_id)
    fresh = _fresh_fact_topic(rows, now)
    if fresh:
        return Topic(fresh["content"], "fresh_fact")
    older = _older_rows(rows, now)
    if older and deterministic_uniform(f"topic-old:{persona_id}", now) < TOPIC_OLD_P:
        index = int(deterministic_uniform(f"topic-pick:{persona_id}", now) * len(older))
        return Topic(older[index]["content"], "old_memory")
    return None
```

删除原 `pick_topic` 里的 `return "..." , "..."` 元组写法（整段替换）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_bot_proactive.py -v`
Expected: 全部 passed（原有 40+ 用例必须零回归；`topic[1]` 在 `_deliver` 里仍可用）

- [ ] **Step 5: 提交**

```bash
cd d:/workspace/cKunAgentSSH
git add backend/bot/proactive.py backend/tests/test_bot_proactive.py
git commit -m "feat(bot): 主动消息新增计划回溯选题（spec 2026-10-31-plan-followup）"
```

---

### Task 6: 决策层放行、问过即标记、过期清理

**Files:**
- Modify: `backend/bot/proactive.py:71-106`（`desire`）
- Modify: `backend/bot/proactive.py:423-492`（`tick`）
- Modify: `backend/bot/proactive.py:494-559`（`probe`）
- Modify: `backend/bot/proactive.py:561-611`（`_deliver`）
- Test: `backend/tests/test_bot_proactive.py`

**Interfaces:**
- Consumes: `plan_due_topic`（Task 5）、`db.mark_plan_asked` / `db.expire_plan_hooks`（Task 3）
- Produces: `desire(..., plan_due: bool = False)`；`_deliver` 成功后钩子进入 `asked`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_bot_proactive.py` 追加：

```python
def test_desire_allows_a_due_plan_to_break_the_stranger_floor():
    """计划回溯不是未完话题，是「你亲口说过的事」——不熟也该能开口。"""
    state = {"intimacy": 15, "novelty": 60, "tension": 0, "valence": 0, "energy": 0.8}
    assert pro.desire(state, pending_topic=False, gap_hours=0.0) < pro.FIRE_THRESHOLD
    assert (
        pro.desire(state, pending_topic=False, gap_hours=0.0, plan_due=True)
        >= pro.FIRE_THRESHOLD
    )


def test_desire_still_respects_tension_and_low_energy_with_a_due_plan():
    """放行的是「有具体依据时能开口」，不是「无视状态乱开口」。"""
    state = {"intimacy": 15, "novelty": 0, "tension": 50, "valence": 0, "energy": 0.5}
    assert pro.desire(state, pending_topic=False, gap_hours=0.0, plan_due=True) < pro.FIRE_THRESHOLD


def test_desire_without_plan_due_keeps_the_old_floor():
    state = {"intimacy": 10, "novelty": 100, "tension": 0, "valence": 1.0, "energy": 1.0}
    assert pro.desire(state, pending_topic=True, gap_hours=1e9) < pro.FIRE_THRESHOLD


@pytest.mark.asyncio
async def test_deliver_marks_the_plan_as_asked(base_db, monkeypatch):
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    hook_id = _add_due_hook(persona["id"])
    scheduler = pro.ProactiveScheduler()

    async def fake_push(_target, _text, *, now):
        return "push", "", None

    monkeypatch.setattr(scheduler, "_push_qq", fake_push)
    topic = pro.Topic("他之前提过：看展，问问后来怎么样了", "plan_due", hook_id)
    ok = await scheduler._deliver(
        _qq_target(persona, conv), persona, "看展去了吗",
        desire_value=0.8, topic=topic, now=TARGET_NOW,
    )

    assert ok is True
    assert db.due_plan_hook(persona["id"], "2026-11-02 23:00:00", "2026-11-01 12:00:00") is None
    row = _proactive_rows(persona["id"])[-1]
    assert row["reason"] == "ok:plan_due"


@pytest.mark.asyncio
async def test_deliver_does_not_mark_when_the_platform_rejects(base_db, monkeypatch):
    """平台拒收（skip）时不能标记：她想说但没说成，这件事不该作废。"""
    persona, conv = _persona_conv(channel="qq", external_id="c2c:user-1")
    hook_id = _add_due_hook(persona["id"])
    scheduler = pro.ProactiveScheduler()

    async def fake_push(_target, _text, *, now):
        return "skip", "quota", None

    monkeypatch.setattr(scheduler, "_push_qq", fake_push)
    topic = pro.Topic("他之前提过：看展，问问后来怎么样了", "plan_due", hook_id)
    ok = await scheduler._deliver(
        _qq_target(persona, conv), persona, "看展去了吗",
        desire_value=0.8, topic=topic, now=TARGET_NOW,
    )

    assert ok is False
    state = db._get_conn().execute(
        "SELECT state FROM bot_plan_hooks WHERE id = ?", (hook_id,)
    ).fetchone()["state"]
    assert state == "pending", "平台拒收不消耗这次机会，下一轮重试"


@pytest.mark.asyncio
async def test_tick_expires_plan_hooks_past_the_followup_window(base_db, monkeypatch):
    persona, _conv = _persona_conv()
    db.add_plan_hook(
        persona["id"], None, None, "看展", "今晚",
        "2026-10-20 18:00:00", "2026-10-21 00:00:00", "2026-10-20 12:00:00",
    )
    monkeypatch.setattr(db, "list_proactive_targets", lambda: [])

    await pro.ProactiveScheduler().tick()

    row = db._get_conn().execute(
        "SELECT state FROM bot_plan_hooks WHERE persona_id = ?", (persona["id"],)
    ).fetchone()
    assert row["state"] == "expired"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_bot_proactive.py -k "due_plan or plan_as_asked or platform_rejects or expires_plan" -v`
Expected: FAIL —— `TypeError: desire() got an unexpected keyword argument 'plan_due'`

- [ ] **Step 3: 给 `desire` 加放行参数**

```python
def desire(
    state: dict,
    *,
    speed: str = "normal",
    pending_topic: bool = False,
    gap_hours: float = 0.0,
    plan_due: bool = False,
) -> float:
    """「想找你」的意愿，0..1。

    权重即产品判断：关系不熟不打扰（0.30）、话说一半最硬（0.25）、
    计划到期了（0.40，独立项：有时效，过期作废）、有新鲜事想分享（0.20）、
    久不见会想念（0.15）、心情好想说话（0.10）；
    有疙瘩不想先低头（-0.60）、没精力不想说话（-0.50）。

    plan_due 单独拿 0.40 而不是复用 pending_topic 的 0.25：算过数才知道
    0.25 对陌生人根本过不了 0.55（intimacy=10 时 0.30 项只有 0.081）。
    """
    intimacy = float(state.get("intimacy") or 0.0)
    novelty = float(state.get("novelty") or 0.0)
    tension = float(state.get("tension") or 0.0)
    valence = float(state.get("valence") or 0.0)
    energy = float(state.get("energy") or 0.0)

    expected = EXPECTED_GAP_H.get(speed or "normal", 60.0)
    miss = max(0.0, min(1.0, (gap_hours - expected) / expected)) if expected > 0 else 0.0

    value = (
        0.30 * _sigmoid((intimacy - 30.0) / 20.0)
        + 0.20 * (novelty / 100.0)
        + 0.25 * (1.0 if pending_topic else 0.0)
        + 0.40 * (1.0 if plan_due else 0.0)
        + 0.15 * miss
        + 0.10 * max(0.0, valence)
        - 0.60 * (tension / 100.0)
        - 0.50 * max(0.0, 0.5 - energy)
    )
    # 关系硬底线：不熟的人（intimacy<20）哪怕话说一半也不打扰 —— 未完话题
    # 是最硬的钩子，但那是对熟人的；对陌生人它只是"还不知道你叫什么"。
    # 计划回溯例外：他说过的事，对陌生关系也是最自然的开口依据（计划有 36h
    # 时效窗，错过就作废）。四重抑制闸仍然全程生效。
    if intimacy < 20.0 and not plan_due:
        value = min(value, FIRE_THRESHOLD - 0.01)
    return max(0.0, min(1.0, value))
```

> 整个函数体照上块落地（包含底部硬底线），不必分两次改。

- [ ] **Step 4: `tick` 与 `probe` 传入 `plan_due`**

`tick` 里把这一段：

```python
            state, gap_hours, pending_topic, cooldown_raw, stage = await asyncio.to_thread(
                _state_snapshot, persona_id, speed
            )
            d = desire(state, speed=speed, pending_topic=pending_topic, gap_hours=gap_hours)
```

改为：

```python
            state, gap_hours, pending_topic, cooldown_raw, stage = await asyncio.to_thread(
                _state_snapshot, persona_id, speed
            )
            due = await asyncio.to_thread(plan_due_topic, persona_id, now)
            d = desire(
                state, speed=speed, pending_topic=pending_topic,
                gap_hours=gap_hours, plan_due=due is not None,
            )
```

`tick` 开头清理处追加：

```python
        await asyncio.to_thread(db.expire_stale_proactive, _fmt_ts(now))
        await asyncio.to_thread(
            db.expire_plan_hooks,
            _fmt_ts(now - timedelta(hours=PLAN_FOLLOWUP_H)),
        )
```

`probe` 里同样加 `due = await asyncio.to_thread(plan_due_topic, persona_id, now)`，并把 `desire(...)` 调用补上 `plan_due=due is not None`；`probe` 的 `now` 用 `datetime.now(timezone.utc)` 局部变量，避免多处各自取时。

- [ ] **Step 5: `_deliver` 三处成功分支标记 `asked`**

在 `_deliver` 内定义局部 helper（放在方法开头、`pfx` 之后）：

```python
        async def _mark_spoken() -> None:
            # 用 getattr 而不是 topic.plan_id：既有用例（如
            # test_deliver_records_a_pushed_message_as_delivered）仍用二元组传 topic，
            # 直接取属性会 AttributeError。
            plan_id = getattr(topic, "plan_id", "")
            if plan_id:
                await asyncio.to_thread(db.mark_plan_asked, plan_id, _fmt_ts(now))
```

三个成功分支各调一次，位置固定：

- `local` 分支：`await _fire_local(...)` 之后、`return True` 之前；
- `push` 分支：写完 `add_bot_proactive_log(...)` 之后、`return True` 之前；
- `queued` 分支：写完 `add_bot_proactive_log(...)` 之后、`return True` 之前（寄存也算说出口）。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_bot_proactive.py -v`
Expected: 全部 passed

- [ ] **Step 7: 提交**

```bash
cd d:/workspace/cKunAgentSSH
git add backend/bot/proactive.py backend/tests/test_bot_proactive.py
git commit -m "feat(bot): 计划回溯放行陌生人闸、问过即标记、超窗清理（spec 2026-10-31-plan-followup）"
```

---

### Task 7: 全量验证与文档回填

**Files:**
- Modify: `docs/superpowers/specs/2026-10-31-plan-followup-design.md`（追加「实施回填」节）

**Interfaces:**
- Consumes: Task 1-6 全部产出
- Produces: 无代码产出

- [ ] **Step 1: 跑后端全量测试**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全部 passed。若有与本设计无关的既有失败，先记录失败用例名，再单独跑一次确认它在改动前就失败（`git stash` 不推荐，直接看失败栈是否触及本次改动文件）。

- [ ] **Step 2: 跑 lint**

Run: `cd backend && python -m ruff check --select F,E9,E7 bot/plan.py bot/memory.py bot/proactive.py database.py tests/test_bot_plan.py tests/test_bot_proactive.py tests/test_bot_memory.py`
Expected: All checks passed

- [ ] **Step 3: 真机冒烟（无法自动化，记录结果）**

1. 启动后端 + 本地陪聊，对某人格说「周末要去看展」。
2. 查库确认钩子：

```bash
cd backend
python -c "import database as db; print([dict(r) for r in db._get_conn().execute('SELECT id, plan_text, state, due_end FROM bot_plan_hooks')])"
```

后端已启动时库已初始化，无需再调 `init_db`。

3. 把 `due_end` 手工改成过去 2 小时，等一轮 tick（≤75s），对该会话点「试一句」，确认回显 `action=push/send`、`reason` 含 `ok:plan_due`，且 `state` 变为 `asked`。
4. 再点一次「试一句」，确认回显 `reason` 里 **不再** 出现 `plan_due`（只问一次）。

- [ ] **Step 4: 回填 spec**

在 `docs/superpowers/specs/2026-10-31-plan-followup-design.md` 末尾追加实测结果（用例数、命令、与设计的偏差）。若 Step 3 发现偏差，先改代码或改设计，二者只留一份口径。

- [ ] **Step 5: 提交**

```bash
cd d:/workspace/cKunAgentSSH
git add docs/superpowers/specs/2026-10-31-plan-followup-design.md
git commit -m "docs(spec): 计划到期回溯实施回填"
```

---

## 完工标准

- 用户说「周末要去看展」→ 周日 24:00 之后、36h 之内，她主动问一次，且只问一次。
- 同一件事改期（「下周要去看展」）→ 旧钩子 `superseded`，新时间生效。
- 记忆被记忆维护淘汰后，钩子仍能触发（钩子自带 `plan_text`）。
- 平台拒收不消耗这次机会（钩子仍是 `pending`）。
- `python -m pytest tests/ -q` 全绿；`ruff check --select F,E9,E7` 通过。
