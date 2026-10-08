"""S7-06 回归：schedule_op 的授权键粒度必须收到 action 级。

schedule_op 是多动作工具（create|list|cancel），而 gate 的授权键在拿不到「命令」
时退化成**裸工具名**。原先只有 `create` 且带 command 时 bound_command 才返回值，
于是 list / cancel / 只给 task_prompt 的 create 全都落在裸工具名那一支 —— 用户对
一张查询卡片点「始终允许」，永久放行的是该工具的全部 action，包括删掉手工配好的
任务。headless run 用同一份 registry，无人值守的定时 run 因此也能增删 schedules。
"""
from agent.harness.reins.gate import PermissionGate
from agent.tools.builtin.ops_schedule import ScheduleOpTool


def _key(args: dict) -> str:
    return PermissionGate._grant_key("schedule_op", ScheduleOpTool.bound_command(args))


def test_every_action_gets_its_own_grant_key():
    list_key = _key({"action": "list"})
    cancel_key = _key({"action": "cancel", "schedule_id": "sch-1"})
    other_cancel_key = _key({"action": "cancel", "schedule_id": "sch-2"})
    create_prompt_key = _key({"action": "create", "task": "每天巡检磁盘水位"})

    assert list_key and cancel_key and other_cancel_key and create_prompt_key
    assert len({list_key, cancel_key, other_cancel_key, create_prompt_key}) == 4, (
        "四个动作共键 = 一次「始终允许」放行 schedule_op 全部动作"
    )


def test_list_grant_does_not_exempt_mutating_actions():
    """`list` 刻意不绑标的（它不改动任何东西，风险判据来自 ToolMeta moderate，
    见 ops_schedule.bound_command 的注释），于是它的授权键就是裸工具名那一支。

    这在本工具上**不构成漏洞**：会改动调度的两个动作都已经绑到自己的标的，键与
    裸工具名不同 ⇒ 在查询卡片上点「始终允许」放行不到「删掉手工配好的任务」。
    钉这个不变式，而不是钉「三个 action 都不许落裸键」——后者会把 list 的判据
    从 ToolMeta 挤进命令分类兜底（得到「unknown command, default approval」）。
    """
    list_key = _key({"action": "list"})
    assert list_key == "schedule_op"
    for args in ({"action": "cancel", "schedule_id": "sch-1"},
                 {"action": "create", "task": "巡检"},
                 {"action": "create", "command": "du -sh /var/log"}):
        assert _key(args) != list_key, f"{args} 与 list 共键：批查询的卡片放行了改动动作"

    # cancel 不带 id 时无标的可绑 ⇒ 落裸键，但执行侧先把它拒了
    # （ops_schedule._cancel: "cancel 需要提供 schedule_id 参数"），不会真删东西
    assert _key({"action": "cancel"}) == list_key


def test_cancel_is_declared_destructive_for_prod():
    """prod 升级读 meta.destructive_actions（gate 按 args["action"] 命中）。"""
    assert "cancel" in ScheduleOpTool.meta.destructive_actions, (
        "不声明就没人把「删掉用户配好的定时任务」当 dangerous 逐次询问"
    )
    assert "list" not in ScheduleOpTool.meta.destructive_actions
