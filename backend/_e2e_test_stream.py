"""端到端集成测试：模拟 LLM + 真实后端进程，验证 /api/chat/stream 真实流式输出。

流程：
1. 起 OpenAI 兼容模拟 LLM（127.0.0.1:18081/v1，流式返回固定 token）
2. 起真实后端 main.py（临时 DB，指向模拟 LLM）
3. 建 SSH 默认模型 → 建会话 → POST /api/chat/stream
4. 断言 SSE 收到 content token 与 [DONE]
5. 409 自愈验证：手动置 in_progress → 再发请求应自愈成功
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

BACKEND = os.path.dirname(os.path.abspath(__file__))
LLM_PORT = 18081
BE_PORT = 18010
failures = []


def check(name, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


# ── 1. 模拟 LLM：OpenAI 兼容 /v1/chat/completions（SSE） ──
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for tok in ["你好", "，", "世界"]:
            chunk = {
                "id": "c1", "object": "chat.completion.chunk", "created": 1,
                "model": body.get("model", "mock"),
                "choices": [{"index": 0, "delta": {"content": tok}, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.05)
        final = {
            "id": "c1", "object": "chat.completion.chunk", "created": 1,
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):  # 静默
        pass


llm_srv = HTTPServer(("127.0.0.1", LLM_PORT), MockLLM)
threading.Thread(target=llm_srv.serve_forever, daemon=True).start()
print(f"[mock-llm] listening on {LLM_PORT}")


def api(path, payload=None, method=None, token="test-token"):
    url = f"http://127.0.0.1:{BE_PORT}/api{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    tmp = tempfile.mkdtemp(prefix="ckun-e2e-")
    env = dict(os.environ,
               DB_PATH=os.path.join(tmp, "e2e.db"),
               CKUN_API_TOKEN="test-token",
               PYTHONUNBUFFERED="1")
    be = subprocess.Popen(
        [sys.executable, "main.py", "--port", str(BE_PORT), "--host", "127.0.0.1"],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        # 等就绪
        for _ in range(60):
            try:
                st, _ = api("/health")
                if st == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            check("后端就绪", False)
            return
        check("后端就绪", True)

        # 建 SSH 默认模型指向模拟 LLM（接口为 /settings/models）
        st, model = api("/settings/models", payload={
            "name": "mock", "base_url": f"http://127.0.0.1:{LLM_PORT}/v1",
            "api_key": "EMPTY", "model_id": "mock-model", "is_default_ssh": True,
        })
        check("创建模型 200", st == 200)
        mid = model.get("id")
        check("模型有 id", bool(mid))

        # 建会话
        st, conv = api("/chat/conversations", payload={"title": "e2e", "mode": "ssh"})
        check("创建会话 200", st == 200)
        cid = conv.get("id")

        # 流式请求（真实 SSE 读取）
        req = urllib.request.Request(
            f"http://127.0.0.1:{BE_PORT}/api/chat/stream",
            data=json.dumps({"message": "测试", "conversation_id": cid,
                             "model_id": mid, "mode": "ssh"}).encode(),
            method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-API-Token", "test-token")
        contents, done, err = [], False, None
        with urllib.request.urlopen(req, timeout=30) as r:
            check("chat/stream 200", r.status == 200)
            check("Content-Type 是 event-stream",
                  "text/event-stream" in r.headers.get("Content-Type", ""))
            for raw in r:
                line = raw.decode().strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    done = True
                    break
                ev = json.loads(data)
                if ev.get("error"):
                    err = ev["error"]
                if ev.get("content"):
                    contents.append(ev["content"])

        check("收到 [DONE]", done)
        check("收到流式 content token", "".join(contents) == "你好，世界")
        check("无 error 事件", err is None)

        # 409 自愈：模拟滞留锁 → 再发请求应自愈成功
        st2, _ = api(f"/chat/conversations/{cid}", payload={"status": "in_progress"},
                     method="PATCH") if False else (None, None)
        # 直接用 sqlite 置 in_progress（模拟滞留）
        import sqlite3
        conn = sqlite3.connect(os.path.join(tmp, "e2e.db"))
        conn.execute("UPDATE conversations SET status='in_progress' WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{BE_PORT}/api/chat/stream",
            data=json.dumps({"message": "自愈测试", "conversation_id": cid,
                             "model_id": mid, "mode": "ssh"}).encode(),
            method="POST")
        req2.add_header("Content-Type", "application/json")
        req2.add_header("X-API-Token", "test-token")
        try:
            with urllib.request.urlopen(req2, timeout=30) as r:
                check("滞留锁下请求 200（自愈成功）", r.status == 200)
        except urllib.error.HTTPError as e:
            check(f"滞留锁下请求 200（自愈成功）, 实际 {e.code}", False)

        print()
        if failures:
            print(f"结果: {len(failures)} 项失败 -> {failures}")
            sys.exit(1)
        print("结果: 全部通过")
    finally:
        be.terminate()
        try:
            be.wait(timeout=10)
        except Exception:
            be.kill()
        llm_srv.shutdown()


if __name__ == "__main__":
    main()
