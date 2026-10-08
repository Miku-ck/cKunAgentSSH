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


def test_bare_tool_name_key_is_gone():
    """裸工具名那一支（bound_command=None）对三个正常 action 都不该再出现。"""
    for args in ({"action": "list"},
                 {"action": "cancel", "schedule_id": "sch-1"},
                 {"action": "create", "task": "巡检"}):
        assert _key(args) != "schedule_op", f"{args} 仍退化成工具级授权"


def test_cancel_is_declared_destructive_for_prod():
    """prod 升级读 meta.destructive_actions（gate 按 args["action"] 命中）。"""
    assert "cancel" in ScheduleOpTool.meta.destructive_actions, (
        "不声明就没人把「删掉用户配好的定时任务」当 dangerous 逐次询问"
    )
    assert "list" not in ScheduleOpTool.meta.destructive_actions
