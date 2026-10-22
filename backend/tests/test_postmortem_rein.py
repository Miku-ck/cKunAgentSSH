"""事后复盘 PostmortemRein 单测（方案 Phase 2）。

覆盖：成功 run 生成并入库（kind='postmortem'）、纯聊天 run 不生成、
开关关闭不生成、LLM 失败静默不阻塞、超时静默、材料含工具序列与最终答复、
标题截断、作用域纪律（无会话落 global）。
"""
import asyncio
from pathlib import Path

import pytest

import database as db
from agent.harness.contracts import AgentConfig, ReinDecision, RunContext
from agent.harness.reins import lifecycle
from agent.harness.reins.lifecycle import PostmortemRein
from agent.memory import mstore


@pytest.fixture
def memdb():
    """与 test_memory_v2 共用同一个库文件 + 每测试清表（线程本地连接不能换路径）。"""
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    path = base / "memv2-test.db"
    original = db._db_path
    # 全量回归时前面的模块可能把线程绑定到别的库：close-all 让所有线程
    # 在下一次 _get_conn 时按当前 _db_path 重绑（自定义路径测试的既有约定）
    db._close_all_connections()
    db.init_db(str(path))
    conn = db._get_conn()
    for t in ("memories", "memory_jobs", "memory_audit"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    mstore.reset_flags()
    yield
    mstore.reset_flags()
    db._close_all_connections()
    if original:
        db.init_db(original)


class FakeLLM:
    """StreamLLMClient 桩：类级记录调用入参（实例在 _generate_postmortem 内部创建，
    测试只能拿类），行为由类级开关控制，autouse fixture 每测试复位。"""

    calls: list = []
    fail: bool = False
    delay: float = 0.0
    content = "任务：排查 502\n结果：已修复\n经验：先看上游日志"

    def __init__(self, base_url="", api_key=""):
        self.base_url = base_url

    async def call(self, **kwargs):
        FakeLLM.calls.append(kwargs)
        if FakeLLM.fail:
            raise RuntimeError("LLM 炸了")
        if FakeLLM.delay:
            await asyncio.sleep(FakeLLM.delay)
        return type("Out", (), {"content": FakeLLM.content})()


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeLLM.calls = []
    FakeLLM.fail = False
    FakeLLM.delay = 0.0
    yield


def _ctx(*, enabled=True, with_tools=True, task="排查 nginx 502") -> RunContext:
    ctx = RunContext(
        run_id="run-t",
        task=task,
        conversation_id="c1",
        system_prompt="sp",
        history=[],
        memories=[],
        config=AgentConfig(postmortem_enabled=enabled),
        llm_base_url="http://x",
        llm_api_key="k",
        llm_model="m1",
        ssh_session_id="h1",
    )
    if with_tools:
        ctx.tool_calls_log.append(
            {"tool": "sys_overview", "args": {"top": 3}, "iteration": 1, "success": True})
        ctx.tool_calls_log.append(
            {"tool": "shell_exec", "args": {"command": "tail -n 20 error.log"}, "iteration": 2,
             "success": False})
    ctx.messages.append({"role": "assistant", "content": "已定位并修复，验证通过。"})
    ctx.state_history.append({"state": "completed"})
    ctx.errors.append("第 2 轮命令退出码 1")
    return ctx


async def _drain():
    """等全部后台复盘任务落地（runbook 同款手法：从强引用集合收割）。"""
    tasks = [t for t in lifecycle._POSTMORTEM_TASKS if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _tick(ctx):
    """check + 收割必须在同一个事件循环里。

    check() 用 create_task 投递后台复盘；asyncio.run 退出时会取消所有未完成任务，
    所以「先 run(check)、再另起 run(drain)」等于把任务杀掉 —— 断言「无记录」的
    用例会被误判通过，断言「有记录」的两个用例永远挂在 0 条。
    """
    decision = await PostmortemRein().check(ctx)
    await _drain()
    return decision


def _postmortem_rows():
    return mstore.list_memories(kind="postmortem", limit=10)


def test_generated_and_persisted(memdb, monkeypatch):
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", FakeLLM)
    ctx = _ctx()
    d = asyncio.run(_tick(ctx))
    assert isinstance(d, ReinDecision) and not d.intercepted

    rows = _postmortem_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"].startswith("复盘：")
    assert row["scope"] == "global"   # 会话 ID 跨进程回收，复盘固定 global
    assert row["source"] == "agent"
    assert row["agent_run_id"] == "run-t"
    assert "经验" in row["content"]
    # 材料断言：工具序列与最终答复都进了 prompt
    prompt = FakeLLM.calls[0]["messages"][0]["content"]
    assert "sys_overview" in prompt and "已定位并修复" in prompt and "退出码 1" in prompt
    assert FakeLLM.calls[0]["model"] == "m1"
    assert FakeLLM.calls[0]["enable_thinking"] is False


def test_pure_chat_no_generation(memdb, monkeypatch):
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", FakeLLM)
    ctx = _ctx(with_tools=False)
    asyncio.run(_tick(ctx))
    assert _postmortem_rows() == []
    assert not lifecycle._POSTMORTEM_TASKS


def test_disabled_no_generation(memdb, monkeypatch):
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", FakeLLM)
    ctx = _ctx(enabled=False)
    asyncio.run(_tick(ctx))
    assert _postmortem_rows() == []
    assert not lifecycle._POSTMORTEM_TASKS


def test_llm_failure_silent(memdb, monkeypatch):
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", FakeLLM)
    FakeLLM.fail = True
    try:
        asyncio.run(_tick(_ctx()))  # 不抛异常即通过
    finally:
        FakeLLM.fail = False
    assert _postmortem_rows() == []


def test_llm_timeout_silent(memdb, monkeypatch):
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", FakeLLM)
    FakeLLM.delay = 1.0
    monkeypatch.setattr(lifecycle, "_POSTMORTEM_TIMEOUT_S", 0.05)
    try:
        asyncio.run(_tick(_ctx()))
    finally:
        FakeLLM.delay = 0.0
    assert _postmortem_rows() == []


def test_no_llm_config_no_generation(memdb, monkeypatch):
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", FakeLLM)
    ctx = _ctx()
    ctx.llm_base_url = ""
    asyncio.run(_tick(ctx))
    assert _postmortem_rows() == []


def test_title_truncated_and_global_scope(memdb, monkeypatch):
    monkeypatch.setattr("agent.harness.llm_client.StreamLLMClient", FakeLLM)
    ctx = _ctx(task="非" * 100)
    ctx.ssh_session_id = ""
    ctx.tool_calls_log.append({"tool": "sys_overview", "args": {}, "iteration": 1, "success": True})
    asyncio.run(_tick(ctx))
    rows = _postmortem_rows()
    assert len(rows) == 1
    assert len(rows[0]["title"]) <= len("复盘：") + 50
    assert rows[0]["scope"] == "global"


def test_material_truncation_guard():
    """长 run 材料：工具序列/错误/答复各段截断，prompt 不会无限膨胀。"""
    ctx = _ctx()
    for i in range(200):
        ctx.tool_calls_log.append({"tool": f"t{i}", "args": {"k": "v" * 500}, "iteration": i,
                                   "success": True})
    ctx.messages.append({"role": "assistant", "content": "长" * 5000})
    ctx.errors.extend(["e" * 1000] * 30)
    material = lifecycle._postmortem_material(ctx)
    assert len(material) < 20000
    assert material.count("- t") <= 60
