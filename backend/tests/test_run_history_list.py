"""GET /agent/runs 的过滤与分页（spec 2026-09-30-retire-ai-side-panel R2）

钉三件容易悄悄坏的事：
- items 与 total 必须是同一个过滤集合（审计表那边 count 曾漏条件，翻页时
  "共 N 条"与眼前几行不是同一批）；
- 列表默认不返回 state_history / context_summary 这两个整段 JSON 列 ——
  表格没有一列用得到，带上就是把列表响应撑大几十倍；
- offset 翻页取得到更旧的记录；
- 端点层同样生效（路由参数名写错时 db 函数全绿、筛选被悄悄忽略），limit/offset 有边界。
"""
from pathlib import Path
import re

import pytest

import database as db


@pytest.fixture
def runs_db(tmp_workspace):
    """换库必须连连接池一起换。

    `database` 的池是 keep-alive：`conn.close()` 只 rollback、底层句柄留着复用，
    光改 `_db_path` 不改线程里的旧连接 —— 第二个用例起主线程仍在读第一个用例的库，
    而新路径压根没被建出来，工作线程随后连上的是一个空文件（no such table）。
    全仓库换库的 fixture 都成对调 `_close_all_connections()`，这里照抄。
    """
    original = db._db_path
    path = Path(tmp_workspace) / "runs-list-test.db"
    db._close_all_connections()
    db.init_db(str(path))
    try:
        conn = db._get_conn()
        conn.execute("DELETE FROM agent_runs")
        conn.commit()
        conn.close()
        # agent_runs.conversation_id 有 FK（ON DELETE CASCADE），先有会话再有 run
        c1 = db.create_conversation("会话一")["id"]
        c2 = db.create_conversation("会话二")["id"]
        for i in range(5):
            db.create_agent_run(f"run{i}", c1 if i < 3 else c2, f"任务 {i}")
        yield {"c1": c1, "c2": c2}
    finally:
        db._close_all_connections()
        db.init_db(original)
        db._close_all_connections()


def test_total_与_items_同一份过滤(runs_db):
    items = db.get_agent_runs(conversation_id=runs_db["c1"])
    assert len(items) == 3
    assert db.count_agent_runs(conversation_id=runs_db["c1"]) == 3
    # 不带条件时两者也必须一致，否则"共 5 条"配着 3 行就是骗人
    assert db.count_agent_runs() == len(db.get_agent_runs(limit=50)) == 5


def test_status_过滤同时作用于列表与计数(runs_db):
    # 全部还是 running；改成 completed 的只有前两条
    db.update_agent_run("run0", status="completed")
    db.update_agent_run("run1", status="completed")
    assert len(db.get_agent_runs(status="completed", limit=50)) == 2
    assert db.count_agent_runs(status="completed") == 2
    assert db.count_agent_runs(status="never_such") == 0


def test_run_id_精确匹配(runs_db):
    """trace 号是 `id = ?`：粘半个号筛不出东西，与审计页那条同一口径。"""
    assert len(db.get_agent_runs(run_id="run0", limit=50)) == 1
    assert db.count_agent_runs(run_id="run") == 0


def test_列表默认不带两个大_json_列(runs_db):
    row = db.get_agent_runs(limit=1)[0]
    assert "state_history" not in row
    assert "context_summary" not in row
    # 旧调用方（consciousness_tick）要整行时仍拿得到全列
    assert "state_history" in db.get_agent_runs(limit=1, full=True)[0]


