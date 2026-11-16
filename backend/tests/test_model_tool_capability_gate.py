"""模型能力门（supports_tool_calling 消费）— 预判去 tools 的回归测试。

背景：models_config 的三列能力标记此前只写不读（前端只渲染徽标）。能力门上线后
`supports_tool_calling=0` 有了真实语义：Driver 构建 tools_schema 时预判去 tools
直接纯文本跑，而不是带 tools 上去撞 BadRequest 再靠 llm_adapter 降级兜底。

守护点：
- 三态语义：False=去 tools 且发 warning；None/True=维持现状（schema 原样透传）；
- 与强制收敛轮叠加不冲突（两者都是 tools=[]，收敛轮语义不受影响）；
- 存量基线迁移：一次性 0→1（app_settings 守卫幂等），迁移后用户显式设 0 才生效；
- 种子模型 supports_tool_calling=1；
- get_model_config 返回 dict 携带该列（三持有点注入的数据前提）。

运行: python -m pytest backend/tests/test_model_tool_capability_gate.py -v
"""
import asyncio

import database as db
from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    ReinDecision,
    ReinPoint,
    RunContext,
    State,
    ToolCall,
)
from agent.harness.driver import Driver

_SENTINEL_SCHEMA = [{"type": "function", "function": {"name": "shell_exec"}}]


class _NoopTelemetry:
    async def emit(self, ev):
        return None


class _StubBridle:
    async def assemble(self, ctx):
        return list(ctx.messages)

    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text}


class _StubSaddle:
    def schemas(self):
        return list(_SENTINEL_SCHEMA)

    async def execute(self, tc, ctx, auto_rollback=True):
        from agent.harness.contracts import ToolResult
        return ToolResult(success=True, output="ok", duration_ms=1), None


class _RecordingStirrups:
    """记录每次 call_llm 的 tools 实参；前 N 轮返回工具调用，之后返回文本"""

    def __init__(self, tool_rounds: int = 1):
        self.calls: list[list] = []
        self._round = 0
        self._tool_rounds = tool_rounds

    async def call_llm(self, ctx, messages, tools_schema):
        self.calls.append(list(tools_schema or []))
        self._round += 1
        if self._round <= self._tool_rounds:
            return LLMOutput(tool_calls=[ToolCall(
                id=f"tc{self._round}", name="shell_exec", args={"command": "echo hi"})])
        return LLMOutput(content="最终总结：已完成全部事项")

    async def undo_all(self, ctx):
        return None


class _StubReins:
    async def check(self, point: ReinPoint, ctx: RunContext) -> ReinDecision:
        return ReinDecision()


class _StubHarness:
    def __init__(self, stirrups):
        self.telemetry = _NoopTelemetry()
        self.bridle = _StubBridle()
        self.saddle = _StubSaddle()
        self.stirrups = stirrups
        self.cost_tracker = None
        self.reins = _StubReins()


async def _collect(harness, ctx):
    events = []
    async for ev in Driver().drive(ctx, harness):
        events.append(ev)
    return events


def _run(stirrups, **config_kw):
    config = AgentConfig(max_iterations=3, enable_intention_guide=False, **config_kw)
    ctx = RunContext.new(task="测试任务", config=config)
    harness = _StubHarness(stirrups)
    events = asyncio.run(_collect(harness, ctx))
    return ctx, harness, events


# ── 三态语义 ──────────────────────────────────────────────────────────────────

def test_capability_false_strips_tools_and_warns():
    stirrups = _RecordingStirrups()
    ctx, harness, events = _run(stirrups, supports_tool_calling=False)
    # 每一轮 call_llm 收到的 tools 都是空（预判去 tools，而非撞 400 后降级）
    assert stirrups.calls and all(tools == [] for tools in stirrups.calls)
    # 用户可见的 warning 只发一次
    warns = [e for e in events if e.kind == "warning" and e.payload.get("code") == "tool_calling_disabled"]
    assert len(warns) == 1
    assert "纯文本" in warns[0].payload["message"]


def test_none_and_true_keep_schema_passthrough():
    for value in (None, True):
        stirrups = _RecordingStirrups()
        ctx, harness, events = _run(stirrups, supports_tool_calling=value)
        assert stirrups.calls[0] == _SENTINEL_SCHEMA
        assert not any(
            e.kind == "warning" and e.payload.get("code") == "tool_calling_disabled"
            for e in events)


