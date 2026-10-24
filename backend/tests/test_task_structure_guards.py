"""长任务结构化护栏（spec 2026-10-24-task-structuring · 接缝 S1/S2）

钉死四条安全底座（都在纯函数 coerce_structure_payload 上，脱离真实模型/DB）：

1. 主机字段锁死 active target：模型返回任何主机值都被丢弃 —— 这是唯一能
   翻转 test/prod 审批结论的路径，必须硬封。
2. 写动作新颖性收敛：write + (inferred|completed) → added + requires_confirm，
   has_added_write_steps 随之为真（前端据此把 auto 审批回落 normal）。
3. plan 姿态剔除所有写步骤（对齐 resolveRunModes 的 plan=只读）。
4. 外机字面量扫描：步骤/字段出现允许集合外的主机 → 写步骤升 added 逼确认。

另钉两条跨层契约：raw_text 上限 == AgentRunRequest.task 上限；结构化端点
必须 tools=[]（纯推理，不可能执行任何东西）。

运行: python -m pytest backend/tests/test_task_structure_guards.py -v
"""
import os

import pytest

import database as db
from agent.task_structure import coerce_structure_payload


def _step(text, op_kind="read", provenance="inferred", **kw):
    return {"text": text, "op_kind": op_kind, "provenance": provenance, **kw}


def test_host_field_locked_to_active_target_regardless_of_llm():
    """模型把主机写成另一台 → 响应里 host 仍 = active_host，且只读、来源 verbatim。"""
    payload = {
        "steps": [_step("看磁盘")],
        # 模型臆测了一台 prod 机器：这是必须被丢弃的路径
        "fields": {"host": {"value": "root@evil-prod.example", "provenance": "verbatim"}},
    }
    out = coerce_structure_payload(
        payload, active_host="web-01", environment="test", posture=None,
        allowed_hosts=["web-01"],
    )
    host = out["fields"]["host"]
    assert host["value"] == "web-01", "主机字段必须锁死 active target"
    assert host["editable"] is False
    assert host["provenance"] == "verbatim"


def test_write_step_with_inferred_provenance_coerced_to_added():
    """用户没提过的写动作（模型标 inferred/completed）→ 一律收敛成 added + 需确认。"""
    payload = {"steps": [
        _step("重启 nginx", op_kind="write", provenance="inferred"),
        _step("清理日志", op_kind="write", provenance="completed"),
    ]}
    out = coerce_structure_payload(payload, active_host="web-01", environment="test",
                                   posture=None, allowed_hosts=["web-01"])
    assert all(s["provenance"] == "added" for s in out["steps"])
    assert all(s["requires_confirm"] for s in out["steps"])
    assert out["has_added_write_steps"] is True


def test_verbatim_write_step_not_forced_to_added():
    """用户原话里就有的写动作保持 verbatim，不被误伤成"需确认"（回退基准来自原文）。"""
    payload = {"steps": [_step("重启 nginx", op_kind="write", provenance="verbatim")]}
    out = coerce_structure_payload(payload, active_host="web-01", environment="test",
                                   posture=None, allowed_hosts=["web-01"])
    s = out["steps"][0]
    assert s["provenance"] == "verbatim"
    assert s["requires_confirm"] is False
    assert out["has_added_write_steps"] is False


def test_plan_posture_drops_all_write_steps():
    """plan 姿态只读：任何写步骤都被剔除，只留 read。"""
    payload = {"steps": [
        _step("看状态", op_kind="read", provenance="verbatim"),
        _step("重启服务", op_kind="write", provenance="added"),
    ]}
    out = coerce_structure_payload(payload, active_host="web-01", environment="prod",
                                   posture="plan", allowed_hosts=["web-01"])
    assert all(s["op_kind"] == "read" for s in out["steps"])
    assert out["has_added_write_steps"] is False


def test_foreign_host_in_step_text_flags_needs_confirm():
    """步骤文本出现允许集合外的主机 → 写步骤升 added 逼用户核对作用对象（不静默放行外机）。"""
    payload = {"steps": [_step("在 10.9.9.9 上执行 systemctl restart nginx",
                               op_kind="write", provenance="verbatim")]}
    out = coerce_structure_payload(payload, active_host="web-01", environment="test",
                                   posture=None, allowed_hosts=["web-01"])
    s = out["steps"][0]
    assert s["provenance"] == "added"
    assert s["requires_confirm"] is True


def test_allowed_host_token_not_flagged():
    """出现的是当前 active target 主机 → 不算外机，不升级。"""
    payload = {"steps": [_step("重启 root@web-01 上的 nginx",
                               op_kind="write", provenance="verbatim")]}
    out = coerce_structure_payload(payload, active_host="web-01", environment="test",
                                   posture=None, allowed_hosts=["web-01"])
    assert out["steps"][0]["requires_confirm"] is False


