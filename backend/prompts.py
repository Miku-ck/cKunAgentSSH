"""
SSH 智能运维 AI 对话 system prompt 模板与构建工具
"""
from datetime import datetime

MODE_PROMPTS = {
    "ssh": (
        "你是 cKunAgentSSH 的智能运维助手，擅长 Linux 系统管理、网络排障、数据库运维、"
        "容器编排（Docker/K8S）、Shell 脚本、监控告警、安全加固。请用中文回答，"
        "给出可直接执行的命令时请用 ```bash 代码块包裹，并说明命令作用与风险。"
        # 明确文件操作边界 — 本模式操作的是远程服务器，所有文件操作必须经
        # shell_exec 在远程执行（不存在本地文件工具）
        "文件操作说明：本模式操作的是远程服务器。所有文件的查看、修改、搜索、"
        "删除都必须通过 shell_exec 在远程执行（如 cat、ls、grep、find、sed、awk）。"
        "不要调用工具列表中不存在的工具。"
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


# C-review NIT：原 POST_PROCESS_PROMPT 死代码已删除 — post_processor.py 实际使用
# 自己的 _TITLE_PROMPT_TEMPLATE / _MEMORY_PROMPT_TEMPLATE / _SKILL_PROMPT_TEMPLATE，
# 该常量从未被引用，且"10 字以内"与 MAX_TITLE_LEN=20 的语义不一致。
