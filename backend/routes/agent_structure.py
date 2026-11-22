"""Agent 任务结构化路由：长需求 → 结构化草稿（纯推理，无工具）。

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与请求/响应契约不变：
POST /agent/structure
"""
import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db
from prompts import SSH_MODE
from routes.terminal import _get_model_config

logger = logging.getLogger(__name__)
router = APIRouter()


class StructureTarget(BaseModel):
    """当前 active target —— 结构化时主机字段的唯一来源（前端从 useHomeTarget 取）。"""
    host: str = ""
    session_type: Literal["local", "remote"] = "local"


class StructureRequest(BaseModel):
    """POST /agent/structure —— 长任务结构化的纯推理请求（发送前可选旁路）。

    raw_text 上限与 AgentRunRequest.task 同值：改一处须同步另一处与前端
    TASK_CHAR_LIMIT，否则要么结构化收不下、要么采用后发送 422。"""
    raw_text: str = Field(..., min_length=1, max_length=50000)
    active_target: StructureTarget = StructureTarget()
    # 仅作展示提示（前端读当前主机判定）；结构化端点不据此放行任何操作，
    # 真正的 prod 审批强化仍发生在下游真实 run（见 task_structure §0 不变量）。
    environment: Literal["test", "prod"] = "test"
    # 已连接主机白名单：结构化文本里出现集合外的主机 = 臆测 → 逼用户确认。
    allowed_hosts: list[str] = []
    model_id: str = ""


def _extract_json_object(text: str) -> Optional[dict]:
    """从 LLM 正文里取第一个 JSON 对象：容忍 ```json 围栏与前后解释文字。"""
    import json as _json
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        # 去掉可能的 json 语言标注行
        if s.lower().startswith("json"):
            s = s[4:]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = _json.loads(s[start:end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


@router.post("/agent/structure")
async def structure_task(req: StructureRequest):
    """把一段较长的模糊运维需求拆成结构化草稿（纯推理，无工具，不执行任何东西）。

    spec 2026-10-24-task-structuring · 接缝 S2。

    两道护栏互相兜底：① 系统提示词让模型「绝不自定主机、写动作非原话即 added」；
    ② coerce_structure_payload 无论模型是否听话都硬锁主机字段、收敛写步骤、
    plan 姿态去写、扫描外机。第二道是可单测的安全底座（见 test_task_structure_*）。

    刻意只 tools=[]、enable_thinking=False：这是一次无工具 LLM 推理，本端点不可能
    触达 shell/SSH/写文件 —— 它是发送前的可选旁路，不是发送前置条件（失败前端降级，
    原稿照发）。"""
    from agent.task_structure import (
        _MAX_STRUCTURE_TOKENS,
        build_structure_system_prompt,
        coerce_structure_payload,
    )

    # 模型解析与 run 同一口径：显式 model_id 优先，否则 SSH 档默认再全局默认。
    effective_model_id = req.model_id
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, SSH_MODE)
        if default_model:
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, None)
        if default_model:
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        raise HTTPException(status_code=400, detail="未配置默认模型，无法结构化")

    model_cfg = await asyncio.to_thread(_get_model_config, effective_model_id)
    if not model_cfg:
        raise HTTPException(status_code=404, detail=f"模型不存在: {effective_model_id}")

    system_prompt = build_structure_system_prompt(req.active_target.host, req.environment)
    from agent.harness.llm_client import StreamLLMClient

    llm = StreamLLMClient(
        base_url=model_cfg["base_url"],
        api_key=model_cfg["api_key"],
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": req.raw_text},
    ]
    try:
        out = await asyncio.wait_for(
            llm.call(
                messages=messages,
                tools=[],  # 空工具面 = 纯推理：本端点不可能执行任何操作
                model=model_cfg.get("model_id") or model_cfg.get("name", ""),
                temperature=0.2,
                max_tokens=_MAX_STRUCTURE_TOKENS,
                enable_thinking=False,
                top_p=model_cfg.get("top_p", 0.9),
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="结构化超时，请直接发送原文")
    except Exception as e:
        logger.warning("结构化 LLM 调用失败: %s", e)
        raise HTTPException(status_code=502, detail="结构化失败，原文可照常发送") from e

    payload = _extract_json_object(out.content)
    if payload is None:
        # 失败诚实上报：不返回"空结构"骗前端当成功，前端据此降级保留原稿可发。
        raise HTTPException(status_code=502, detail="结构化返回无法解析，原文可照常发送")

    coerced = coerce_structure_payload(
        payload,
        active_host=req.active_target.host,
        environment=req.environment,
        allowed_hosts=req.allowed_hosts,
    )
    coerced["meta"] = {
        "model_used": model_cfg.get("model_id") or model_cfg.get("name", ""),
        "tokens_in": out.prompt_tokens,
        "tokens_out": out.completion_tokens,
    }
    return coerced
