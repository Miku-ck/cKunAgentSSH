import sqlite3, json

f = r"C:\Users\ckun\AppData\Roaming\cKunAgentSSH\db\ckunagentic.db"
c = sqlite3.connect(f)
c.row_factory = sqlite3.Row
print("== schedules ==")
for r in c.execute("SELECT id,name,schedule_type,target_host,last_status,last_run_id,last_run_at,next_run_at FROM schedules"):
    print(dict(r))
print("== schedule_runs (recent) ==")
cols = [x[1] for x in c.execute("PRAGMA table_info(schedule_runs)")]
print("cols:", cols)
for r in c.execute("SELECT * FROM schedule_runs ORDER BY started_at DESC LIMIT 6"):
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, str) and len(v) > 300:
            d[k] = v[:300] + "...[trunc]"
    print(json.dumps(d, ensure_ascii=False))