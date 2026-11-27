"""运维专家建议谓词 —— 关键词级规则，不是新分类器（spec §6.3）。

触发：未选专家 + 非纯对话 +（运维动作 ∨ 运维名词）。复用 intention 的词边界
匹配器；运维名词小词表覆盖只读运维（"看下磁盘"不命中变更动词）。
误报成本低（非阻塞 chip），漏报由用户手选兜底。判定失败由调用方静默 no-op。
"""
from agent.intention import (
    _any_match_wordbounded,
    _precompile_markers,
)

# 文件清理、代码提交、安装依赖等通用变更不等于运维；只有明确的基础设施动作
# 才在这里触发，其余任务靠运维名词命中（如"清理日志""安装 nginx"）。
_OPS_ACTIONS = [
    "重启", "停止", "启动", "关掉", "关闭服务", "杀掉", "杀死", "重新加载",
    "restart", "stop", "start", "reload", "kill",
    "部署", "发布", "上线", "回滚", "回退", "扩容", "缩容", "迁移", "备份", "恢复",
    "deploy", "rollback", "scale",
    "apt-get", "yum", "dnf", "apk", "sudo",
]
_COMPILED_OPS_ACTIONS = _precompile_markers(_OPS_ACTIONS)

_OPS_NOUNS = [
    "服务器", "磁盘", "端口", "进程", "服务", "日志", "容器", "主机",
    "内存", "负载", "数据库",
    "systemctl", "docker", "nginx", "kubernetes", "k8s", "mysql", "redis",
]
_COMPILED_OPS_NOUNS = _precompile_markers(_OPS_NOUNS)


def should_suggest_ops(task: str, *, expert_selected: bool, is_conversation: bool) -> bool:
    if expert_selected or is_conversation:
        return False
    text = task or ""
    if not text.strip():
        return False
    lowered = text.lower()
    if _any_match_wordbounded(_COMPILED_OPS_ACTIONS, lowered, text):
        return True
    return _any_match_wordbounded(_COMPILED_OPS_NOUNS, lowered, text)