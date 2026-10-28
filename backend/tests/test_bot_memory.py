"""陪聊长期记忆：抽取规则（纯函数，无 IO）。

只测抽取本身 —— 存取、注入、路由在各自文件里覆盖。
"""
import asyncio

import database as db
from agent.memory import mstore, retriever
from bot import memory as mem


def _facts(text: str) -> list[str]:
    return [f.content for f in mem.extract_facts(text)]


def test_extracts_name_residence_and_preference():
    facts = _facts("我叫小林，我住在杭州，我喜欢吃辣")
    assert "用户叫小林" in facts
    assert "用户住在杭州" in facts
    assert "用户喜欢辣" in facts


def test_extracts_birthday_job_pet_and_family():
    facts = _facts("我生日是3月5号，我是算法工程师，我养了只猫，我女儿叫豆豆")
    assert "用户的生日是3月5号" in facts
    assert "用户的职业是算法工程师" in facts
    assert "用户养了猫" in facts
    assert "用户的女儿是豆豆" in facts


def test_negative_forms_win_over_positive_like():
    facts = _facts("我不喜欢吃香菜，我讨厌加班")
    assert "用户不吃香菜" in facts
    assert "用户不喜欢加班" in facts
    # 否定句不能同时被当成喜好
    assert "用户喜欢香菜" not in facts
    assert "用户喜欢加班" not in facts


def test_habit_and_plan():
    facts = _facts("我每天早上跑步，周末要去看展")
    assert "用户的长期习惯：早上跑步" in facts
    assert "用户的计划：看展" in facts


def test_sensitive_message_is_not_mined():
    # 身份证 / 手机号 / 密码 / 门牌：整条不抽，不是脱敏后抽
    assert _facts("我的身份证是110101199001011234") == []
    assert _facts("我的手机号13800138000") == []
    assert _facts("密码: hunter2") == []
    assert _facts("我住在杭州，密码是 hunter2") == []


def test_negative_instruction_suppresses_extraction():
    assert _facts("别记这个，我叫小林") == []
    assert _facts("不用记，我住在杭州") == []


def test_forget_instruction_is_detected():
    assert mem.detect_forget("忘掉我说过的火锅") == "火锅"
    assert mem.detect_forget("别记我的火锅") == "火锅"
    # 「别记这个」是抑制，不是定向删除
    assert mem.detect_forget("别记这个") is None
    assert mem.detect_forget("我今天吃了火锅") is None


def test_verbose_chatter_is_not_mined():
    # 捕获组有长度上限：整段闲聊不能被当成一条事实
    assert _facts("我喜欢" + "很" * 40 + "好吃的东西") == []
    # 非居住语境不产生居住事实
    assert "用户住在开会" not in _facts("我在开会")


# ── 存取层 ──


def _persona(name: str = "晚风") -> dict:
    return db.create_bot_persona(name=name)


def test_remember_writes_facts_into_persona_scope(base_db):
    persona = _persona()
    written = mem.remember(persona["id"], "conv-1", "我叫小林，我住在杭州")

    assert written == 2
    rows = mem.list_memories(persona["id"])
    assert {row["content"] for row in rows} == {"用户叫小林", "用户住在杭州"}
    assert all(row["scope"] == f"persona:{persona['id']}" for row in rows)
    assert all(row["kind"] == mem.KIND for row in rows)


def test_same_fact_is_not_written_twice(base_db):
    persona = _persona()
    assert mem.remember(persona["id"], "conv-1", "我叫小林") == 1
    assert mem.remember(persona["id"], "conv-2", "我叫小林") == 0
    assert len(mem.list_memories(persona["id"])) == 1


def test_single_slot_fact_supersedes_the_previous_one(base_db):
    persona = _persona()
    mem.remember(persona["id"], "conv-1", "我住在杭州")
    mem.remember(persona["id"], "conv-2", "我住在上海")

    rows = mem.list_memories(persona["id"])
    assert [row["content"] for row in rows] == ["用户住在上海"]
    # 旧行不是被删，是让位（status=superseded 且指向新行）
    conn = db._get_conn()
    try:
        old = conn.execute(
            "SELECT status, superseded_by FROM memories WHERE content = ?", ("用户住在杭州",)
        ).fetchone()
    finally:
        conn.close()
    assert old["status"] == "superseded"
    assert old["superseded_by"] == rows[0]["id"]


def test_multi_slot_facts_coexist(base_db):
    persona = _persona()
    mem.remember(persona["id"], "conv-1", "我喜欢辣")
    mem.remember(persona["id"], "conv-2", "我喜欢咖啡")
    assert {row["content"] for row in mem.list_memories(persona["id"])} == {
        "用户喜欢辣",
        "用户喜欢咖啡",
    }


def test_memories_are_scoped_per_persona(base_db):
    a = _persona("晚风")
    b = _persona("小暖")
    mem.remember(a["id"], "conv-1", "我叫小林")

    assert [row["content"] for row in mem.list_memories(b["id"])] == []
    assert mem.recall_sync(b["id"], "小林") == []


def test_forget_by_keyword_invalidates_the_row(base_db):
    persona = _persona()
    mem.remember(persona["id"], "conv-1", "我喜欢辣")
    removed = mem.forget(persona["id"], "辣")

    assert removed == 1
    assert mem.list_memories(persona["id"]) == []


def test_clear_all_forgets_everything(base_db):
    persona = _persona()
    mem.remember(persona["id"], "conv-1", "我叫小林，我住在杭州")
    assert mem.clear_all(persona["id"]) == 2
    assert mem.list_memories(persona["id"]) == []


def test_recall_returns_plain_lines_without_memory_ids(base_db):
    persona = _persona()
    mem.remember(persona["id"], "conv-1", "我喜欢辣")
    lines = mem.recall_sync(persona["id"], "辣")

    assert lines == ["用户喜欢辣"]
    assert not any("[mem:" in line for line in lines)


def test_recall_falls_back_to_recent_when_nothing_matches(base_db):
    persona = _persona()
    mem.remember(persona["id"], "conv-1", "我喜欢辣")
    # 问的是完全不相关的事：陪伴场景里「它确实记得」比相关性更重要
    assert mem.recall_sync(persona["id"], "今天天气怎么样") == ["用户喜欢辣"]


def test_recall_finds_the_name_when_asked_by_question(base_db):
    """中文 bigram 索引桥接不了问句与事实：「你还记得我叫什么吗」与「用户叫小林」
    没有任何两字重合，必须靠单字重合度把名字捞出来。"""
    persona = _persona()
    mem.remember(persona["id"], "conv-1", "我叫小林，我住在杭州，我喜欢吃辣")

    assert mem.recall_sync(persona["id"], "你还记得我叫什么吗") == ["用户叫小林"]
    # 字面重合时 FTS 直接命中
    assert mem.recall_sync(persona["id"], "小林") == ["用户叫小林"]


def test_agent_scope_never_sees_persona_memories(base_db):
    persona = _persona()
    mem.remember(persona["id"], "conv-1", "我喜欢辣")

    hits = asyncio.run(
        retriever.search("辣", scopes=["global", "host:s1"], kinds=None, top_k=10)
    )
    assert hits == []
    # Agent 的兜底路径也必须带 scope，否则故障时会把人格记忆串进 Agent run
    assert mstore.recent_fallback_lines(5, ["global", "host:s1"]) == []
