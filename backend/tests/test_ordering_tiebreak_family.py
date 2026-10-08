"""同秒排序族的三处漏网：非唯一排序键 + 翻页 / 取「最后一条」。

`_now()` 只到秒，所以「同一批写入的多行 created_at/updated_at 完全相等」是本仓的
真实形状，不是边角：一次会话连发几条 run、一轮 run 里连调几个工具、用户消息和
回复落在同一秒，都会撞出并列键。`get_agent_runs`（本文件外的
test_run_history_list.py::test_offset_翻页取更旧的）和 `get_recent_messages`
（test_minor_round3.py::test_get_recent_messages_rowid_tiebreak）都为此补过兜底键，
这里是同一族里还没补的兄弟：

- `list_conversations` 外层翻页：并列时两次独立请求给 SQLite 自己决定次序 →
  同一条会话印在两页上、另一条永远不出现；
- `list_conversations` 的 preview 子查询：`ORDER BY … DESC LIMIT 1` 在同键行里
  扫到的第一条就是「最优」，于是会话列表预览的是用户刚发的那句，而不是回复；
- `list_skills` 翻页：同上；
- `get_tool_calls`：轨迹回放按它排因果序，同秒的连续调用次序随机 = 面板上
  「先重启后改配置」这种顺序谎话。

源码级那条守卫是主力：并列次序由查询计划决定，行为类断言可能在修好之前碰巧
也绿（小表全扫正好按 rowid 返回），而「每条 ORDER BY 都得有第二个排序项」这个
不变式不会碰巧成立。行为类用例保留，它们钉的是用户看得见的那两个症状。
"""
import inspect
import re

import pytest

import database as db

_STOP = re.compile(r"\bLIMIT\b|\bOFFSET\b|\"|'", re.IGNORECASE)


def _terms_from_src(src: str) -> list[list[str]]:
    """从源码文本抽每条 ORDER BY 的排序项（按逗号切，忽略换行与缩进）

    终止符必须包含引号：SQL 到字符串字面量结尾就结束了，只认 LIMIT/右括号的话会一路
    吃进后面的 Python 实参 —— `ORDER BY created_at` 后面那个逗号被当成第二个排序项，
    守卫对 get_tool_calls 直接放行（首跑就是这么漏掉的）。
    """
    groups = []
    for m in re.finditer(r"ORDER\s+BY", src, re.IGNORECASE):
        rest = src[m.end():]
        stop = _STOP.search(rest)
        clause = rest[: stop.start()] if stop else rest
        terms = [t.strip() for t in clause.split(",") if t.strip()]
        assert terms, f"有条 ORDER BY 一个排序项都没抽到，守卫在比空集：{clause[:40]!r}"
        groups.append(terms)
    return groups


def _order_terms(fn_name: str) -> list[list[str]]:
    return _terms_from_src(inspect.getsource(getattr(db, fn_name)))


def test_抽取器自己数得对_变异自检():
    """解析类守卫必须先证明自己数得准，否则「条数」和「≥2 项」都是编出来的。"""
    assert _terms_from_src('SELECT * FROM t ORDER BY created_at",\n        (x,)') == [
        ["created_at"]]
    assert _terms_from_src("ORDER BY m.created_at DESC LIMIT 1") == [["m.created_at DESC"]]
    assert _terms_from_src('ORDER BY priority DESC, updated_at DESC"') == [
        ["priority DESC", "updated_at DESC"]]
    # 已经修好的兄弟必须被数成「有兜底键」；数不出来就是抽取器在骗人，不是它变好了
    assert all(len(g) >= 2 for g in _order_terms("get_agent_runs")), (
        "get_agent_runs 的 started_at DESC, id DESC 没被数出 2 项 —— 抽取器不可信")



