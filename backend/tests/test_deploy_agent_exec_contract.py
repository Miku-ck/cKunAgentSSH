"""远端执行器模式表契约 — agent-exec.py 的 HARDLINE/DANGEROUS 与后端 safety.py 同源。

scripts/deploy/agent-exec.py 顶部两张表是 backend/agent/harness/safety.py 同名表的
**逐字移植**（对 run.sh 解码出的 job 内部命令做递归安全分类用）。本文件把两侧表
规范化后（pattern 源串 + 编译 flags + reason 文案）逐条比对：任何一侧单独增删改
正则或文案都会让「后端判定口径」与「远端白名单口径」漂移 —— 后端放行的命令远端
拒绝（误杀），或后端要审批的命令远端静默放行（真洞），本测试立即失败。

运行: python -m pytest backend/tests/test_deploy_agent_exec_contract.py -v
"""
import importlib.util
from pathlib import Path

import pytest

from agent.harness import safety

# 以文件路径加载 agent-exec.py（不在 backend 包内，与 test_remote_whitelist_contract 同法）
_WRAPPER_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "agent-exec.py"
)
_spec = importlib.util.spec_from_file_location("ckun_agent_exec_tables", _WRAPPER_PATH)
agent_exec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_exec)


def _normalized(table) -> list[tuple[str, int, str]]:
    """规范化条目：pattern 编译后的源串 + flags + reason。"""
    return [(p.pattern, p.flags, reason) for p, reason in table]


@pytest.mark.parametrize("table_name", ["HARDLINE_PATTERNS", "DANGEROUS_PATTERNS"])
def test_pattern_table_matches_backend(table_name):
    remote = _normalized(getattr(agent_exec, table_name))
    backend = _normalized(getattr(safety, table_name))
    assert remote == backend, (
        f"{table_name} 两侧漂移：\n"
        f"  仅 agent-exec.py 有: {sorted(set(remote) - set(backend))}\n"
        f"  仅 safety.py 有:    {sorted(set(backend) - set(remote))}\n"
        "agent-exec.py 的表必须从 backend/agent/harness/safety.py 同名表逐字移植"
        "（两侧注释里声明的契约），改任一侧必须同步另一侧。"
    )


def test_guard_actually_catches_drift():
    """自检：模拟 agent-exec 落掉后端最新一条条目，比对必须报出漂移 ——
    没有这条，规范化若写错（恒等比较），上面的契约测试就成了摆设。"""
    drifted = _normalized(agent_exec.DANGEROUS_PATTERNS[:-1])
    backend = _normalized(safety.DANGEROUS_PATTERNS)
    assert drifted != backend
    assert sorted(set(backend) - set(drifted)) == [backend[-1]]
