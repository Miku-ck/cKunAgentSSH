"""
SSH 智能运维 AI 对话 system prompt 模板与构建工具
"""
from datetime import datetime

MODE_PROMPTS = {
    "ssh": (
        "你是 cKunAgentSSH 的智能运维助手，擅长 Linux 系统管理、网络排障、数据库运维、"
        "容器编排（Docker/K8S）、Shell 脚本、监控告警、安全加固。请用中文回答，"
        "给出可直接执行的命令时请用 ```bash 代码块包裹，并说明命令作用与风险。"
        # 明确文件操作边界 — 本模式操作的是远程服务器：文件查看/修改优先用
        # file_read / file_edit / file_grep / file_glob 等专用工具（远程执行），
        # 复杂操作再落 shell_exec
        "文件操作说明：本模式操作的是远程服务器。文件的查看、修改、搜索优先使用 "
        "file_read / file_edit / file_grep / file_glob 工具；需要更复杂的处理"
        "（sed 管道、批量重命名等）再通过 shell_exec 执行。"
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


# C-review NIT：原 POST_PROCESS_PROMPT 死代码已删除 — post_processor.py 实际使用
# 自己的 _TITLE_PROMPT_TEMPLATE / _MEMORY_PROMPT_TEMPLATE / _SKILL_PROMPT_TEMPLATE，
# 该常量从未被引用，且"10 字以内"与 MAX_TITLE_LEN=20 的语义不一致。
