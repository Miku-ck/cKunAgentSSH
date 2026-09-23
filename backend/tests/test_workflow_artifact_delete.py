"""工作流工件删除（「目标」/「计划」chip 的 ×）—— agent/workflow.py + routes/workflow.py

删除是工件生命周期里唯一的用户写操作，所以本文件的反例集中在**误放**方向：
一个能删到 base 目录之外的入参，代价（删掉别人会话的工件、甚至任意 .md）
远大于误拦（点了 × 没反应）。
"""
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.workflow as wf
from routes.workflow import delete_workflow_artifact


def _mk(base, cid, **arts):
    for kind, name in (("spec", wf._SPEC_FILE), ("plan", wf._PLAN_FILE), ("goal", wf._GOAL_FILE)):
        if kind in arts:
            (Path(base) / cid).mkdir(parents=True, exist_ok=True)
            (Path(base) / cid / name).write_text(arts[kind], encoding="utf-8")


# ── delete_artifact：入参白名单与幂等 ──

def test_delete_goal_falls_back_to_lower_phase(tmp_workspace):
    """删掉 goal.md 后阶段回落到已存在的最高阶 —— chip 的 × 有连带后果，必须可见。"""
    _mk(tmp_workspace, "c1", plan="步骤", goal="成功标准")
    assert wf.get_phase("c1", tmp_workspace) == "goal"
    assert wf.delete_artifact("c1", "goal", tmp_workspace) is True
    assert wf.get_phase("c1", tmp_workspace) == "plan"


def test_delete_plan_falls_back_to_spec(tmp_workspace):
    """「计划」chip 的 × 走同一函数：plan.md 删掉后阶段回落到 spec。
    与 goal 那条分开写，是因为两枚 chip 各自只删自己的 kind —— 计划不该连带删掉目标。"""
    _mk(tmp_workspace, "c1", spec="规格", plan="步骤")
    assert wf.get_phase("c1", tmp_workspace) == "plan"
    assert wf.delete_artifact("c1", "plan", tmp_workspace) is True
    assert wf.get_phase("c1", tmp_workspace) == "spec"
    # 只动了自己那一份
    assert (Path(tmp_workspace) / "c1" / wf._SPEC_FILE).exists()


def test_delete_is_idempotent(tmp_workspace):
    """文件不存在返回 False 而非抛错：重复点击/前端重进面板都不该报错。"""
    assert wf.delete_artifact("c1", "goal", tmp_workspace) is False
    _mk(tmp_workspace, "c1", goal="g")
    assert wf.delete_artifact("c1", "goal", tmp_workspace) is True
    assert wf.delete_artifact("c1", "goal", tmp_workspace) is False


def test_delete_rejects_unknown_kind(tmp_workspace):
    """kind 只认 spec/plan/goal —— 它决定拼进路径的文件名，不能接受任意值。"""
    _mk(tmp_workspace, "c1", goal="g")
    for junk in ("goal.md", "../goal", "", "spec.md", "database"):
        assert wf.delete_artifact("c1", junk, tmp_workspace) is False
    assert wf.get_phase("c1", tmp_workspace) == "goal"  # 一个都没删掉


def test_delete_cannot_escape_base_dir(tmp_workspace):
    """会话 id 里的 .. 不得越出 base：这是本端点的主风险（路径穿越）。"""
    outside = Path(tmp_workspace) / "outside"
    outside.mkdir()
    (outside / "goal.md").write_text("别人的目标", encoding="utf-8")
    base = Path(tmp_workspace) / "wf"
    base.mkdir()
    assert wf.delete_artifact("../outside", "goal", base) is False
    assert (outside / "goal.md").read_text(encoding="utf-8") == "别人的目标"


def test_delete_requires_conversation_id(tmp_workspace):
    assert wf.delete_artifact("", "goal", tmp_workspace) is False


def test_no_model_facing_artifact_delete_tool():
    """删除**不得**有模型侧入口 —— 这是设计 A 硬门的前提（spec §5.4）。

    一旦模型能删工件，`plan_write` 一个空内容就是"取消批准"，批准门形同虚设。
    这里钉住 workflow 工具模块的对外面：只有三个写工具，且模块不引用删除函数。
    新增任何工具都会让这条变红，逼作者重读一遍这个不变量再更新清单。
    """
    import inspect
    import agent.tools.builtin.workflow_tools as wt

    src = Path(inspect.getsourcefile(wt)).read_text(encoding="utf-8")
    names = {
        obj.name for obj in vars(wt).values()
        if inspect.isclass(obj) and getattr(obj, "name", None)
    }
    assert names == {"spec_write", "plan_write", "goal_write"}
    assert "delete_artifact" not in src
    # 写入骨架拒绝空内容：否则 "plan_write('')" 就是那条逃逸口
    assert "内容不能为空" in src


# ── 路由层 ──
# 直接 await 路由协程，不走 TestClient：本仓库的沙箱在 TestClient 建事件循环上
# 偶发挂死（RC=124、无失败摘要），而 pytest-asyncio 的 1000+ 条同风格用例稳定。

@pytest.fixture
def base(tmp_workspace):
    b = Path(tmp_workspace) / "wf"
    b.mkdir()
    return b


async def _delete(cid, kind):
    """调用路由；HTTPException 原样返回，交给断言判 status_code。"""
    try:
        return await delete_workflow_artifact(conversation_id=cid, kind=kind)
    except HTTPException as e:
        return e


@pytest.mark.asyncio
async def test_route_returns_phase_after_delete(base, monkeypatch):
    """响应带回删除后的 phase：删掉 goal 会回落到 spec，前端据此提示连带后果。"""
    monkeypatch.setattr(wf, "_DEFAULT_BASE_DIR", base)
    _mk(base, "c1", spec="规格", goal="目标")
    r = await _delete("c1", "goal")
    assert isinstance(r, dict), r
    # spec 还在 → 回落到 spec，而不是报告"已无目标"这种无信息量的话
    assert r == {"deleted": True, "phase": "spec"}


@pytest.mark.asyncio
async def test_route_rejects_bad_kind(base, monkeypatch):
    """400 而不静默成功：静默会让一个拼错的 kind 看起来像"删了但没删掉"。"""
    monkeypatch.setattr(wf, "_DEFAULT_BASE_DIR", base)
    _mk(base, "c1", goal="目标")
    for junk in ("goal.md", "../../etc/passwd", "", "any"):
        r = await _delete("c1", junk)
        assert getattr(r, "status_code", None) == 400, (junk, r)
    assert wf.get_phase("c1", base) == "goal"


@pytest.mark.asyncio
async def test_route_requires_conversation_id(base, monkeypatch):
    monkeypatch.setattr(wf, "_DEFAULT_BASE_DIR", base)
    r = await _delete("", "goal")
    assert getattr(r, "status_code", None) == 400
