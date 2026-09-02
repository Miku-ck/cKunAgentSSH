"""头尾保留截断常量 — 单一数据源（C-review NIT）。

原 4000/1500/1000 三值在 shell_exec / file_ops / grep_tool / bridle 四处
手工复制（drift 风险）：改一处不改另一处会导致工具层截断与上下文层
截断阈值脱节。现统一在此定义，各处 import 引用。
"""
# 超过该长度触发头尾保留截断
TRUNC_THRESHOLD = 4000
# 截断时保留的头部字符数
TRUNC_HEAD = 1500
# 截断时保留的尾部字符数
TRUNC_TAIL = 1000
