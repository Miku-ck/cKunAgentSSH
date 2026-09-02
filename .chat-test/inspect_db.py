# -*- coding: utf-8 -*-
import sqlite3

conn = sqlite3.connect(r"D:\workspace\cKunAgentSSH\.chat-test\ckun-test.db")
conn.row_factory = sqlite3.Row
for r in conn.execute("select id, title, status, created_at from conversations"):
    print("CONV:", dict(r))
print("agent_memories rows:", conn.execute("select count(*) c from agent_memories").fetchone()["c"])
print("memories rows:", conn.execute("select count(*) c from memories").fetchone()["c"])
row = conn.execute(
    "select content from messages where conversation_id=(select id from conversations order by created_at desc limit 1)"
    " and role='assistant' order by created_at desc limit 1"
).fetchone()
print("=== LAST ASSISTANT FULL ===")
print(row["content"] if row else "(none)")
