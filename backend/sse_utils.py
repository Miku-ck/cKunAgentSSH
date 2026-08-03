"""
SSE 工具函数 — 公共 SSE 格式化
"""
import json


def sse_event(data: dict) -> str:
    """格式化 SSE data 事件"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
