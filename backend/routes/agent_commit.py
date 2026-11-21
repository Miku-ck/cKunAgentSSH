"""Agent 提交信息生成路由：变更清单 → conventional commits（纯推理，无工具）。

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与请求/响应契约不变：
POST /agent/commit-message
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db
from prompts import SSH_MODE
from routes.terminal import _get_model_config

logger = logging.getLogger(__name__)
router = APIRouter()


class CommitMessageRequest(BaseModel):
    """POST /agent/commit-message —— 提交信息生成的纯推理请求（无工具，不触达仓库）。

    输入 = 仓库名 / 分支 / 变更文件清单（带 porcelain 状态前缀）+ 可选 diff 摘要
    （选中文件对 HEAD 的 unified diff，渲染层已按 clampDiffText 截断）；模型只做
    文字归纳，这里再钉一层字段上限。"""
    repo: str = Field(..., min_length=1, max_length=200)
    branch: str = Field("", max_length=256)
    files: list[str] = Field(..., min_length=1, max_length=500)
    diff: str = Field("", max_length=8000)


def _sanitize_commit_message(text: str) -> str:
    """LLM 输出 → 合法单行提交信息：剥围栏行/引号、取首行、截断到 200（与
    shared/git-repos-core.isValidCommitMsg 同口径，主进程提交前还会复核）。"""
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.split("\n")
        lines = lines[1:]  # 首行围栏（含语言标注整行）
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    first = s.split("\n", 1)[0].strip().strip('`"“”').strip()
    if len(first) > 200:
        first = first[:200].rstrip()
    return first


@router.post("/agent/commit-message")
async def generate_commit_message(req: CommitMessageRequest):
    """按变更文件清单生成 conventional commits 提交信息（纯推理，无工具）。

    与 /agent/structure 同一管线与护栏：tools=[] + enable_thinking=False ——
    本端点不可能执行任何操作，只是文字归纳。失败返回 4xx/5xx，前端降级为
    规则草稿或手写，不阻塞提交。"""
    # 模型解析：默认（SSH 档 > 全局档）> 第一个可用模型 —— 库里有模型但没标默认时
    # 不强迫用户先去设置页打标（提交信息生成是可选旁路，降级路径本就存在）；
    # 一个模型都没有才要求先配置。
    cfg = await asyncio.to_thread(db.get_default_model, SSH_MODE) or await asyncio.to_thread(db.get_default_model, None)
    effective_model_id = (cfg or {}).get("id") or (cfg or {}).get("model_id")
    if not effective_model_id:
        models = await asyncio.to_thread(db.list_models_config)
        if not models:
            raise HTTPException(
                status_code=400,
                detail="还没有可用的 AI 模型：请到 设置 → AI 模型 添加并保存，然后重试",
            )
        effective_model_id = models[0].get("id") or models[0].get("model_id")

    model_cfg = await asyncio.to_thread(_get_model_config, effective_model_id)
    if not model_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"模型配置已失效（{effective_model_id}）：请到 设置 → AI 模型 检查后重试",
        )

    listing = "\n".join(f[:200] for f in req.files)
    system_prompt = (
        "你是 git 提交信息生成器。根据变更文件清单（及提供的 diff 内容，若有）写一条 "
        "conventional commits 风格的提交信息：格式为 type(scope): 描述 或 type: 描述；"
        "type 从 feat/fix/docs/chore/test/refactor 里选，scope 用最贴切的模块名（拿不准就省略）；"
        "描述用简体中文、不超过 30 字、概括这批变更实际做了什么——有 diff 时以 diff 内容为准，"
        "没有 diff 时根据文件路径与状态合理推断。"
        "只输出这一行提交信息本身，不要解释、引号或代码围栏。"
    )
    user_content = f"仓库：{req.repo}\n分支：{req.branch or '未知'}\n变更文件（状态 路径）：\n{listing}"
    if req.diff.strip():
        user_content += f"\n\n变更 diff（可能已截断）：\n{req.diff.strip()}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    from agent.harness.llm_client import StreamLLMClient

    llm = StreamLLMClient(base_url=model_cfg["base_url"], api_key=model_cfg["api_key"])
    try:
        out = await asyncio.wait_for(
            llm.call(
                messages=messages,
                tools=[],  # 空工具面 = 纯推理：本端点不可能执行任何操作
                model=model_cfg.get("model_id") or model_cfg.get("name", ""),
                temperature=0.3,
                max_tokens=120,
                enable_thinking=False,
                top_p=model_cfg.get("top_p", 0.9),
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="模型服务 30 秒未响应：可重试，或直接手写提交信息")
    except Exception as e:
        logger.warning("提交信息 LLM 调用失败: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"模型调用失败（{str(e)[:120] or '服务异常'}）：可重试，或直接手写提交信息",
        ) from e

    message = _sanitize_commit_message(out.content)
    if not message:
        raise HTTPException(status_code=502, detail="模型返回了空结果：可重试，或直接手写提交信息")
    return {"message": message}
