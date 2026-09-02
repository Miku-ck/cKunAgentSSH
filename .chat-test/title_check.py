# -*- coding: utf-8 -*-
"""验证默认标题会话的自动标题生成"""
import json
import time
import httpx

BASE = "http://127.0.0.1:18099/api"
H = {"X-API-Token": "ckun-chat-test-token"}

with httpx.Client(timeout=60, headers=H) as c:
    r = c.post(f"{BASE}/chat/conversations", json={"mode": "ssh"})  # 不传 title → 默认标题
    conv = r.json()
    cid = conv.get("id") or conv.get("conversation_id")
    print("创建(默认标题):", cid, "初始title:", repr(conv.get("title")))

    with c.stream("POST", f"{BASE}/chat/stream", json={
        "message": "帮我写个 crontab，每天凌晨 3 点备份 /etc 目录到 /backup",
        "conversation_id": cid, "model_id": "", "mode": "ssh",
    }, timeout=httpx.Timeout(30, read=180)) as resp:
        for _ in resp.iter_lines():
            pass
    print("对话完成, 等待后处理生成标题...")

    for i in range(12):
        time.sleep(5)
        r = c.get(f"{BASE}/chat/conversations")
        mine = next((x for x in r.json() if (x.get("id") or x.get("conversation_id")) == cid), {})
        t = mine.get("title")
        print(f"  [{(i+1)*5}s] title={t!r}")
        if t and t != conv.get("title"):
            print("标题已自动生成:", t)
            break
    else:
        print("!! 60s 内标题未生成 (仍为默认)")
