"""S7-17 回归：cron 的报错文案不许承诺校验器不认的东西。

原文案写「必须是数字或 '*'」，而 '*' 走到 `_is_digits` 必定为假 ⇒ 被拒。
模型（和人）照着指引重试 `* 9 * * *` 会撞回同一句自相矛盾的报错，原地打转。
min/hour 这两个字段确实没有 '*' 的合法形状（daily/weekly/monthly 都要固定时分），
所以该改的是文案，并把真正的出路（mode=interval）一起说清楚。
"""
import pytest

from agent.tools.builtin import ops_schedule


@pytest.mark.parametrize("cron,label", [
    ("* 9 * * *", "min"),
    ("30 * * * *", "hour"),
    ("* * * * 5", "min"),
])
def test_star_is_rejected_without_promising_it(cron, label):
    data, err = ops_schedule._parse_cron(cron)
    assert data is None
    assert err is not None
    assert f"{label} 必须是数字" in err
    # 关键：不能再出现「或 '*'」这种自相矛盾的邀请
    assert "或 '*'" not in err
    assert "mode=interval" in err, err


def test_the_supported_shapes_still_parse():
    assert ops_schedule._parse_cron("* * * * *") == (
        {"schedule_type": "daily", "time_hour": 0, "time_minute": 0,
         "weekday": 0, "day_of_month": 1}, None)
    daily, err = ops_schedule._parse_cron("30 9 * * *")
    assert err is None and daily["time_minute"] == 30 and daily["time_hour"] == 9
    weekly, err = ops_schedule._parse_cron("0 3 * * 1")
    assert err is None and weekly["schedule_type"] == "weekly"
    monthly, err = ops_schedule._parse_cron("15 2 5 * *")
    assert err is None and monthly["schedule_type"] == "monthly"


@pytest.mark.parametrize("cron", [
    "30 9 * * x",      # dow 位
    "30 9 32 * *",     # dom 越界
    "70 9 * * *",      # min 越界
    "30 25 * * *",     # hour 越界
])
def test_no_emitted_message_invites_a_star(cron):
    """其余分支的文案同族 —— 逐条走一遍真正**发出去**的消息。

    刻意不扫模块源码：那条注释本身就在解释旧文案，扫源码的守卫会把自己的说明当违规。
    """
    data, err = ops_schedule._parse_cron(cron)
    assert data is None
    assert err
    assert "或 '*'" not in err
