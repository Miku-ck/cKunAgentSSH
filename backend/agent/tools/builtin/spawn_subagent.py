"""
子 Agent 工具 — 让主 Agent 把独立子任务交给一个轻量子 Agent 处理

设计目标：
- 让主 Agent 在面对复杂任务时能拆分出独立子任务并行/串行处理
- 子 Agent 不嵌套完整 Harness（避免递归深度过高、资源消耗过大）
- 子 Agent 是一次独立的 LLM 调用，有自己的 system_prompt 和子任务描述
- 子 Agent 无工具调用能力（纯推理），返回文本结果给主 Agent
- 主 Agent 拿到结果后自行决定下一步

适用场景：
1. 代码审查：让子 Agent 专注审查某个文件
2. 方案设计：让子 Agent 针对某个子问题给出方案
3. 文档生成：让子 Agent 根据代码生成文档
4. 测试用例：让子 Agent 生成测试代码

注意：子 Agent 使用与主 Agent 相同的 LLM 配置（base_url/api_key/model）。
"""
import asyncio
import logging
from typing import Optional

from agent.tools import BaseTool, ToolResult, ToolMeta, ToolContext

logger = logging.getLogger(__name__)


# 子 Agent 迭代数硬上限，防止子任务失控消耗过多 token
_MAX_SUBAGENT_TOKENS = 4096


class SpawnSubagentTool(BaseTool):
    """子 Agent 工具

    让主 Agent 把一个独立子任务交给子 Agent 处理。
    子 Agent 是一次独立 LLM 调用（无工具），返回推理结果。

    使用场景：
    - 把大任务拆分成多个独立子任务并行处理
    - 让子 Agent 专注分析/设计，主 Agent 负责执行
    - 避免主 Agent 上下文被某个子问题的细节淹没
    """

    name = "spawn_subagent"
    description = (
        "启动一个子 Agent 处理独立子任务。子 Agent 是一次无工具的 LLM 推理调用，"
        "有自己的 system_prompt 和 task。适合做代码审查、方案设计、文档生成、"
        "测试用例生成等可独立完成的子任务。返回子 Agent 的推理结果文本。"
        "使用此工具可以把复杂任务拆分成多个子任务，避免主 Agent 上下文过载。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "子任务描述，需清晰独立，包含足够上下文让子 Agent 理解要做什么",
            },
            "system_prompt": {
                "type": "string",
                "description": "子 Agent 的角色设定/专长指引。例如：'你是一位资深 Python 工程师，专注于代码审查'",
            },
            "extra_context": {
                "type": "string",
                "description": "可选的额外上下文（如代码片段、文件内容等），会拼接到 task 之后",
            },
        },
        "required": ["task", "system_prompt"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        cacheable=False,
        timeout=120.0,
    )

    async def execute(self, context: ToolContext, task: str,
                      system_prompt: str, extra_context: str = "") -> ToolResult:
        """执行子 Agent 调用

        参数:
            context: 工具执行上下文（含 workspace/mode 等信息）
            task: 子任务描述
            system_prompt: 子 Agent 角色设定
            extra_context: 额外上下文（可选）

        返回:
            ToolResult，output 为子 Agent 的推理结果
        """
        if not task.strip():
            return ToolResult(
                False, "", "task 不能为空", priority="high"
            )
        if not system_prompt.strip():
            return ToolResult(
                False, "", "system_prompt 不能为空", priority="high"
            )

        try:
            result_text = await self._run_subagent(
                task=task,
                system_prompt=system_prompt,
                extra_context=extra_context or "",
                tool_ctx=context,
            )
            return ToolResult(
                True,
                result_text,
                priority="normal",
            )
        except Exception as e:
            logger.exception("子 Agent 运行失败")
            return ToolResult(
                False, "", f"子 Agent 运行失败: {e}", priority="high"
            )

    async def _run_subagent(
        self,
        task: str,
        system_prompt: str,
        extra_context: str,
        tool_ctx: ToolContext,
    ) -> str:
        """运行子 Agent — 一次独立 LLM 调用

        子 Agent 复用主 Agent 的 LLM 配置（从 DB 读取），
        但使用自己的 system_prompt 和 messages。
        """
        from agent.harness.llm_client import StreamLLMClient

        # 从 DB 获取当前模式的默认模型配置
        # D 修复（事件循环阻塞）：内部含多次同步 db 查询，放线程池执行
        model_cfg = await asyncio.to_thread(self._get_model_config, tool_ctx)
        if not model_cfg:
            return "[子 Agent] 未找到可用模型配置，无法运行"

        model_id = model_cfg.get("model_id") or model_cfg.get("id", "")
        llm = StreamLLMClient(
            base_url=model_cfg["base_url"],
            api_key=model_cfg["api_key"],
        )

        # 构造子 Agent 的 messages
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        user_content = task
        if extra_context:
            user_content += f"\n\n--- 附加上下文 ---\n{extra_context}"
        messages.append({"role": "user", "content": user_content})

        # 调用 LLM（非流式收集完整结果）
        try:
            # 修复：添加超时防止 LLM 服务挂起导致子 Agent 永久阻塞
            result = await asyncio.wait_for(
                llm.call(
                    messages=messages,
                    tools=[],
                    model=model_id,
                    temperature=0.5,  # 子 Agent 用较低温度提高确定性
                    max_tokens=_MAX_SUBAGENT_TOKENS,
                    enable_thinking=False,
                ),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            logger.warning("子 Agent LLM 调用超时（120s）")
            return "[子 Agent LLM 调用超时]"
        except Exception as e:
            logger.warning("子 Agent LLM 调用失败: %s", e)
            return f"[子 Agent LLM 调用失败: {e}]"

        # 审查修复（P2 E6）：子 Agent 的 LLM token 计入主 Agent CostTracker —
        # 原实现子 Agent 调用的 token 用量完全丢失，主 Agent 面板显示的
        # cost/usage 与实际不符（只计主 Agent 自身的 LLM 调用）。
        # llm.call() 返回的 LLMOutput 含 prompt_tokens / completion_tokens
        # （由 StreamLLMClient 从 usage chunk 提取或估算兜底）
        if tool_ctx.cost_tracker is not None and hasattr(result, "prompt_tokens"):
            try:
                tracker = tool_ctx.cost_tracker
                tracker.record_llm_call(
                    getattr(result, "prompt_tokens", 0),
                    getattr(result, "completion_tokens", 0),
                )
            except Exception as rec_err:
                logger.debug("子 Agent token 记录失败: %s", rec_err)

        return result.content or "[子 Agent 未返回内容]"

    def _get_model_config(self, tool_ctx: ToolContext) -> Optional[dict]:
        """从 DB 获取当前模式的默认模型配置

        优先级：
        1. 当前模式（tool_ctx.mode）的默认模型
        2. 全局默认模型
        3. 任意一个可用模型
        """
        try:
            import database as db
            mode = tool_ctx.mode or "ssh"
            # 1. 模式默认 → 2. 全局默认 → 3. 任意可用（list_models_config 返回 list[dict]，取前 10 个）
            model = db.get_default_model(mode)
            if not model:
                model = db.get_default_model(None)
            if not model:
                models = db.list_models_config()[:10]
                model = models[0] if models else None
            if not model:
                return None
            # 以上查询的 api_key 均已脱敏，子 Agent 的 LLM 调用需要真实 key，按 id 重取
            cfg_id = model.get("id")
            if cfg_id:
                return db.get_model_config(cfg_id, mask_key=False) or model
            return model
        except Exception as e:
            logger.warning("获取模型配置失败: %s", e)
        return None