@pytest.mark.parametrize(
    "fn_name,expected_groups",
    [
        # (ORDER BY 条数, 每条至少几个排序项)。条数写死 = 新增一条漏兜底的查询
        # 会在这里红，而不是静默少检一条。
        ("list_conversations", 2),
        ("list_skills", 1),
        ("get_tool_calls", 1),
    ],
)
def test_every_order_by_has_a_unique_tiebreak(fn_name, expected_groups):
    groups = _order_terms(fn_name)
    assert len(groups) == expected_groups, (
        f"{fn_name} 的 ORDER BY 条数变了（{len(groups)} != {expected_groups}）："
        "要么新增了一条没兜底键的排序，要么这条守卫检的位置已经不对了")
    for terms in groups:
        assert len(terms) >= 2, (
            f"{fn_name}: {terms[0]!r} 单独排序 —— _now() 只到秒，同批写入必然并列，"
            f"翻页会重复/漏行，取 LIMIT 1 会拿到任意一条。补唯一键（id 或 rowid）。")


@pytest.fixture
def ord_db():
    """固定同一个库文件、逐用例清表（同 test_memory_v2.memdb）。

    不借 tmp_workspace：它的 teardown 会 rmtree 掉数据库文件所在目录，而
    `database` 的连接是线程本地 keep-alive —— 主线程 `_close_all_connections()`
    关不到别的线程手里的句柄，Windows 下在还开着的文件上删目录就是挂起/幽灵数据
    （test_run_history_list.py + test_memory_v2.py 的合跑在 HEAD 上就卡在这，
    5 个点之后 150s 不返）。库文件留着不删，路径切换才安全。
    """
    from pathlib import Path

    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    original = db._db_path
    db._close_all_connections()
    db.init_db(str(base / "ordering-test.db"))
    try:
        conn = db._get_conn()
        try:
            for table in ("conversations", "skills", "tool_calls", "agent_runs", "messages"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()
        yield
    finally:
        db._close_all_connections()
        if original:
            db.init_db(original)
        db._close_all_connections()


def _pin(column: str, table: str, value: str) -> None:
    conn = db._get_conn()
    try:
        conn.execute(f"UPDATE {table} SET {column} = ?", (value,))
        conn.commit()
    finally:
        conn.close()


def test_conversation_pages_never_repeat_or_drop_a_session(ord_db):
    ids = [db.create_conversation(f"会话 {i}")["id"] for i in range(5)]
    _pin("updated_at", "conversations", "2026-10-08 09:00:00")

    page1 = [r["id"] for r in db.list_conversations(limit=2, offset=0)]
    page2 = [r["id"] for r in db.list_conversations(limit=2, offset=2)]
    assert len(page1) == len(page2) == 2
    assert not set(page1) & set(page2), "同一条会话出现在两页上"
    assert set(page1) | set(page2) | {
        r["id"] for r in db.list_conversations(limit=1, offset=4)
    } == set(ids), "两页拼起来不是全集：有会话翻页翻不到"
    # 翻页必须真的走完全集，而不是两次都给同一批（limit 大到一次装下时）
    assert [r["id"] for r in db.list_conversations(limit=50)] == sorted(ids, reverse=True)


def test_preview_shows_the_latest_message_not_an_arbitrary_one(ord_db):
    """"最后一条消息"在同秒里必须取插入序最晚的那条 —— 预览显示的是回复不是提问。"""
    cid = db.create_conversation("同秒对话")["id"]
    db.add_message(cid, "user", "nginx 配置在哪")
    db.add_message(cid, "assistant", "在 /etc/nginx/nginx.conf")
    _pin("created_at", "messages", "2026-10-08 09:00:00")

    row = db.list_conversations(limit=50, mode=None)[0]
    assert row["preview"] == "在 /etc/nginx/nginx.conf", (
        f"预览拿到的是 {row['preview']!r}：ORDER BY created_at DESC LIMIT 1 在一堆"
        "同键行里返回扫描到的第一条 = 用户那句，回复被吞了")


def test_skill_pages_never_repeat_or_drop_a_skill(ord_db):
    for i in range(5):
        db.create_skill(name=f"技能 {i}", slug=f"ord-{i}", system_prompt="x")
    _pin("updated_at", "skills", "2026-10-08 09:00:00")

    page1 = [r["id"] for r in db.list_skills(limit=2, offset=0)]
    page2 = [r["id"] for r in db.list_skills(limit=2, offset=2)]
    assert len(page1) == 2 and len(page2) == 2
    assert not set(page1) & set(page2), "同一条技能印在两页上"
    all_ids = {r["id"] for r in db.list_skills(limit=50, offset=0)}
    assert set(page1) | set(page2) <= all_ids
    assert len(all_ids) == 5
