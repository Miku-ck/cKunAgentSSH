"""Spec/Plan/Goal 三阶段工作流工具

配套 backend/agent/workflow.py（纯文件落盘，无 DB）。工具只做一件事：
把模型产出的规格 / 计划 / 成功标准文档写入对应工件文件。阶段推进与
成功判定交给注入 system prompt 的工作流段 + 用户确认，工具层不越权做验收。

- spec_write  → 写规格文档（读需求、理解后沉淀产物，等待用户确认）
- plan_write  → 写执行步骤计划（配合 todo_write 建任务清单，用户批准后才动手）
- goal_write  → 写成功标准（作为执行锚，后续每轮 run 都会复现注入）
"""
from agent.tools import BaseTool, ToolResult, ToolMeta, ToolContext

# 单次工件内容上限：工件会整篇注入 system prompt，过长会挤掉方法论引导
_MAX_CONTENT_CHARS = 4000


def _clip_tooltip(text: str, limit: int = 60) -> str:
    """预览片段：太长用省略号截断，避免工具回显刷屏。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


class WriteWorkflowArtifactTool(BaseTool):
    """工作流工件写入的公共骨架（三类工具共享，仅 artifact 名/措辞不同）。"""

    artifact = ""          # spec/plan/goal
    kind_label = ""        # 规格 / 计划 / 成功标准
    action_label = ""      # 已写入规格 / 已写入计划 / 已设定成功标准

    async def execute(self, context: ToolContext, content: str) -> ToolResult:
        if not context.conversation_id:
            return ToolResult(
                False, "",
                "无会话上下文，无法落盘工作流工件。请在会话内运行。",
                priority="high",
            )
        if not content or not content.strip():
            return ToolResult(
                False, "",
                f"{self.kind_label}内容不能为空。请先理解需求后再沉淀成文档。",
                priority="high",
            )
        if len(content) > _MAX_CONTENT_CHARS:
            return ToolResult(
                False, "",
                f"{self.kind_label}过长（>{_MAX_CONTENT_CHARS} 字符）。请精炼成要点，"
                "细节留在自己的推理里，不要把整段输出塞进工件。",
                priority="high",
            )

        from agent import workflow
        writer = {
            "spec": workflow.write_spec,
            "plan": workflow.write_plan,
            "goal": workflow.write_goal,
        }.get(self.artifact)
        saved = writer(context.conversation_id, content)
        if not saved:
            return ToolResult(
                False, "",
                f"{self.kind_label}写入失败，请重试。",
                priority="high",
            )

        phase = workflow.get_phase(context.conversation_id)
        out = (
            f"{self.action_label}，当前阶段：{phase}。\n"
            f"内容预览：{_clip_tooltip(saved)}"
        )
        return ToolResult(True, out, priority="normal")


class SpecWriteTool(WriteWorkflowArtifactTool):
    """写需求规格文档 —— 进入 Spec 阶段。"""

    name = "spec_write"
    description = (
        "把对用户需求的理解沉淀为规格文档（Spec）。在动手前调用：先只读调研、"
        "必要时向用户澄清，确认理解后写成清晰的需求规格，等待用户确认再进入规划。"
        "写入后当前工作流进入 spec 阶段。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "规格文档全文（markdown）：目标、范围、约束、验收要点、待确认问题",
            },
        },
        "required": ["content"],
    }
    meta = ToolMeta(risk_level="safe", reversible=True, cacheable=False,
                    idempotent=True)
    artifact = "spec"
    kind_label = "规格文档"
    action_label = "规格已写入"


class PlanWriteTool(WriteWorkflowArtifactTool):
    """写执行计划文档 —— 进入 Plan 阶段。"""

    name = "plan_write"
    description = (
        "把已确认的规格拆成有序执行步骤，写成计划文档（Plan）。建议同步用 todo_write "
        "建立任务清单以便逐项推进。写入后当前工作流进入 plan 阶段；应等待用户批准计划"
        "后再动手执行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "执行计划全文（markdown）：有序步骤清单、每步交付物、依赖与顺序",
            },
        },
        "required": ["content"],
    }
    meta = ToolMeta(risk_level="safe", reversible=True, cacheable=False,
                    idempotent=True)
    artifact = "plan"
    kind_label = "执行计划"
    action_label = "执行计划已写入"


class GoalWriteTool(WriteWorkflowArtifactTool):
    """写成功标准文档 —— 进入 Goal 阶段。"""

    name = "goal_write"
    description = (
        "把『这次任务达成什么、怎么算成功』写成成功标准（Goal），作为本任务贯穿执行的"
        "锚点。在计划获批准后调用：明确可验收的成功标准，后续每轮执行都会复现注入，"
        "完成判定以此为准，不擅自降低标准。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "成功标准全文（markdown）：可验收的结果、做与不做的边界、完成判定依据",
            },
        },
        "required": ["content"],
    }
    meta = ToolMeta(risk_level="safe", reversible=True, cacheable=False,
                    idempotent=True)
    artifact = "goal"
    kind_label = "成功标准"
    action_label = "成功标准已设定"