def test_capability_false_with_forced_convergence_no_conflict():
    """能力门（全程去 tools）与收敛轮（末轮禁 tools）叠加：不崩、纯文本收尾 COMPLETED。"""
    stirrups = _RecordingStirrups()
    ctx, harness, events = _run(stirrups, supports_tool_calling=False)
    assert ctx.state == State.COMPLETED, f"纯文本应正常收尾，实际 {ctx.state}"


def test_capability_false_ignores_hallucinated_tool_calls():
    """tools=[] 下模型仍凭惯性输出 tool_calls → 物理丢弃（收敛轮同款防御）。"""
    stirrups = _RecordingStirrups(tool_rounds=99)
    ctx, harness, events = _run(stirrups, supports_tool_calling=False)
    assert ctx.state == State.COMPLETED, f"幻觉调用应被丢弃并收尾，实际 {ctx.state}"
    assert not ctx.tool_calls_log


# ── DB 层：存量基线迁移 / 种子 / 列读取 ───────────────────────────────────────

def _fresh_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(tmp_workspace + "/toolcap.sqlite")
    return db


def _seed_one_row():
    """造一条模型行（种子直插路径，绕开 create 默认值）。"""
    conn = db._get_conn()
    try:
        db._insert_seed_model(
            conn, model_id="legacy-m", base_url="http://upstream.invalid/v1",
            api_key="", supports_thinking=False, is_default=False,
            sort_order=0, now=db._now())
        conn.commit()
    finally:
        conn.close()


def test_seed_models_have_tool_calling_enabled(tmp_workspace):
    """种子模型（_insert_seed_model 直插路径）必须带 supports_tool_calling=1
    （0 会立即失能工具；首启种子不经 create_model_config，单独钉住）。"""
    _fresh_db(tmp_workspace)
    _seed_one_row()
    row = db._get_conn().execute(
        "SELECT supports_tool_calling FROM models_config WHERE model_id = 'legacy-m'"
    ).fetchone()
    assert row and row["supports_tool_calling"] == 1


def test_baseline_migration_upgrades_legacy_zeros_once(tmp_workspace):
    """存量行 0 → 1 一次性升级；守卫键落库后，此后显式设 0 不再被迁移改写。"""
    _fresh_db(tmp_workspace)
    _seed_one_row()
    conn = db._get_conn()
    # 模拟存量：把一行显式降到 0（此时守卫键已在首次 init 落库）
    conn.execute("UPDATE models_config SET supports_tool_calling = 0")
    conn.commit()
    assert db.get_app_setting("migration.tool_capability_baseline") == "done"
    row = db._get_conn().execute(
        "SELECT supports_tool_calling FROM models_config LIMIT 1").fetchone()
    assert row["supports_tool_calling"] == 0, "守卫键已存在：用户显式设 0 必须生效"


def test_baseline_migration_on_legacy_db(tmp_workspace):
    """旧库（守卫键不存在）首次 init：0 → 1 升级 + 守卫键落库。"""
    db._close_all_connections()
    db.init_db(tmp_workspace + "/legacy.sqlite")
    _seed_one_row()
    conn = db._get_conn()
    conn.execute("UPDATE models_config SET supports_tool_calling = 0")
    conn.execute("DELETE FROM app_settings WHERE key = 'migration.tool_capability_baseline'")
    conn.commit()
    db._close_all_connections()
    db.init_db(tmp_workspace + "/legacy.sqlite")
    rows = db._get_conn().execute(
        "SELECT supports_tool_calling FROM models_config").fetchall()
    assert rows and all(r["supports_tool_calling"] == 1 for r in rows)
    assert db.get_app_setting("migration.tool_capability_baseline") == "done"


def test_get_model_config_carries_capability_column(tmp_workspace):
    """三个持有点从 get_model_config 注入的列必须真的在返回 dict 里；
    create 路径默认 True（与基线迁移同口径，UI 不再发送该字段）。"""
    _fresh_db(tmp_workspace)
    cfg_id = db.create_model_config(name="m", base_url="http://upstream.invalid/v1")["id"]
    cfg = db.get_model_config(cfg_id, mask_key=False)
    assert "supports_tool_calling" in cfg
    assert cfg["supports_tool_calling"] == 1
