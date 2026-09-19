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


# ── 运维方法论注入 ────────────────────────────────────────────────────────
# 背景：MODE_PROMPTS 只给了角色与输出约定，routes/agent.py 追加的 5 段引导
# 全是"怎么用工具"，没有一句"怎么做运维"。结果是模型容易直接下发变更命令：
# 不先确认当前状态、不说明影响、不交代回滚。下面两段按任务性质二选一注入。

OPS_METHOD_READONLY = (
    "远程运维方法（只读任务）：本任务不改变目标机状态。"
    "优先用专用只读工具取证（sys_overview / process_inspect / network_probe / "
    "log_read / file_read / file_grep），并基于真实输出作答；"
    "涉及发行版、服务名、路径、端口时以探测结果为准，不要凭训练数据里的印象推断。"
)

OPS_METHOD_CHANGE = (
    "远程运维方法（变更任务）：\n"
    "0. 若探测后发现本任务实际是纯只读（不含任何变更动作），按只读方式处理即可，不必套用以下条款。\n"
    "1. 先探测再变更：动手前先用只读工具确认当前状态（服务是否在跑、配置现状、磁盘余量），不要跳过这一步。\n"
    "2. 执行前给出结构化说明：做什么 / 为什么（依据哪次探测结果）/ 影响范围 / 是否可逆 / 如何回滚。\n"
    "3. 优先可逆操作：能用 reload 不用 restart，能改配置不删配置，先备份再修改。\n"
    "4. 目标含糊先确认：若存在多个候选（多个同名服务、路径不明、影响面不确定），"
    "先列出候选与各自影响，等用户确认后再执行，不要自行替用户选择。\n"
    "5. 小步变更并验证：一次只做一个变更，执行后用只读工具验证（状态 / 进程 / 端口 / 日志），确认成功再继续。\n"
    "6. 如实汇报：结果与预期不符时如实报告并给出恢复建议，不要声称已完成。"
)


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
