"""
SSH 智能运维 AI 对话 system prompt 模板与构建工具
"""
from datetime import datetime

MODE_PROMPTS = {
    "ssh": (
        "你是 cKunAgentSSH 的智能运维助手，擅长 Linux 系统管理、网络排障、数据库运维、"
        "容器编排（Docker/K8S）、Shell 脚本、监控告警、安全加固。请用中文回答，"
        "给出可直接执行的命令时请用 ```bash 代码块包裹，并说明命令作用与风险。"
    ),
}


def _current_time_str() -> str:
    """返回当前时间字符串，注入 system prompt 防止模型引用过期训练数据中的时间"""
    now = datetime.now()
    # 中文星期映射（避免模型按英文翻译回出错）
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays[now.weekday()]
    return f"{now.year}年{now.month:02d}月{now.day:02d}日 {weekday} {now.hour:02d}:{now.minute:02d}"


def build_system_prompt(mode: str, context_prompt: str | None, memories: list[dict]) -> str:
    """构建完整 system prompt：模式基础 prompt + 当前时间 + 记忆注入 + 上下文 prompt

    context_prompt 放在最后，使其包含的用户自定义 system_prompt
    具有最高优先级，可覆盖/补充前面的模式指令与记忆。
    """
    parts = [MODE_PROMPTS.get(mode, MODE_PROMPTS["ssh"])]

    # 注入当前时间（解决 LLM 训练数据截止导致"今天是 X 年"回答过时的问题）
    parts.append(f"[系统时间] 当前时间：{_current_time_str()}。如用户询问日期/星期/时间，请以本信息为准，不要使用训练数据中的旧时间。")

    if memories:
        mem_text = "\n".join(f"- {m['content']}" for m in memories[:5])
        parts.append(f"以下是你在过往对话中记住的关键信息，可参考：\n{mem_text}")

    if context_prompt:
        parts.append(context_prompt)

    return "\n\n".join(parts)


POST_PROCESS_PROMPT = """\
请分析以下 AI 助手与用户的单轮对话，完成三个任务：

1. 提取关键记忆（最多 3 条）：用户偏好、重要事实、可复用上下文。每条用一句话表达。
2. 判断是否可沉淀为技能：如果对话中包含可复用的操作步骤、模板、规则、脚本或最佳实践，则生成一个技能摘要；否则 skills 为空。每个技能必须包含 trigger_words 字段：3-5 个触发词，当用户消息包含这些词时自动激活该技能。触发词应为具体、区分性强的关键词。
3. 生成会话标题（10 字以内中文）：基于用户本轮问题与助手回复，提炼关键词。

请严格按以下 JSON 格式输出，不要包含其他内容：

{
  "memories": ["记忆1", "记忆2"],
  "skills": [
    {
      "name": "技能名称",
      "slug": "skill-slug",
      "description": "技能描述",
      "system_prompt": "技能内容",
      "trigger_words": ["触发词1", "触发词2", "触发词3"]
    }
  ],
  "title": "关键字摘要"
}
"""