def test_offset_翻页取更旧的(runs_db):
    # 把 5 条钉进**同一秒**：_now() 只到秒，同秒才是本仓的真实形状（一次会话里
    # 连发几条 run）。没有 `id DESC` 兜底时 SQLite 对同键行的次序由查询计划自己
    # 决定，而翻页是两次独立请求 → 同一条印在两页上、另一条永远不出现。
    conn = db._get_conn()
    try:
        conn.execute("UPDATE agent_runs SET started_at = '2026-10-08 09:00:00'")
        conn.commit()
    finally:
        conn.close()
    assert [r["id"] for r in db.get_agent_runs(limit=50)] == [
        "run4", "run3", "run2", "run1", "run0",
    ]
    page1 = [r["id"] for r in db.get_agent_runs(limit=2, offset=0)]
    page2 = [r["id"] for r in db.get_agent_runs(limit=2, offset=2)]
    assert len(page1) == 2 and len(page2) == 2
    assert not set(page1) & set(page2)
    # 两页拼起来必须是全集：不重还要不漏，否则"共 5 条"底下只有 4 条可见
    assert set(page1) | set(page2) | {r["id"] for r in db.get_agent_runs(limit=1, offset=4)} == \
        {"run0", "run1", "run2", "run3", "run4"}


def _client():
    import fastapi
    from fastapi.testclient import TestClient

    from routes.agent import router

    app = fastapi.FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_端点_列表与总数走同一条件(runs_db):
    """把 /agent/runs 本身跑一遍：路由参数名与 db 形参对不上时，
    列表函数全绿而端点悄悄忽略筛选，正是审计表踩过的形状。"""
    db.update_agent_run("run0", status="completed")
    body = _client().get("/agent/runs?status=completed&limit=50").json()
    assert body["total"] == 1
    assert [r["id"] for r in body["items"]] == ["run0"]
    assert "state_history" not in body["items"][0]


def test_端点_越界分页参数被拒(runs_db):
    """limit/offset 有边界：limit=0 会返回空 items 却报 total=5，
    前端据此渲染出"共 5 条 / 第 1–0 条"；大 limit 则是不设防的全表拉取。"""
    client = _client()
    assert client.get("/agent/runs?limit=0").status_code == 422
    assert client.get("/agent/runs?limit=100000").status_code == 422
    assert client.get("/agent/runs?offset=-1").status_code == 422


def _agent_run_row_keys() -> set[str]:
    """解析 src/api/client.ts 里 AgentRunRow 接口的成员名（先剥注释）。

    剥注释这条不是讲究排版：`//[^\n]*` 不去掉的话，接口上方那句"没有目标主机与
    审批档"会被当成字段名读进来（round22 的 CI-yml 解析守卫就是被注释里的
    `|| true` 骗过一次）。
    """
    src = (Path(__file__).resolve().parents[2] / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    m = re.search(r"export interface AgentRunRow\s*\{([^}]*)\}", src)
    assert m, "client.ts 里找不到 AgentRunRow 接口（改名或挪走了，守卫该红在这里）"
    body = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", m.group(1))
    return set(re.findall(r"^\s*(\w+)\s*:", body, re.MULTILINE))


def _projection_columns() -> set[str]:
    return {c.strip() for c in db._RUN_LIST_COLUMNS.split(",") if c.strip()}


def test_列表键集合三层逐字段相等(runs_db):
    """投影列 ⇄ 端点实发键 ⇄ client.ts 的 AgentRunRow，三份必须是同一个集合。

    本仓反复付学费的「跨层键漂移」：改名只发生在一侧，两侧各自的用例都绿，
    而表格里那一格静默印空白。只钉"这一份有 14 个键"不够，要钉**两边相等**。
    """
    cols = _projection_columns()
    assert len(cols) == 14
    assert _agent_run_row_keys() == cols
    body = _client().get("/agent/runs?limit=50").json()
    assert set(body["items"][0]) == cols


def test_键守卫自己会红_变异自检():
    """解析类守卫是代码：拿不到东西时它会绿着骗人，比没有守卫更糟。

    变异必须打在**已抽取的块**上 —— 直接对整份 client.ts 做字符串替换会命中
    同名别处（round23 抄这段时就踩过：改 top_p 命中的是读取侧 ModelConfig）。
    """
    keys = _agent_run_row_keys()
    assert len(keys) == 14, "解析到的键数不对，上一条断言就是在比空集"
    mutated = (keys - {"tool_call_count"}) | {"calls"}
    assert mutated ^ _projection_columns() == {"tool_call_count", "calls"}