def test_unknown_op_kind_defaults_to_write_fail_closed():
    """模型没给合法 op_kind → 保守按 write 处理（宁可多问也不把写当只读放过）。"""
    payload = {"steps": [{"text": "改一下配置", "provenance": "inferred"}]}
    out = coerce_structure_payload(payload, active_host="web-01", environment="test",
                                   posture=None, allowed_hosts=["web-01"])
    s = out["steps"][0]
    assert s["op_kind"] == "write"
    assert s["requires_confirm"] is True


def test_empty_host_active_still_verbatim_and_no_execution_fields():
    """无远程目标（local）：host 空串但来源仍 verbatim；装配文本不含主机行。"""
    payload = {"steps": [_step("查本地进程", op_kind="read", provenance="verbatim")]}
    out = coerce_structure_payload(payload, active_host="", environment="test",
                                   posture=None, allowed_hosts=[])
    assert out["fields"]["host"]["value"] == ""
    assert out["fields"]["host"]["provenance"] == "verbatim"
    assert "主机：" not in out["structured_text"]


def test_raw_text_limit_matches_agent_task_limit():
    """raw_text 上限必须与 AgentRunRequest.task 同值 —— 不同步则采用后发送 422。"""
    from routes.agent import AgentRunRequest, StructureRequest
    task_max = AgentRunRequest.model_fields["task"].metadata
    raw_max = [m for m in StructureRequest.model_fields["raw_text"].metadata
               if getattr(m, "max_length", None)]
    task_len = next(m.max_length for m in task_max if getattr(m, "max_length", None))
    assert raw_max[0].max_length == task_len, (
        f"raw_text={raw_max[0].max_length} 与 task={task_len} 不一致"
    )


def test_structure_endpoint_is_pure_no_tools():
    """源码守卫：/agent/structure 调用必须是纯推理（tools=[] 且关思考），
    否则这个"预览"端点就长出了执行能力，与『不可能触达 shell/SSH』的立论相违。"""
    import inspect
    from routes import agent as agent_routes
    src = inspect.getsource(agent_routes.structure_task)
    assert "tools=[]" in src, "结构化工具面必须恒空"
    assert "enable_thinking=False" in src


def test_agent_run_request_structuring_fields_backward_compatible():
    """三个新字段必须带向后兼容默认：老客户端不传 → enhanced=False / raw=None /
    added_write=False，既有 run 行为完全不变。"""
    from routes.agent import AgentRunRequest
    req = AgentRunRequest(task="重启 nginx", conversation_id="c")
    assert req.enhanced is False
    assert req.source_raw_text is None
    assert req.enhanced_has_added_write_steps is False


def test_structure_aware_permission_mode_downgrades_auto():
    """含新增写步骤 + auto → normal；其余组合原样返回（不误伤 plan/normal）。"""
    from routes.agent import _structure_aware_permission_mode as f
    assert f(True, "auto") == "normal"
    assert f(False, "auto") == "auto"
    assert f(True, "normal") == "normal"
    assert f(True, "plan") == "plan"


@pytest.fixture
def fresh_db(tmp_workspace):
    original = db._db_path
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "structure_dual_text.db"))
    yield
    db._close_all_connections()
    if original:
        db.init_db(original)


def test_raw_and_structured_both_persisted(fresh_db):
    """审计双文本：结构稿存进 content，原文存进 agent_trace（既有 JSON 列，无迁移）。

    钉住"用户以为发的是 A、实际下达的是 B"必须可事后对照 —— 对应仓库的
    审计诚实性口径。"""
    structured = "## 目标\n- 主机：web-01\n\n## 步骤\n1. [读] 查磁盘"
    raw = "帮我看下这台慢机器顺便把nginx重启了别丢数据"
    cid = db.create_conversation("结构化会话")["id"]
    db.add_message(
        cid, "user", structured,
        agent_trace={"task_structuring": {
            "enhanced": True, "raw_text": raw, "has_added_write_steps": False,
        }},
    )
    # 直接读回 DB 列（agent_trace 是 JSON 字符串），证明双文本真的落了盘。
    row = db._get_conn().execute(
        "SELECT content, agent_trace FROM messages WHERE conversation_id = ?",
        (cid,),
    ).fetchone()
    assert row["content"] == structured, "下达的结构稿必须原样留存"
    import json as _json
    trace = _json.loads(row["agent_trace"])
    assert trace["task_structuring"]["raw_text"] == raw, "原文必须可与结构稿对照"
