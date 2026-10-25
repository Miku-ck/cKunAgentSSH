"""设置模块 round 23：启动回填 / 定时任务取模型 / 保留条数读取的回归。

三条都是「写侧正确、读侧或另一条路径没接上」的同族缺陷：
- post_process 的四个开关：PUT 会推内存全局，启动回填只塞给 Manager 构造参数，
  而 submit() 每次都把构造快照换成模块全局 —— 重启即回到「全开」。
- 定时任务读的是全局默认档，设置页的 ★ 说明却写着「对话/终端/定时任务读这档」。
- conv_retention.keep_count 是所有数值设置里唯一读取不校界的一个，而它流向
  `DELETE ... WHERE rn > ?`。
"""
import os

import pytest


@pytest.fixture
def base_db(tmp_workspace):
    import database as db
    db.init_db(os.path.join(tmp_workspace, "settings23.db"))
    db._close_all_connections()
    yield db
    db._close_all_connections()


# ── 1. 后处理启动回填必须进模块全局 ──


def test_init_manager_pushes_config_into_module_global(base_db):
    """main.py 的启动路径与面板 PUT 共用同一份 _config。

    旧行为：init_post_process_manager(cfg) 只把它存进 Manager 实例，模块全局仍是
    dataclass 默认（全 True），GET 报 false 而 submit 照跑 —— 面板与运行时相反。
    """
    import post_processor as pp
    original = pp.get_post_process_config()
    try:
        cfg = pp.PostProcessConfig(
            enabled=False, generate_title=False, extract_memory=False, generate_skill=False)
        mgr = pp.init_post_process_manager(cfg)
        assert pp.get_post_process_config().enabled is False, \
            "启动回填没进模块全局，submit() 会把它换回 dataclass 默认"
        assert mgr.config.enabled is False
    finally:
        pp.set_post_process_config(original)


def test_submit_stays_closed_after_boot_backfill(base_db):
    """按 main.py 的形状回填后，第一次 submit 就不该放行任务。

    只验「关闭」这一半：放行的一支会真的起后台任务去打 LLM，在这个沙箱里
    连 socketpair 都会挂死，与本用例要验的接线无关。
    """
    import asyncio
    import post_processor as pp
    original_cfg = pp.get_post_process_config()
    original_manager = pp._manager
    try:
        base_db.set_app_setting("post_process.enabled", "false")
        pp.init_post_process_manager(pp.load_post_process_config_from_db())
        assert asyncio.run(pp.get_post_process_manager().submit(
            conversation_id="c", user_message="q", assistant_content="a")) == "", \
            "库里写着关闭，重启回填后 submit 仍放行任务"
    finally:
        pp._manager = original_manager
        pp.set_post_process_config(original_cfg)


# ── 2. 定时任务按 ★ SSH 档取模型 ──


def test_schedule_model_prefers_ssh_default(base_db):
    """★ 与 ⊕ 指向不同行时，定时任务必须用 ★ 那档（与设置页文案同口径）。"""
    from agent.schedule_engine import ScheduleEngine

    ssh = base_db.create_model_config(
        name="SSH档", base_url="https://ssh.example/v1", model_id="deepseek-ssh")
    glob = base_db.create_model_config(
        name="全局档", base_url="https://glob.example/v1", model_id="qwen-glob")
    base_db.set_default_model(ssh["id"], mode="ssh")
    base_db.set_default_model(glob["id"], mode="global")

    resolved = ScheduleEngine._resolve_model()
    assert resolved["id"] == ssh["id"], \
        "定时任务读的是全局档：用户只设了 ★ 却跑到 ⊕ 那条模型上"
    # 反向锁：旧读法（只取全局档）在这同一份数据上取到的是 glob 行 ——
    # 少了这一行，用例可能只是在断言一个恰好成立的顺序。
    assert base_db.get_default_model(None, mask_key=False)["id"] == glob["id"]


def test_schedule_model_falls_back_to_global(base_db):
    """SSH 档没设时回落到全局档，而不是直接报「未配置默认模型」。"""
    from agent.schedule_engine import ScheduleEngine

    glob = base_db.create_model_config(
        name="全局档", base_url="https://glob.example/v1", model_id="qwen-glob")
    base_db.set_default_model(glob["id"], mode="global")
    assert ScheduleEngine._resolve_model()["id"] == glob["id"]


def test_schedule_model_none_when_no_default(base_db):
    from agent.schedule_engine import ScheduleEngine
    assert ScheduleEngine._resolve_model() is None


# ── 3. 保留条数读取校界 ──


@pytest.mark.parametrize("raw,expected", [
    ("1000", 1000),
    ("0", 0),            # 0 是合法的「不限制」哨兵，必须原样读出
    ("-1", 1000),        # 越界回默认，不钳到边界
    ("99999999", 1000),  # 越界回默认：这条值会进 DELETE ... WHERE rn > ?
    ("abc", 1000),
])
def test_keep_count_read_is_bounds_aware(base_db, raw, expected):
    base_db.set_app_setting("conv_retention.keep_count", raw)
    assert base_db.get_conv_retention()["keep_count"] == expected


