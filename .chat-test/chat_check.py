# -*- coding: utf-8 -*-
"""AI 运维助手普通对话能力 端到端检查脚本"""
import json
import time
import httpx

BASE = "http://127.0.0.1:18099/api"
TOKEN = "ckun-chat-test-token"
H = {"X-API-Token": TOKEN}

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def read_sse(resp, cap_s=120, verbose=False):
    """解析 SSE 流，返回 (事件列表, 首token延迟s, 总耗时s)"""
    events, contents, reasonings = [], [], []
    t0, t_first = time.time(), None
    n = 0
    for line in resp.iter_lines():
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        if verbose and n < 8:
            print(f"    [evt {n} t={time.time()-t0:.2f}s keys={list(ev.keys())} len={len(ev.get('content',''))}]")
        n += 1
        events.append(ev)
        if "content" in ev and ev["content"]:
            if t_first is None:
                t_first = time.time() - t0
            contents.append(ev["content"])
        if ev.get("reasoning"):
            reasonings.append(ev["reasoning"])
        if time.time() - t0 > cap_s:
            break
    return events, "".join(contents), "".join(reasonings), (t_first or -1), time.time() - t0


with httpx.Client(timeout=180, headers=H) as c:
    # 1. 健康检查
    r = c.get(f"{BASE}/health")
    record("health", r.status_code == 200, r.text[:80])

    # 2. 模型列表
    r = c.get(f"{BASE}/models")
    models = r.json() if r.status_code == 200 else []
    record("models 列表", r.status_code == 200 and len(models) >= 1,
           f"{len(models)} 个模型: {[m.get('model_id') or m.get('id') for m in models if isinstance(m, dict)]}")

    # 3. 创建会话
    r = c.post(f"{BASE}/chat/conversations", json={"title": "chat-check", "mode": "ssh"})
    conv = r.json() if r.status_code == 200 else {}
    cid = conv.get("id") or conv.get("conversation_id") or ""
    record("创建会话", r.status_code == 200 and bool(cid), f"cid={cid}")

    # 4. 第一轮对话（运维问题）— 用 stream() 真实流式读取
    content, reasoning, t_first, t_total, errs, ok_status = "", "", -1, -1, [], False
    with c.stream("POST", f"{BASE}/chat/stream", json={
        "message": "一台 Linux 生产服务器磁盘使用率到了 95%，帮我给出排查思路和清理步骤，按优先级排。",
        "conversation_id": cid, "model_id": "", "mode": "ssh",
    }, timeout=httpx.Timeout(30, read=180)) as r:
        ok_status = r.status_code == 200
        if ok_status:
            events, content, reasoning, t_first, t_total = read_sse(r, verbose=True)
            errs = [e.get("error") for e in events if "error" in e]
    ok = ok_status and len(content) > 50 and not errs
    record("第1轮 SSE 流式对话", ok,
           f"status={r.status_code} 首token={t_first:.1f}s 总时长={t_total:.1f}s 回复长度={len(content)} 错误={errs[:2]}")
    print("---- 第1轮回复(前600字) ----")
    print(content[:600])
    print("----------------------------")

    # 5. 第二轮：上下文引用（指代上一轮内容）
    content2, errs2, t_first2, t_total2 = "", [], -1, -1
    with c.stream("POST", f"{BASE}/chat/stream", json={
        "message": "你上面让我先用 du 排查，把这条命令改成递归两层并只显示大于 1G 的目录。",
        "conversation_id": cid, "model_id": "", "mode": "ssh",
    }, timeout=httpx.Timeout(30, read=180)) as r:
        if r.status_code == 200:
            events2, content2, _, t_first2, t_total2 = read_sse(r)
            errs2 = [e.get("error") for e in events2 if "error" in e]
    has_du = "du" in content2.lower()
    record("第2轮 多轮上下文引用", r.status_code == 200 and has_du and not errs2,
           f"status={r.status_code} 含du命令={has_du} 错误={errs2[:2]}")
    print("---- 第2轮回复(前400字) ----")
    print(content2[:400])
    print("----------------------------")

    # 6. 历史持久化
    r = c.get(f"{BASE}/chat/conversations/{cid}/messages")
    msgs = r.json() if r.status_code == 200 else []
    roles = [m.get("role") for m in msgs] if isinstance(msgs, list) else []
    ok = r.status_code == 200 and roles.count("user") >= 2 and roles.count("assistant") >= 2
    record("历史消息持久化", ok, f"消息序列={roles}")

    # 7. 会话状态
    r = c.get(f"{BASE}/chat/conversations")
    convs = r.json() if r.status_code == 200 else []
    mine = next((x for x in convs if (x.get("id") or x.get("conversation_id")) == cid), {})
    record("会话状态/更新时间", r.status_code == 200 and mine.get("status") in ("completed", "in_progress", None),
           f"status={mine.get('status')} title={mine.get('title')!r}")

    # 8. 边界：空消息
    r = c.post(f"{BASE}/chat/stream", json={"message": "   ", "conversation_id": cid, "mode": "ssh"})
    record("边界: 空消息拒绝(400)", r.status_code == 400, f"status={r.status_code}")

    # 9. 边界：不存在会话
    r = c.post(f"{BASE}/chat/stream", json={"message": "hi", "conversation_id": "no-such-conv", "mode": "ssh"})
    record("边界: 不存在会话(404)", r.status_code == 404, f"status={r.status_code}")

    # 10. 边界：未带 token
    r = httpx.post(f"{BASE}/chat/stream", json={"message": "hi", "conversation_id": cid, "mode": "ssh"}, timeout=10)
    record("边界: 无 token 拒绝(401)", r.status_code == 401, f"status={r.status_code}")

    # 11. 终端命令预测（AI 助手附带能力）
    r = c.post(f"{BASE}/terminal/predict", json={
        "session_id": "test-pty-1",
        "current_input": "",
        "recent_commands": ["git status", "git diff"],
        "cwd": "/srv/app",
        "platform": "linux",
    })
    preds = r.json().get("predictions", []) if r.status_code == 200 else []
    record("终端命令预测", r.status_code == 200 and len(preds) > 0, f"status={r.status_code} predictions={preds[:3]}")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== 结果: {passed}/{len(results)} 通过 =====")
