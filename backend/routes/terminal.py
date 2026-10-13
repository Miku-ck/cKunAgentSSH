"""
终端预测路由 — AI 智能预测下一步命令

POST /api/terminal/predict — 根据终端上下文预测下一步可能输入的命令
"""
import asyncio
import json
import logging
from contextlib import aclosing

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db
from database import get_model_config as _db_get_model_config_raw

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

# 审查修复（P2 E8）：LLM 调用加整体超时 — 原实现流式读取无上限时间保护，
# LLM 服务挂起或慢响应时该端点永久阻塞，连接池耗尽后影响所有 FastAPI 请求
# （包括健康检查）。15s 对预测类短回复已足够。
# 提成常量是为了让测试能把流挂起路径的等待压到毫秒级
_LLM_TIMEOUT_S = 15.0


def _get_model_config(model_id: str):
    """获取模型配置（含真实 api_key）"""
    return _db_get_model_config_raw(model_id, mask_key=False)


# ── Request / Response Models ──

class TerminalPredictRequest(BaseModel):
    """终端预测请求"""
    # 会话 ID（本地为 ptyId，远程为 sshId）
    session_id: str = Field(..., min_length=1)
    # 当前已输入的命令前缀（可能为空）
    current_input: str = ""
    # 最近执行的命令历史（最近 N 条，按时间正序）
    # 审查修复（P2 E8）：加上限 — 原实现无约束，渲染进程传数万条历史
    # 全量反序列化到内存，再构建 LLM prompt（每条都进 context_parts），
    # 极端情况下撑爆请求体 + prompt。10 条已足够预测上下文
    recent_commands: list[str] = Field(default_factory=list, max_length=50)
    # 当前工作目录
    cwd: str = ""
    # 操作系统类型
    platform: str = "linux"  # linux / windows / macos
    # 使用的模型 ID（可选，默认使用 SSH 模式默认模型）
    model_id: str = ""


class TerminalPredictResponse(BaseModel):
    """终端预测响应"""
    predictions: list[str] = Field(default_factory=list)
    model_used: str = ""


# ── 系统提示词 ──

TERMINAL_PREDICT_SYSTEM_PROMPT = """你是一个终端命令预测助手。根据用户最近的命令历史、当前工作目录和已输入的前缀，预测用户接下来最可能输入的命令。

规则：
1. 只输出命令本身，不要解释、不要 markdown 格式、不要引号
2. 每行一条命令，最多 5 条
3. 按可能性从高到低排序
4. 命令应该是可执行的完整命令（不含注释）
5. 考虑命令之间的逻辑关联（如 git status 后可能是 git diff / git add / git commit）
6. 考虑当前工作目录的上下文
7. 如果无法预测，返回空列表

输出格式（JSON）：
{"predictions": ["command1", "command2", ...]}
"""


# ── 预测路由 ──

@router.post("/terminal/predict")
async def predict_terminal_command(req: TerminalPredictRequest):
    """预测终端下一步命令"""
    # 获取模型配置
    model_id = req.model_id
    if not model_id:
        # 审查 M-13：db 同步调用放线程池，避免阻塞事件循环
        default_model = await asyncio.to_thread(db.get_default_model, 'ssh')
        if not default_model:
            default_model = await asyncio.to_thread(db.get_default_model, None)
        if not default_model:
            raise HTTPException(status_code=400, detail="未配置默认模型，请先设置模型")
        model_id = default_model.get("id") or default_model.get("model_id")

    model_cfg = await asyncio.to_thread(_get_model_config, model_id)
    if not model_cfg:
        raise HTTPException(status_code=400, detail=f"Unknown model_id: {model_id}")

    # 构建用户消息
    context_parts = []
    if req.cwd:
        context_parts.append(f"当前工作目录: {req.cwd}")
    if req.platform:
        context_parts.append(f"操作系统: {req.platform}")
    if req.recent_commands:
        recent = req.recent_commands[-10:]  # 最多取最近 10 条
        context_parts.append("最近命令历史:\n" + "\n".join(f"  {i+1}. {cmd}" for i, cmd in enumerate(recent)))
    if req.current_input:
        context_parts.append(f"当前已输入: {req.current_input}")

    if not context_parts:
        return TerminalPredictResponse(predictions=[], model_used=model_id)

    context_text = "\n".join(context_parts)
    user_message = f"""根据以下终端上下文，预测用户接下来最可能输入的命令：

{context_text}

请以 JSON 格式返回预测结果。"""

    # 调用 LLM
    messages = [
        {"role": "system", "content": TERMINAL_PREDICT_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    # 推理参数
    temperature = model_cfg.get("temperature", 0.3)  # 预测用较低温度
    top_p = model_cfg.get("top_p", 0.9)
    max_tokens = 256  # 预测结果很短，不需要太多 token

    full_content = ""
    try:
        # aclosing：超时取消时生成器停在半途，不显式关闭就要等 GC ——
        # llm_adapter 的 _client_in_flight 计数不归零，会挡住客户端 LRU 回收
        from llm_adapter import chat_stream
        async def _read_stream() -> str:
            parts: list[str] = []
            async with aclosing(chat_stream(
                base_url=model_cfg["base_url"],
                api_key=model_cfg["api_key"],
                model=model_cfg.get("model_id") or model_cfg["id"],
                messages=messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )) as chunk_iter:
                async for chunk in chunk_iter:
                    if chunk.content:
                        parts.append(chunk.content)
            return "".join(parts)

        full_content = await asyncio.wait_for(_read_stream(), timeout=_LLM_TIMEOUT_S)

    except asyncio.TimeoutError:
        logger.warning("Terminal predict LLM call timed out (%ss)", _LLM_TIMEOUT_S)
        return TerminalPredictResponse(predictions=[], model_used=model_id)
    except Exception as e:
        logger.warning(f"Terminal predict LLM call failed: {e}")
        return TerminalPredictResponse(predictions=[], model_used=model_id)

    # 解析 JSON 响应
    predictions = _parse_predictions(full_content)
    return TerminalPredictResponse(predictions=predictions, model_used=model_id)


def _parse_predictions(content: str) -> list[str]:
    """从 LLM 输出中解析预测命令列表"""
    if not content:
        return []

    # 尝试直接解析 JSON
    try:
        data = json.loads(content.strip())
        if isinstance(data, dict) and "predictions" in data:
            preds = data["predictions"]
            if isinstance(preds, list):
                return [str(p).strip() for p in preds if p and str(p).strip()][:5]
    except json.JSONDecodeError:
        pass

    # 回退：逐行解析
    lines = content.strip().split("\n")
    predictions = []
    for line in lines:
        line = line.strip()
        # 去除序号前缀（如 "1. " "- " "* "）
        if line and line[0] in "123456789" and len(line) > 2 and line[1:3] in [". ", ") "]:
            line = line[3:].strip()
        elif line.startswith("- ") or line.startswith("* "):
            line = line[2:].strip()
        # 去除引号
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
        if line and not line.startswith("{") and not line.startswith("["):
            predictions.append(line)
        if len(predictions) >= 5:
            break

    return predictions
