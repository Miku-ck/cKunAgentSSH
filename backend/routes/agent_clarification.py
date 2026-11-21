"""澄清向导路由：用户答案落库（已废弃的 skip 保留 410 壳）。

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与请求/响应契约不变。
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

router = APIRouter()

# P0-3: custom_text 最大长度限制，防止 prompt injection
_CUSTOM_TEXT_MAX_LEN = 500


class ClarificationAnswerRequest(BaseModel):
    """用户回答澄清问题的请求体（带防御性校验）"""
    run_id: str = Field(..., description="运行 ID", max_length=128)
    answers: dict = Field(
        default_factory=dict,
        description="答案字典 {step_index(key str): {choice: 'A'|'B'|'C'|'custom', custom_text?: str}}",
    )

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, v: dict) -> dict:
        """P0-3: 校验 answers 结构，拒绝畸形 step_idx 与超长 custom_text"""
        if not isinstance(v, dict):
            raise ValueError("answers 必须是 dict")
        if len(v) > 32:
            raise ValueError(f"answers 步数过多（>{32}）")
        sanitized: dict[str, dict] = {}
        for key, item in v.items():
            # step_idx 必须是可解析的 0-based 整数
            try:
                idx = int(key)
                if idx < 0:
                    raise ValueError
            except (ValueError, TypeError):
                logger.warning("非法 clarification step_idx: %r（已跳过）", key)
                continue
            if not isinstance(item, dict):
                continue
            choice = item.get("choice", "")
            if choice not in ("A", "B", "C", "custom"):
                logger.warning("非法 clarification choice: %r（已跳过）", choice)
                continue
            cleaned: dict[str, str] = {"choice": choice}
            if choice == "custom":
                txt = str(item.get("custom_text", ""))
                if len(txt) > _CUSTOM_TEXT_MAX_LEN:
                    txt = txt[:_CUSTOM_TEXT_MAX_LEN]
                cleaned["custom_text"] = txt
            sanitized[str(idx)] = cleaned
        return sanitized


@router.post("/agent/clarification/answer")
async def submit_clarification_answer(req: ClarificationAnswerRequest):
    """用户回答完澄清问题后调用 — 写入答案到 pending 存储

    审查R2修复：改 async def —— _CLARIFICATION_DONE/_CLARIFICATION_CONTEXT 与
    PermissionGate 同为「循环内消费 + 线程池写入」的进程级可变状态（澄清拦截器、
    release-run 回收都跑在事件循环上），此前本路由在 anyio 线程池里写它们存在
    跨线程竞态；回到循环后天然串行。审计文件写放线程池。

    前端 ClarifyingWizard 在用户答完所有步（或点击"完成"）后提交。
    下一轮 PRE_TOOL_CALL 时 ClarificationInterceptor 读到 _CLARIFICATION_DONE
    即注入用户答案上下文、发 clarification_done 事件、放行工具执行。
    """
    from agent.harness.reins import record_clarification_answers
    # 审计写入从 routes.agent 局部导入：测试对该模块的 append_record_async
    # 做 monkeypatch，局部导入才能拿到替换后的函数（与 agent_runs 同策略）。
    from routes.agent import append_record_async
    # answers 已由 field_validator 清洗：step_idx 合法、choice 受控、custom_text 截断
    # P1-6: 统一入口维护 done+context 配对与时间戳（TTL 可回收孤儿答案）
    record_clarification_answers(req.run_id, req.answers)
    logger.info("clarification answer: run_id=%s steps=%d", req.run_id, len(req.answers))
    # 审计：向导的两个终态必须可事后复原（answered / skipped）
    await append_record_async(
        action="clarification_answer",
        conclusion="answered",
        run_id=req.run_id,
        target_type="run",
        target_id=req.run_id,
        result=f"steps={len(req.answers)}",
    )
    return {"run_id": req.run_id, "status": "recorded", "steps": len(req.answers)}


@router.post("/agent/clarification/skip")
async def skip_clarification(req: ClarificationAnswerRequest):
    """已废弃（410 Gone）— 永久等待语义删除了「跳过」。

    用户现在只有两条路：提交答案（POST /agent/runs/{run_id}/resume），
    或取消整个任务（POST /agent/runs/{run_id}/cancel）。保留端点壳是
    为了让未升级的旧前端拿到明确的 410，而不是 404 静默失败。
    """
    raise HTTPException(
        status_code=410,
        detail="跳过澄清已下线：请提交答案（resume）或取消任务（cancel）",
    )
