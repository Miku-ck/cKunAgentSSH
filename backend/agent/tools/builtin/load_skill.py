"""load_skill — 按需加载技能完整指令"""
import logging

from agent.tools import BaseTool, ToolMeta, ToolResult
from skill_registry import get_skill_registry

logger = logging.getLogger(__name__)

_MAX_CONTENT_LEN = 3000


class LoadSkillTool(BaseTool):
    name = "load_skill"
    description = (
        "按需加载指定技能的完整指令。当任务需要某个技能的专长时，"
        "调用此工具并传入技能 slug，工具会返回该技能的 system prompt 内容。"
        "你应该在后续轮次中严格遵循返回的内容执行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill_slug": {
                "type": "string",
                "description": "技能的唯一 slug（可在可用技能目录中查看）",
            },
            "reason": {
                "type": "string",
                "description": "调用原因（可选，用于审计）",
            },
        },
        "required": ["skill_slug"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
    )

    async def execute(
        self,
        context,
        skill_slug: str,
        reason: str = "",
    ) -> ToolResult:
        registry = get_skill_registry()
        skill = registry.get_by_slug(skill_slug, mode=context.mode)

        if not skill:
            return ToolResult(
                success=False,
                output="",
                error=f"未找到技能 `{skill_slug}`，或该技能在当前模式下未启用。",
                priority="high",
            )

        content = skill.get("system_prompt", "") or ""
        if len(content) > _MAX_CONTENT_LEN:
            content = content[:_MAX_CONTENT_LEN] + "\n... [技能内容较长，已截断]"

        name = skill.get("name") or skill_slug
        output = (
            f"已加载技能「{name}」（slug: {skill_slug}）"
            + (f"，调用原因：{reason}" if reason else "")
            + f"。\n---技能内容开始---\n{content}\n---技能内容结束---\n"
            "请在后续轮次中遵循上述技能内容执行。"
        )

        logger.info("load_skill 调用: %s (mode=%s)", skill_slug, context.mode)
        return ToolResult(success=True, output=output, priority="high")
