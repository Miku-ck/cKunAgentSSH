"""运维专家建议谓词守卫：触发/不触发边界 + 只读运维覆盖。

关键词级规则（复用变更动词表 + 运维名词小词表），不是新分类器。
证伪：让失败路径抛异常 → 调用方静默 no-op 守卫红（见路由侧）。
"""
import pytest

from agent.expert_suggest import should_suggest_ops


@pytest.mark.parametrize("task", [
    "重启 nginx",          # 变更动词
    "看下磁盘空间",        # 只读运维（名词命中，变更动词不命中）
    "查一下端口占用",
    "docker 容器挂了",
    "清理日志",
])
def test_ops_signal_triggers(task):
    assert should_suggest_ops(task, expert_selected=False, is_conversation=False)


def test_selected_expert_suppresses():
    assert not should_suggest_ops("重启 nginx", expert_selected=True, is_conversation=False)


def test_conversation_suppresses():
    assert not should_suggest_ops("你好", expert_selected=False, is_conversation=True)


@pytest.mark.parametrize("task", ["写一首诗", "帮我写周报", "翻译这段英文", ""])
def test_non_ops_task_does_not_trigger(task):
    assert not should_suggest_ops(task, expert_selected=False, is_conversation=False)


def test_none_task_safe():
    assert not should_suggest_ops(None, expert_selected=False, is_conversation=False)