def test_keep_count_out_of_range_row_is_visible_but_rejected(base_db):
    """反向锁：脏行真的在库里（旧读取会原样放出去喂给 DELETE），新读取回默认。"""
    base_db.set_app_setting("conv_retention.keep_count", "99999999")
    assert int(base_db.get_app_setting("conv_retention.keep_count")) == 99999999
    assert base_db.get_conv_retention()["keep_count"] == 1000


def test_keep_count_zero_does_not_delete(base_db):
    """0 = 一条都不删（面板文案口径已按此改）。曾经写着 0 = 清空全部会话。"""
    for i in range(3):
        base_db.create_conversation(f"会话{i}")
    assert base_db.prune_conversations(keep_count=0) == []
    assert len(base_db.list_conversations(limit=50)) == 3


# ── 4. 超时下限常量与「0 = 不覆盖」哨兵同口径 ──


def test_timeout_min_matches_sentinel():
    import netconfig
    assert netconfig.TIMEOUT_MIN == 0, \
        "下限若不为 0，set_policy 会把用户显式写的 0（= 不覆盖）夹成别的值"
    assert netconfig.set_policy("", 0, 1)["timeout_seconds"] == 0


# ── 6. 模型行的 top_p 要真的走到上游请求 ──


@pytest.mark.asyncio
async def test_llm_client_forwards_top_p(monkeypatch):
    """StreamLLMClient.call 把 top_p 交给 chat_stream。

    这一层此前根本没有 top_p 形参，chat_stream 用自己的 0.9 默认 —— 设置页改了
    top_p，Agent 运行一路都是 0.9，而且两端各自有测试，谁都不红。
    """
    import agent.harness.llm_client as lc_mod

    seen: dict = {}

    def fake_chat_stream(**kwargs):
        seen.update(kwargs)

        async def gen():
            if False:  # pragma: no cover —— 只为造出异步生成器
                yield None
        return gen()

    monkeypatch.setattr("llm_adapter.chat_stream", fake_chat_stream)
    client = lc_mod.StreamLLMClient(base_url="https://x.invalid/v1", api_key="k")
    await client.call(
        messages=[{"role": "user", "content": "hi"}],
        tools=[], model="m", temperature=0.3, max_tokens=100,
        enable_thinking=False, top_p=0.11,
    )
    assert seen.get("top_p") == 0.11, f"top_p 没传到 chat_stream：{sorted(seen)}"


def test_agent_config_rebounds_out_of_range_model_params():
    """脏行/越界的 temperature、top_p 在唯一入口回落默认，而不是发出 400。"""
    from agent.harness.contracts import AgentConfig

    ok = AgentConfig(temperature=0.0, top_p=0.0)
    assert (ok.temperature, ok.top_p) == (0.0, 0.0), "0 是合法读数，不能被当成未设置"
    bad = AgentConfig(temperature=5.0, top_p="abc")
    assert (bad.temperature, bad.top_p) == (0.7, 0.9)
    nan = AgentConfig(temperature=float("nan"), top_p=1.5)
    assert (nan.temperature, nan.top_p) == (0.7, 0.9)


# ── 7. 后处理与面板共用一份 DB 读数（回归 round 22 之外的那半）──


def test_post_process_put_and_boot_read_same_row(base_db):
    """PUT 之后重启回填，必须读到用户刚写的那一行。"""
    import post_processor as pp
    original_cfg = pp.get_post_process_config()
    original_manager = pp._manager
    try:
        base_db.update_app_settings_bulk({"post_process.generate_title": "false"})
        assert pp.load_post_process_config_from_db().generate_title is False
        pp.init_post_process_manager(pp.load_post_process_config_from_db())
        assert pp.get_post_process_config().generate_title is False
    finally:
        pp._manager = original_manager
        pp.set_post_process_config(original_cfg)


# ── 5. 兜底时长的「用户写过」判定要用带区间的谓词 ──


def test_dirty_fallback_row_does_not_lock_out_env(base_db):
    """手工写坏的 permission.approval_fallback_s 不算用户设定。

    main.py 启动回填此前用「这一行存在」判定，一个 "abc" 脏行就会把
    CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S 永久压掉。
    """
    base_db.set_app_setting("permission.approval_fallback_s", "abc")
    _value, explicit = base_db.is_int_app_setting_set(
        "permission.approval_fallback_s", 600, low=0, high=86400)
    assert explicit is False, "脏行被当作「用户写过」，env 覆盖就此失效"
    assert _value == 600

    base_db.set_app_setting("permission.approval_fallback_s", "0")
    value, explicit = base_db.is_int_app_setting_set(
        "permission.approval_fallback_s", 600, low=0, high=86400)
    assert (value, explicit) == (0, True), "显式 0（永久等待）必须算用户设定"
