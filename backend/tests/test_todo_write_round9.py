"""todo_write 工具回归 —— 该工具此前零覆盖，本轮修的两处越权写入都在里面。

- status：schema 声明了 enum，代码却原样收下任意字符串并渲染回模型
- content：单条不限长度，而每次 create/update 都把整张表回显进上下文
"""
import pytest

from agent.tools import ToolContext
from agent.tools.builtin.todo_write import (
    TodoWriteTool,
    _STATUSES,
    _MAX_CONTENT_CHARS,
    _MAX_TODOS_PER_RUN,
    _todo_store,
    clear_todos,
)

RUN = "run-todo-test"


def _ctx(run_id: str = RUN) -> ToolContext:
    return ToolContext(ssh_session_id="", mode="ssh", environment="test",
                       visible_terminal=False, agent_run_id=run_id)


@pytest.fixture(autouse=True)
def _isolate_store():
    clear_todos(RUN)
    clear_todos("run-other")
    yield
    clear_todos(RUN)
    clear_todos("run-other")


async def _tool(run_id: str = RUN, **kw):
    return await TodoWriteTool().execute(_ctx(run_id), **kw)


@pytest.mark.asyncio
async def test_crud_round_trip():
    created = await _tool(action="create", content="跑一遍后端闸门")
    assert created.success
    todo_id = created.output.split(":", 1)[1].split("\n", 1)[0].strip()

    assert (await _tool(action="update", id=todo_id, status="in_progress")).success
    listed = await _tool(action="list")
    assert f"[in_progress] 跑一遍后端闸门" in listed.output

    assert (await _tool(action="delete", id=todo_id)).success
    assert "（无任务）" in (await _tool(action="list")).output
    assert RUN in _todo_store  # 删空只清列表，不摘掉 run 的条目


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["doing", "已完成", "PENDING"])
async def test_unknown_status_rejected(status):
    assert (await _tool(action="create", content="x")).success
    res = await _tool(action="update", id=_todo_store[RUN][0]["id"], status=status)
    assert res.success is False
    assert "status 必须是" in res.error
    assert _todo_store[RUN][0]["status"] == "pending"  # 脏值不落库


@pytest.mark.asyncio
@pytest.mark.parametrize("status", _STATUSES)
async def test_declared_status_accepted(status):
    assert (await _tool(action="create", content="x")).success
    res = await _tool(action="update", id=_todo_store[RUN][0]["id"], status=status)
    assert res.success is True, res.error
    assert _todo_store[RUN][0]["status"] == status


def test_status_enum_in_schema_matches_validation_source():
    assert TodoWriteTool.parameters["properties"]["status"]["enum"] == list(_STATUSES)


@pytest.mark.asyncio
async def test_oversized_content_rejected_on_create_and_update():
    big = "长" * (_MAX_CONTENT_CHARS + 1)
    assert (await _tool(action="create", content=big)).success is False
    assert (await _tool(action="create", content="ok" * 20)).success
    tid = _todo_store[RUN][0]["id"]
    res = await _tool(action="update", id=tid, content=big)
    assert res.success is False
    assert _todo_store[RUN][0]["content"] == "ok" * 20


@pytest.mark.asyncio
async def test_max_todos_per_run_cap():
    for i in range(_MAX_TODOS_PER_RUN):
        assert (await _tool(action="create", content=f"t{i}")).success
    over = await _tool(action="create", content="越界")
    assert over.success is False
    assert "上限" in over.error
    assert len(_todo_store[RUN]) == _MAX_TODOS_PER_RUN


@pytest.mark.asyncio
async def test_runs_are_isolated_and_clear_releases():
    await _tool(action="create", content="属于本 run")
    other = await _tool("run-other", action="list")
    assert "（无任务）" in other.output
    assert _todo_store[RUN][0]["content"] == "属于本 run"

    assert (await _tool(action="clear")).success
    clear_todos(RUN)
    assert RUN not in _todo_store


@pytest.mark.asyncio
async def test_missing_run_id_degrades_without_raising():
    ctx = ToolContext(ssh_session_id="", mode="ssh", environment="test",
                      visible_terminal=False)
    res = await TodoWriteTool().execute(ctx, action="create", content="x")
    assert res.success and "降级" in res.output
    assert "（无任务）" in (await TodoWriteTool().execute(ctx, action="list")).output


@pytest.mark.asyncio
async def test_unknown_action_is_failure():
    res = await _tool(action="explode")
    assert res.success is False
    assert "未知 action" in res.error
