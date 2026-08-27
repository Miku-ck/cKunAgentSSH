"""
SSE 工具函数 — 公共 SSE 格式化
"""
import json


def sse_event(data: dict) -> str:
    """格式化 SSE data 事件"""
    # 修复：添加 default=str 处理不可序列化对象（如 datetime），避免 SSE 流中断
    return f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
