# P0 运维 Agent 增强 · 统一设计方案（功能 A / B / C）

> 状态：设计稿 v2（勘察报告 A/B/C 已逐条核对源码后收敛；2026-10-27 设计评审 5 条意见全部核实成立并处置，见 §7）。
> 本文引用的行号以 2026-10-26/27 工作树为准；三份勘察报告与评审意见的关键结论已全部用 Read/grep 复核，未复核项在 §6 显式标注。

---

## 0. 总览

### 0.1 三个功能一句话

| 功能 | 一句话 | 治理底座 |
|---|---|---|
| A 告警触发诊断 run | 渲染层指标连续 N 次越限 → 自动发起一次带完整治理的诊断 agent run，结果进任务台 | 复用 `POST /api/agent/run` 交互式 SSE（审批卡/审计/熔断零成本继承），后端零改动 |
| B run 级一键回滚 | run 结束时把 Saddle Undo 包持久化；轨迹页对已完成 run 提供一键回滚（LIFO、逐项可信度、危险项弹卡、全程审计） | 回滚本身以新 Agent run 走 `/agent/run` 全链；undo 序列化/存储为独立新模块 |
| C 可执行 Runbook | 剧本结构化（步骤=命令+断言+说明），一键执行：经完整 Harness 逐步执行、断言失败即停转诊断、历史可查 | 每步一次无头 run（复用 schedule_engine 同源装配），断言由 executor 确定性判读 |

### 0.2 统一工程约定（三个功能共同遵守）

1. **文件所有权互斥**：实现者只改各自 `ownedFiles`。四个硬共享文件 —— `backend/main.py`、`backend/routes/agent.py`、`src/api/client.ts`、`src/composables/useSshNavigation.ts` —— **一律不出现在任何 ownedFiles**，其接线全部进 §5 sharedWiring 清单，由集成者统一执行。
2. **新端点进新 router 文件**：B → `backend/routes/rollbacks.py`（新建）；C → `backend/routes/runbooks.py`（新建）。A 无新端点。两者均仿照 `backend/routes/schedules.py:19` 的 `APIRouter(tags=[...])` + Pydantic Field 上限模式。
3. **新表进新 store 模块**：B → `backend/undo_store.py`；C → `backend/runbook_store.py`。不往 `backend/database.py` 添加表定义（避免 B/C 在同一共享文件上相撞）。建表用 `CREATE TABLE IF NOT EXISTS` 幂等执行（与 `database.init_db` 同一惯例，见 `backend/database.py:254-294` 各表写法），连接复用 `database._get_conn()`（同一 SQLite 库、同一 WAL/外键配置）。所有外部输入一律参数绑定（铁律：不得拼接/format/f-string 组装 SQL）。
4. **落盘 tmp+rename 原子写**：凡写文件（如 A 的规则若改后端持久化、B 的 allowlist 相关路径已被既有代码覆盖）必须走 `tmp + os.replace` 原子替换，先例：`backend/agent/harness/reins/gate.py:411-437`（注释自称「CONTEXT.md『半写损坏』口径」）。A v1 规则存 localStorage（键级原子），不产生文件写；B/C 本轮新增的落库全走 SQLite 事务。
5. **测试文件命名**：后端 `backend/tests/test_<域>_<守卫什么>.py`；前端 `*.test.ts`（vitest include 覆盖 `src/**/*.test.ts`，见 `vitest.config.ts:14-15`）。后端一律从现有基线扩展风格：undo 回归基线 `test_file_undo.py` / `test_shell_undo.py`，schedule 回归基线 `test_schedule_engine_stability.py` 等（已在 `backend/tests/` 核实存在）。
6. **治理铁律不旁路**：授权先写审计再生效（`backend/agent/audit.py:241-264` append_record 的 critical 契约）；fail-closed（缺证据即拒绝，参照 `saddle.py:217-226` 快照失败一律 NoOp 而非当成「文件不存在」）；生产环境判定只收紧不放宽（`backend/agent/environment_policy.py:117`：`effective = "prod" if "prod" in (host_env, req) else "test"`）；命令内容级判定统一走 `safety.classify_command`（`saddle.py:66-73` 同源先例）。
7. **审计 conclusion 只用白名单既有值**：`LEGAL_CONCLUSIONS`（`backend/agent/audit.py:145-149`）之外的新值会被**静默改写为 executed**（`audit.py:265-275`），且该清单被 `backend/tests/test_ai_loop_core_regression.py` 的同源清单测试钉死——B/C 本轮**不新增** conclusion 值，剧本/回滚级事件用 `action` 区分、细节放 `result` 字段。

### 0.3 文件所有权矩阵（互斥性总检）

| 文件 | A | B | C | 备注 |
|---|---|---|---|---|
| `backend/routes/agent.py` | 不改 | 不改 | 不改 | 硬共享，接线进 sharedWiring（本轮其实零接线） |
| `backend/main.py` | 不改 | 不改 | 不改 | 硬共享，B/C 的 include_router 进 sharedWiring |
| `src/api/client.ts` | 不改 | 不改 | 不改 | 硬共享；B/C 前端各自从新 api 模块 import |
| `src/composables/useSshNavigation.ts` | 不改 | 不改 | 不改 | 硬共享；A 只读 `useServerMonitorStore` 导出 ref |
| `backend/agent/harness/driver.py` | — | **唯一修改者** | — | B 最小插入 `_emit_done`（§2） |
| `backend/agent/schedule_engine.py` | 不改 | 不改 | 不改 | 无头装配抽取 defer 到 v2（评审意见处置 §7-2），三功能零触碰 |
| `src/views/ssh/SshView.vue` | 接线（sharedWiring 一行） | — | — | 工作树已脏，由集成者统一处理 |
| `src/views/ssh/SchedulesView.vue` | — | — | **唯一修改者** | 工作树干净（git status 核实） |
| `src/components/agent/AgentFullTrace.vue` | — | **唯一修改者** | — | 工作树干净 |
| `backend/agent/tools/builtin/__init__.py` | — | **唯一修改者** | — | B 注册 undo_restore 工具（注册清单 +2 行，见 §2.4） |
| 其余全部为新建文件 | ✓ | ✓ | ✓ | 三功能零重叠 |

> git status（2026-10-26 已运行核实，2026-10-27 复核补遗）：`src/stores/ssh-ai.ts`、`src/views/ssh/SshView.vue`、`src/api/base.ts`、`src/api/client.ts`、`src/composables/useSshNavigation.ts`、`src/stores/server-monitor.ts`、**`src/stores/server-monitor-core.ts`**、`backend/main.py` 为 M 状态——因此本方案对 A 设计为**不碰**其中任何一个（SshView.vue 仅集成者加一行挂载；server-monitor-core.ts 是 A 的对齐基准而非修改对象）。`src/stores/server-monitor-core.ts` 正被在途修改：本文引用其行号（`:134-154`、`:603-604`，2026-10-27 复核仍准确）在合并前可能漂移，**集成者合并时需复核这两处契约**；`backend/agent/harness/*`、`backend/routes/agent.py`、`backend/database.py`、`src/views/ssh/SchedulesView.vue`、`src/components/agent|ssh/Agent*` 均干净，B/C 可安全落点。

---

## 1. 功能 A · 告警触发诊断 run

### 1.1 设计摘要

监控仪表盘每 5s 采集一次指标（`src/stores/server-monitor.ts:32` `POLL_INTERVAL_MS=5000`，自排定时器 `:231-238`），数据只存在于渲染层内存——后端 `backend/routes/` 无任何 metrics/monitor 端点（grep 零命中，本轮已运行核实）。因此**判断与触发放前端**（数据所在地），执行走既有 `POST /api/agent/run` 交互式 SSE：`agentStore.runAgent → streamAgentRun`（`src/stores/agent.ts:334` → `src/api/sse.ts:218`）。由此零成本继承：

- **审批卡**：`await_approval=True` 让 Driver 真的停住（`backend/routes/agent.py:1107`；闸机制注释钉死「只对交互式 run 生效」`backend/agent/harness/reins/approval_wait.py:14-15`）；`permission_needed` 事件 → ApprovalDock 常驻 + toast 喊人（`src/stores/agent.ts:740-803`，toast `:798-800`）。
- **审计/熔断/环境强化**：`resolve_run_environment` 服务端硬判定、客户端声明仅作上限（`backend/routes/agent.py:999-1005`；`environment_policy.py:117` 只收紧）；`max_cost_usd/max_total_tokens` 熔断（`agent.py:425-426`）。
- **任务台可见性**：交互式 run 的用户消息落库（`agent.py:1032-1039`）、assistant 消息+轨迹快照落库（`agent.py:1324-1374`，无正文也落一条 `:1363-1372`）、终态 `snapshot()` 固化（`src/stores/agent.ts:399-434`）——这是**不选** schedule_engine 无头路径的决定性理由：无头 run 不写任何会话消息（grep `add_message` 于 `backend/agent/schedule_engine.py` 零命中，已运行核实），且物理上给不了审批卡。

**防风暴四层闸**（全部既有机制 + 新模块内轻量状态机）：
1. 连续 N 次越限本身：重载后历史清零（`server-monitor.ts:329-357` stop 清空），需重新累积 N 个 5s 样本 ≈ 自然冷却下限；
2. 新模块内存冷却期 + in-flight 标志（触发后按 host+规则键冷却，默认 30 分钟）；
3. `agentStore` 单流保护 + `isRunning` 拦截（先例 `src/stores/ssh-ai.ts:746-748`；`agent.ts:314-318` 新 run 会 abort 旧流——**所以 A 绝不能在用户 run 进行中触发**，静默放弃等下轮冷却）；
4. 后端同会话并发 409 硬闸（`backend/routes/agent.py:990-997`）——A 用**专用诊断会话**彻底避开与用户交互会话相撞。

**关键样本有效性规则**：`computeCpuDiff` 首轮/计数器未前进返回全 0（`server-monitor-core.ts:603-604`，已核实）——越限计数必须跳过 `used===0 && iowait===0` 的 CPU 样本，否则「CPU < 10%」类低阈值规则会把首轮样本误计为连续越限。同理，采集失败转后台重试期间（`server-monitor.ts:276-301` `enterRetryMode`，15s 间隔）`current` 停更，连续计数以「最后一次成功样本」推进、不因时间流逝虚增；`stop()`/切会话（历史清零）时计数与冷却一并重置。

### 1.2 数据模型与存储

**v1 不建任何后端存储**（指标与判断都在渲染层；无渲染层 = 无轮询 = 无触发源，跨重启持久化冷却非必需）。规则与开关存 **localStorage**（键级原子写，无文件落盘，不涉及 tmp+rename 约束）：

```
localStorage key: ckun.alertTrigger.rules.v1
{
  "version": 1,
  "rules": [                       // 全局规则集（v1 预置三条，可增删改）
    { "id": "cpu_high",   "metric": "cpu",   "op": ">", "threshold": 90,
      "consecutiveN": 6,  "enabled": true },        // 6×5s = 持续 30s
    { "id": "mem_high",   "metric": "mem",   "op": ">", "threshold": 90,
      "consecutiveN": 12, "enabled": true },
    { "id": "disk_high",  "metric": "disk",  "op": ">", "threshold": 92,
      "consecutiveN": 24, "enabled": true, "diskScope": "any" }  // 任一挂载点
  ],
  "hostOverrides": { "<host>": false },   // 按主机开关（缺省 = 启用）
  "cooldownMinutes": 30
}
```

- `metric` 取值域（`ServerMetrics` 形态，`src/stores/server-monitor-core.ts:134-154`，已核实）：`cpu`（cpu.used）、`iowait`、`mem`（mem.percent）、`load1`（load[0]）、`disk`（disks[].percent，`diskScope: any|mount:<路径>`，disks ≤8 块）、`gpu_mem`（gpus[].memPercent）、`swap`（swap.used/total，total=0 时规则跳过）。
- **运行时状态（纯内存，不持久化）**：`{ [host+ruleId]: { count: number, lastValidTs: number, cooldownUntil: number, inFlight: boolean } }`。冷却以**触发时刻**起算（不以 run 终态起算）——run 失败（如 SSE 停滞 90s 看门狗 `src/api/sse.ts:214` 误杀、后端重启）也照常计时，否则每次失败重试都会重置风暴窗口。
- 诊断会话：每次触发经 `api.createConversation(title, 'ssh')`（`src/api/core.ts:189-193`，已核实签名）新建，标题 `[告警诊断] {host} {规则id}`。会话本体走既有 `conversations` 表，**不新增表**。

### 1.3 API 契约

**零新端点、零后端改动。** 前端调用链（全部既有 API）：

```
[每 5s] monitorStore.current 更新
  └─ useAlertTrigger 的 watch（外挂，与 useSshNavigation.ts:366-379 的生命周期 watch 并行共存）
       ├─ alert-trigger-core.evaluate(rules, sample, host) → 触发判定（纯函数）
       ├─ 命中且不在冷却/in-flight：
       │    1. api.createConversation('[告警诊断] …', 'ssh')        // core.ts:189
       │    2. agentStore.runAgent({
       │         task: buildDiagnosisTaskText(host, rule, current, histories), // ≤4KB
       │         conversationId: <新会话 id>,
       │         modelId: settings 默认模型,          // 先例 ssh-ai.ts:759-763
       │         systemPrompt: ALERT_SYSTEM_PROMPT,   // 告警上下文说明
       │         sshSessionId: current.sshId,         // server-monitor.ts:202
       │         environment: 'test',                 // 服务端硬判定只收紧（agent.py:999-1005）
       │         maxCostUsd: <设置默认>,
       │         permissionMode: 'normal',            // 变更操作照常弹卡
       │       })                                     // agent.ts:334 → sse.ts:218
       │    3. toast.info('已在任务台自动发起诊断 …')  // 不抢导航，pendingPrefill 仅作可选旁路
       └─ 409 / isRunning / 未就绪 → 静默放弃，冷却照常计时
```

- **SSE 事件**：全由 `agentStore` 既有消费者处理（`_handleEvent`），A 不新增事件类型。审批卡携带 `environment/host/task/ssh_session_id` 供用户核对目标（`src/stores/agent.ts:779-786`）。
- **诊断 task 文本**（`alert-trigger-core.buildDiagnosisTaskText` 生成，纯函数可单测）：主机名（`current.host`，连接配置反查注入 `server-monitor.ts:187`）+ 触发规则 + 当前快照（越限指标 + mem/swap + 按 percent 排序前 3 块磁盘 + GPU 摘要）+ cpu/mem 历史尾 12 点。**不塞** ports/procs 清单（膨胀且无必要）；硬上限 4000 字符，远低于 `AgentRunRequest.task` 的 50000（`backend/routes/agent.py:398`）。
- **system_prompt**：说明「这是由指标告警自动发起的诊断 run；先只读定位，产出结论与建议修复命令；任何变更命令会弹审批卡由用户裁决」。不传工具面裁剪——诊断需要读工具与建议能力。

### 1.4 本轮实施范围

**做：**
1. `src/utils/alert-trigger-core.ts`（纯逻辑，无 vue 依赖，模式同 `server-monitor-core.ts` 拆分先例 `server-monitor.ts:11`）：规则模型与校验、单样本评估、连续 N 次计数（跳过全 0 CPU 样本、polling=false 时冻结）、冷却状态机、localStorage 读写（版本化 + 损坏兜底重置）、诊断 task 文本构建。
2. `src/composables/useAlertTrigger.ts`（vue 接线）：watch `monitorStore` 导出 ref（`current/cpuHistory/memHistory`，`server-monitor.ts:359-374`）；`agentStore.isRunning` / `appStore.backendStatus` 前置拦截；建会话 + runAgent；触发 toast。
3. 默认三条规则 + 每主机开关（v1 无规则编辑 UI——预置规则 + localStorage 手改即可跑通主链路）。

**明确 defer：**
- 规则管理面板（阈值可视化编辑）→ v2；v1 规则集经 localStorage 键维护。
- 后端持久化规则/冷却（`app_setting`，先例 `backend/agent/schedule_engine.py:109` `db.get_app_setting`）→ v2；届时写文件必须 tmp+rename（gate.py:411-437 先例）。
- 通知渠道（webhook 等，巡检已有 `_notify_webhook` 可参照 `schedule_engine.py:102-127`）→ v2。
- 诊断会话自动跳转导航：v1 只 toast；pendingPrefill 旁路（`src/stores/ssh-ai.ts:410,546-551` + `SshView.vue:352-358` watch）留给集成者评估是否同轮接。
- 多窗口场景：本应用为单渲染进程 Electron，不设计。

### 1.5 测试计划（新增文件与守卫点）

| 文件 | 守什么 |
|---|---|
| `src/utils/alert-trigger-core.test.ts` | 纯逻辑：①连续 N 次越限计数升/降级与重置（切主机/stop 清零）；②**全 0 CPU 样本不计入**（computeCpuDiff 契约对齐）；③冷却期内不重复触发、冷却以触发时刻起算；④每主机开关生效；⑤task 文本包含主机名/规则/越限指标且 ≤4000 字符；⑥localStorage 损坏 JSON 兜底重置 |
| `src/composables/useAlertTrigger.test.ts` | 接线：①`agentStore.isRunning` 时不发起且不重置冷却；②建会话参数（mode='ssh'）与 runAgent 参数（专用 conversationId、sshSessionId=current.sshId、environment='test'、permissionMode='normal'）；③runAgent 抛错/409 时静默放弃、in-flight 标志释放；④polling=false 间隙不触发 |

---

## 2. 功能 B · run 级一键回滚

### 2.1 设计摘要

Saddle 已在每次有副作用的工具调用**执行前**构建 Undo 包并入栈（`backend/agent/harness/saddle.py:319-321`），run 结束时栈内仅剩实际生效的净副作用（失败工具按身份移除 `saddle.py:380-389`、pending_delivery 未执行的移除 `saddle.py:397-398`，均已核实）。三种包形态全 JSON 可序列化：`ShellUndo{command, reverse_command|None, ssh_session_id}`（`saddle.py:97-99`）、`FileContentUndo{path, ssh_session_id, snapshot_b64|None, existed}`（`saddle.py:136-139`，快照上限 200KB 原始字节 → base64 约 267KB，`ops_common.py:241` `B64_READ_MAX_BYTES=200*1024` 已核实）、`NoOpUndo{reason}`（`contracts.py:222-228`，reason ∈ readonly/sandbox_blocked/file_no_path/file_write_requires_ssh/file_snapshot_unavailable）。

**两个不可妥协的结论（源码核实）：**
1. **现有 undo 执行通道是治理旁路**：`ShellUndo/FileContentUndo.apply_reverse` 直连 `ElectronBridge.exec_ssh_command`（`saddle.py:115-119,157-161`），不经 `registry.execute` → 无 FilesystemGuard/PermissionGate → **无审批卡、无 denied/executed 审计**。FAILED run 的 `undo_all` 已在用这条旁路（`driver.py:1684-1686`）。因此**一键回滚绝不能复用 `apply_reverse`/`undo_all` 作为产品功能**——回滚必须以**新 Agent run** 走 `POST /agent/run` 全链：`await_approval=True`（`routes/agent.py:1107`）→ 审批等待闸（`driver.py:1361-1461` + `reins/approval_wait.py:52-122`）→ FilesystemGuard dangerous 分类弹卡（`reins/guards.py:96-98`）→ grant 写前审计 critical=True（`routes/agent.py:1742` 起）→ AuditLogger 落 executed/failed。反向命令本身被判 dangerous（如 cp 的反向 rm）正好由弹卡消化。
1a. **snapshot 级还原需要受治理的读取/执行通道**（评审定案）：`snapshot_b64` 单行可达 ~267KB（200KB 原始字节，`ops_common.py:241`），而回滚 run 唯一文本输入 task 被 `AgentRunRequest.task` 钉死 50000 字符（`routes/agent.py:398`），快照既塞不进 task_text，也**绝不许**进 LLM 上下文（token 成本与截断风险）。解法是新增专用工具 `undo_restore`（注册进工具面，§2.4）：由回滚 run 的模型按 plan 给出的 `(run_id, seq)` 逐项调用，工具实现内部从 undo_store 读快照（不经过模型上下文）并分块写盘还原；工具 `meta.risk_level="dangerous"`（`ToolMeta` 定义 `tools/__init__.py:29-44`；`gate.py:995-1000`：dangerous → 每次授权、不进 session 级缓存）→ 照常弹审批卡，执行经 AuditLogger 落链——治理语义与 file_write 的既有路径同构（`ops_file.py:114-124` `_write_bound_command` 的「内容级判定」先例）。工具内部做**归属校验** fail-closed：调用 run 必须登记在 `run_rollbacks.rollback_run_id`，且 `(run_id, seq)` 必须属于该行的 `source_run_id`，否则拒绝执行。
2. **采集时序**：FAILED run 的栈在 `_emit_done` 开头先被执行再清空（`driver.py:1684-1686` → `stirrups.py:256-259` clear，均已核实）。序列化必须插在 `_emit_done` **最开头、`undo_all` 之前**，FAILED run 的条目标 `status='auto_rolled_back'`（照常落库供审计展示，但回滚 UI 排除）。

**可信度派生**（代码中无现成 confidence 字段，从形态推导，集中在 undo_serial）：`snapshot`（FileContentUndo 且 existed+snapshot，内容级还原）> `derived`（ShellUndo 且 reverse_command 非空——已经过 `_shell_reverse` 的元字符拒绝 + 二次 classify_command 过滤，`saddle.py:230-280` 已核实）> `delete_created`（existed=False，反向是 `rm -f`，标 destructive 告警）> `manual`（ShellUndo reverse=None / NoOpUndo——不进回滚任务，仅展示）。子 Agent 的 ctx 独立 undo 栈但工具面限 READONLY_TOOLS，恒 NoOp，无需合并（设计文档记录即可）。

### 2.2 数据模型与存储

新建 `backend/undo_store.py`（B 独占），两张表，幂等建表 + 参数绑定：

```sql
CREATE TABLE IF NOT EXISTS run_undo_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_run_id TEXT NOT NULL,                -- FK agent_runs(id) ON DELETE CASCADE
    seq         INTEGER NOT NULL,              -- 栈内序号（副作用发生序）
    kind        TEXT NOT NULL,                 -- shell | file | noop
    tool        TEXT NOT NULL DEFAULT '',      -- 产生副作用的工具名
    command     TEXT NOT NULL DEFAULT '',
    reverse_command TEXT,                      -- NULL = 无反向
    path        TEXT,
    snapshot_b64 TEXT,                         -- 单行可达 ~267KB
    existed     INTEGER,
    reason      TEXT,                          -- NoOpUndo reason
    confidence  TEXT NOT NULL DEFAULT 'manual',-- snapshot|derived|delete_created|manual
    ssh_session_id TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT '',      -- ctx.target_host（回滚时定位主机）
    environment TEXT NOT NULL DEFAULT 'test',
    run_status  TEXT NOT NULL DEFAULT '',      -- 采集时 run 终态
    status      TEXT NOT NULL DEFAULT 'pending',-- pending|auto_rolled_back|rolled_back
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_run_undo_run ON run_undo_actions(agent_run_id, seq);

CREATE TABLE IF NOT EXISTS run_rollbacks (
    id              TEXT PRIMARY KEY,
    source_run_id   TEXT NOT NULL,             -- FK agent_runs(id) ON DELETE CASCADE
    rollback_run_id TEXT NOT NULL,             -- 回滚 run（交互式 /agent/run 生成的新 run）
    status          TEXT NOT NULL DEFAULT 'running',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- **放新表而非 agent_runs 加 JSON 列**的理由：undo 与 run 是 1:N；snapshot_b64 单行 ~267KB 会拖重列表查询（列表走固定投影 `_RUN_LIST_COLUMNS`，`backend/database.py:2152-2155`，已核实）；级联清理沿 `prune_agent_runs` 的 FK CASCADE 先例（`database.py:2913-2921` 注释明示 tool_calls 同法）。
- **headless run 防孤儿**：schedule/runbook 无头 run 也会流经 `driver._emit_done`，但其 `create_agent_run` 从未被调用（全仓唯一调用点 `routes/agent.py:1009`，grep 已运行核实）→ `undo_store.record` 写入前先查 `agent_runs` 行存在，不存在则跳过（fail-closed，双保险防 FK 噪音）。
- **FK 强制开启 + prune 竞态防护**（评审核实补正）：`database.py:80` `_create_connection()` 对每条连接 `PRAGMA foreign_keys=ON`（本轮已核实）——FK CASCADE 真实生效，同时「存在性检查 → 插入」之间 `agent_runs` 行可能被 `prune_agent_runs` 竞态删除，INSERT 将抛 `IntegrityError`。因此 `record_run_undo` 按 **run 级整体 catch** `sqlite3.IntegrityError`（整批放弃 + warning + 返回 False），调用方（driver）已有 warning 不阻断语义；竞态用例进 `test_undo_store_persistence_and_cascade.py`。
- 写入走 `asyncio.to_thread`（与 `routes/agent.py` 全部 db 调用同法）；单 run 一次事务批量插入；snapshot 超过 512KB 时截断拒绝入库该行并记 warning（防多文件编辑 run 撑爆 DB，v1 上限）。

### 2.3 API 契约

**后端（全部进新文件 `backend/routes/rollbacks.py`，`APIRouter(tags=["rollbacks"])`，路径避开 `/agent/*` 前缀以免与 agent.py 混淆；鉴权由 main.py 的 X-API-Token 中间件统一覆盖 `/api/*`，`backend/main.py:219-235` 已核实，本轮零接线成本）：**

| 端点 | 语义 |
|---|---|
| `GET /api/rollback/runs/{run_id}/undo-actions` | 返回该 run 的 undo 明细：`{run_id, run_status, terminal, items:[{seq,kind,tool,command,reverse_command,path,existed,confidence,warning,status,host,environment}]}`。仅 `status='pending'` 且 run 处于终态（`completed/human_help/max_reached`，FAILED 已 auto_rolled_back 排除）可回滚；无记录 404。**响应不含 `snapshot_b64`**（大字段不出库、不进前端、不进模型） |
| `POST /api/rollback/plan` | body `{source_run_id}`。服务端组装回滚任务文本（LIFO 序、逐项描述+可信度+危险标记、排除 manual 项、内嵌 source_run_id 与目标 host/environment；**file 项只写 `(run_id, seq)` 引用，快照内容留给 undo_restore 工具**），返回 `{source_run_id, conversation_id, task_text, host, environment, ssh_session_hint, items}`。**只组装不执行**——执行由前端走既有 `POST /api/agent/run`（治理全链），本端点不碰 harness。`conversation_id` 取自 `agent_runs.conversation_id` 列（`database.py:255-256`，plan 本就要读 agent_runs 做存在性/终态判定，零额外成本） |
| `POST /api/rollback/register` | body `{source_run_id, rollback_run_id}`。落 `run_rollbacks` 关联行（幂等：同对已存在则原样返回）。前端在回滚 run 的首个 `run_id` SSE 事件到达后调用（`agentStore.lastRunId` 已捕获，`src/stores/agent.ts` snapshot `:430` 同源字段）。register 亦是 undo_restore 工具归属校验的数据前提 |

**工具面新增（治理读取/执行通道，评审意见 1 定案）：**

| 工具 | 契约 |
|---|---|
| `undo_restore(run_id, seq)` | 新内置工具（`backend/agent/tools/builtin/undo_restore.py`），`meta = ToolMeta(risk_level="dangerous", reversible=True, idempotent=False)` → `gate.py:995-1000`：dangerous 每次授权 → **照常弹审批卡**；执行经 AuditLogger 落 executed/failed。归属校验 fail-closed：`ctx.run_id` 必须登记于 `run_rollbacks.rollback_run_id` 且 `(run_id, seq)` 属于该行的 `source_run_id`，否则拒绝。执行：从 undo_store 读条目（`asyncio.to_thread`）→ `existed=True` 且有快照 → **分块 base64 还原**（首块 `printf '%s' '<chunk>' \| base64 -d > path` 截断写，后续块 `\| base64 -d >> path` 追加，单块 b64 ≤ 120_000 字符防 Linux `MAX_ARG_STRLEN`（128KB/参数）——`ops_file.py:91-94` 的 ARG_MAX 危害已核实；末尾复用 `_verify_remote_write`（`ops_file.py:85-111`）做 `wc -c` 回验，不符判失败）；`existed=False` → `file_delete_cmd`（`ops_common.py:192-195`）。快照**全程不进模型上下文、不进 task**。本工具自身副作用不产 undo（UndoBuilder 对未知工具派 NoOpUndo，`saddle.py:193`；再还原=再调一次 undo_restore 对同一快照，幂等语义成立） |

**SSE 事件：零新增。** 回滚 run 就是普通交互式 run：审批卡（`permission_needed` → ApprovalDock，`src/stores/agent.ts:740-803`）、`done`/`error` 终态、成本快照全部既有。

**前端调用链：**

```
AgentFullTrace（元信息条下方新挂 RollbackPanel，仅当 run 有 undo 数据时渲染）
  └─ rollbackApi.getUndoActions(runId)            // src/api/rollback.ts（新）
       ├─ 渲染逐项：confidence 徽章 + 危险标记（delete_created 反色提示）
       ├─ 「一键回滚」按钮 → rollbackApi.plan(runId) 拿 task_text + conversation_id
       │    ├─ 校验：当前存在指向 host 的活动 SSH 会话（v1 fail-closed：
       │    │   无会话则按钮禁用并提示「请先连接目标主机」——undo 里的
       │    │   ssh_session_id 是临时会话号，可能已失效，不做自动重连）
       │    ├─ agentStore.runAgent({ task: task_text,
       │    │     conversationId: plan.conversation_id,   // ← 原会话，来源见下
       │    │     sshSessionId: <匹配会话>, environment: plan.environment, … })
       │    │    // shell 项：模型 shell_exec 反向命令 → dangerous 照常弹卡
       │    │    // file 项 ：模型调 undo_restore(run_id, seq) → dangerous 弹卡，
       │    │    //           快照从 undo_store 直达远端，不过模型上下文
       │    └─ agentStore.lastRunId 就绪 → rollbackApi.register(source, rollbackRunId)
       └─ run_rollbacks 关联在轨迹页可见（RollbackPanel 显示「本 run 是 X 的回滚」）
```

- **conversationId 数据来源**（评审核实补正）：plan 响应携带（后端从 `agent_runs.conversation_id` 直读）；前端亦可交叉核对 `agentAdminApi.getRunTrace(runId).run.conversation_id`（类型 `src/api/agent.ts:358` 已核实，`AgentFullTrace.vue:117` 已加载该 trace）。**回滚消息落原会话、对用户可见**（routes/agent.py:1032-1039 用户消息 + :1324-1374 assistant 消息都写进该 conversation）——这是有意设计：回滚过程与原任务同处一地可追溯；代价是原会话若有活动 run 会吃 409 硬闸（`routes/agent.py:990-997`），前端走 onError 静默提示、等其结束后重试。

- **不修改 `AgentRunsTable.vue`**（三列契约 `AgentRunsTable.vue:10-12` 与后端投影对齐、`approval-trace-boundary.test.ts:103` 锁定展开行结构，均已核实）——入口只挂 AgentFullTrace 元信息区，其本身已被展开行内嵌（`AgentRunsTable.vue:193-199` `<AgentFullTrace :run-id="r.id" />`）与消息气泡「轨迹」直达复用，一处修改两处生效。
- 前端 API 走 `src/api/rollback.ts`（新），从 `@/api/base` import `request`；**不改 `src/api/client.ts`**（B 实现者不 import client.ts 的新增导出，由集成者在 sharedWiring 决定是否 re-export）。

### 2.4 本轮实施范围

**做：**
1. `backend/agent/harness/undo_serial.py`（新）：三种 Undo 的 `to_dict/from_dict`、confidence 派生、`snapshot_stack(ctx) -> list[dict]`（含 tool 名来源：`ctx.tool_calls_log` 与栈序对齐，取不到时留空）。
2. `backend/undo_store.py`（新）：两表幂等建表 + record/get/register/list + agent_runs 存在性闸 + run 级 `IntegrityError` 整批 catch（prune 竞态防护，§2.2）。
3. `backend/agent/harness/driver.py`（**既有文件修改，最小插入**）：`_emit_done` 开头（`driver.py:1683` 处、FAILED 分支 `undo_all` 之前）一行调用 `undo_store.record_run_undo(ctx, final_state)`（惰性 import，`asyncio.to_thread` 包裹，**失败仅 warning 不阻断 run**——undo 持久化是增强，不允许它拖死主流程）。done payload 追加 `"undo_recorded": bool`（纯透传，routes 无需改动）。
4. `backend/agent/tools/builtin/undo_restore.py`（新）+ `backend/agent/tools/builtin/__init__.py`（注册清单 +2 行：import + `registry.register`，统一清单结构见 `:17-58,108-120`，已核实）：受治理的快照还原工具，契约见 §2.3。
5. `backend/routes/rollbacks.py`（新）：三端点。
6. `src/api/rollback.ts` + `src/components/agent/RollbackPanel.vue`（新）+ `AgentFullTrace.vue` 挂载（约 3 行）。

**明确 defer：**
- 自动重连目标主机（参照 schedule_engine 的 `bridge.connect_ssh` 建连，`schedule_engine.py:511-559`）→ v2；v1 要求已有活动会话（fail-closed，杜绝「回滚命令打到错误机器」）。
- 回滚 run 的逐项执行进度实时上报（部分失败继续的执行器）→ v2。现有 `stirrups.undo` 吞错语义（`stirrups.py:253-254` 仅 warning）是旁路执行器的产物，**不复用**；回滚 run 内模型逐项执行、失败反馈照常回给模型处置（`driver.py:850-858` 既有反馈通道），逐项成败随 tool_calls 落库天然可见。
- 回滚前远端状态 diff 校验（时间旅行防护）→ v2 产品决策项；v1 靠审批卡 + run 关联审计 + 模型对工具失败的处置兜底，文档明示该缺口。
- AgentRunsTable 行级「可回滚」标记列（牵动三方列契约）→ v2。
- FAILED run 的「再回滚」：已 auto_rolled_back，仅审计展示。

### 2.5 测试计划（新增文件与守卫点）

| 文件 | 守什么 |
|---|---|
| `backend/tests/test_undo_serialization_roundtrip.py` | 三种 Undo 编解码保真（snapshot_b64/existed/reverse_command=None 语义不丢）；confidence 派生矩阵（existed=False→delete_created、reverse=None→manual…）；元字符路径不被 reverse 携带 |
| `backend/tests/test_undo_store_persistence_and_cascade.py` | record/get 幂等与 seq 有序；**agent_runs 行不存在（headless run）时跳过**；FK ON DELETE CASCADE 随 prune_agent_runs 级联清理（`PRAGMA foreign_keys=ON` 已核实 `database.py:80`）；**存在性检查后、插入前 agent_runs 被 prune 删除的竞态 → run 级 catch IntegrityError、整批放弃且不抛**；snapshot 超限拒写该行 |
| `backend/tests/test_driver_undo_persist_before_rollback.py` | **最高危时序**：FAILED run 下 record 发生在 `undo_all` 之前（栈非空快照），条目 status='auto_rolled_back'；COMPLETED run 条目 status='pending'；持久化抛异常时 run 主流程不受影响（done 事件照发） |
| `backend/tests/test_rollback_routes_plan_and_register.py` | plan：LIFO 序、manual 项被排除、task_text 内嵌 source_run_id/host/conversation_id；**task_text 长度有上限且响应与 items 均不含 snapshot_b64（大字段不出库）**；非终态 run / 无 pending 记录 404/400；register 幂等（重复同对不重复插行）；参数绑定（无字符串拼接 SQL） |
| `backend/tests/test_undo_restore_tool_governance.py` | **undo_restore 治理契约**：非回滚 run 调用（ctx.run_id 不在 run_rollbacks）→ 拒绝；`(run_id,seq)` 不属于登记的 source_run_id → 拒绝；existed=True 分块还原（单块 ≤120_000 b64 字符、末块追加）后 `wc -c` 回验字节数一致；回验不符判失败；existed=False → rm -f；meta.risk_level='dangerous'（进 gate dangerous 授权路径）且 idempotent=False；快照内容不出现在工具参数与结果输出中 |
| `src/api/rollback.test.ts` | rollbackApi 三方法的 URL/method/body 形状；非 200 走 FriendlyError |
| `src/components/agent/RollbackPanel.test.ts` | 仅 pending+终态时显示按钮；无匹配活动 SSH 会话时按钮禁用且有提示（fail-closed）；逐项 confidence 徽章与 delete_created 危险标记渲染；点击回滚按序调用 plan→runAgent→register，conversationId 取 plan.conversation_id |

---

## 3. 功能 C · 可执行 Runbook

### 3.1 设计摘要

现状：`runbook_save/search` 把剧本当**纯文本**沉淀进 memories 表（`kind='runbook'`，写入 `backend/agent/tools/builtin/runbook_tools.py:133-141`，同名顶替 `:147-153`，检索双路 FTS+向量 `:190-194`，均已核实）。两条硬伤：memories 无结构化列（`backend/database.py:613-636` 已核实）；`mstore.decay_memories` **无 kind 过滤**（UPDATE 的 WHERE 只有 status/pinned/use_count/时间，本轮已核实源码）——长期零命中会被降权到 0.2 地板后 retire，可执行剧本被静默作废。且文本剧本没有可执行的断言与停机语义。

**执行引擎选型：逐步骤驱动、每步一次无头 run、断言由 executor 确定性判读**（否决「整段交给 agent」：断言失败即停会退化成 prompt 约束、无调用方硬停点、步骤进度不可靠）：

- 每步发一次轻量无头 run，prompt 固定模板：「执行命令 X；随后执行验证命令 A；最末单独一行输出 `[步骤结论] 通过` 或 `[步骤结论] 失败: <差异说明>`」。executor 用标记行正则判读（复刻巡检先例：`_INSPECT_VERDICT_RE` + `_extract_inspect_verdict`，`backend/agent/schedule_engine.py:58-92` 已核实），**零新增 LLM 调用、不依赖模型自觉**。
- **拦截即失败**：headless run `await_approval=False`（`harness.py:177-203` docstring 明示「无人值守……拦截即返回」，已核实），步骤里的非 safe 命令会被 reins 拦截返回而非挂起——executor 把拦截当作**步骤失败**转诊断，绝不当作执行成功。
- **失败即停**：某步断言失败 → 终止后续步骤 → 自动发起一次**诊断 run**（无头，prompt 携带失败步骤命令/输出），诊断结论写进执行历史的 diagnosis 字段；前端另有「AI 诊断」按钮复用既有 pendingPrefill 通路（`src/views/ssh/SchedulesView.vue:688-699` askAiDiagnose → `sshAiStore.askAi`，已核实）。
- **防旁路**：断言/验证命令必须随步骤 run 走 harness（审计落链、命令分类统一），executor **不得**直接调 `ops_common.run_headless`（`ops_common.py:149-184` 已核实——它绕过 Reins PRE 检查与审计，是本功能明确禁止的诱惑）。
- **无头 run 装配：独立模块、暂不动 schedule_engine**（评审意见 2 定案）：v1 新建 `backend/agent/headless_run.py` 承载装配逻辑（模型解析+assemble、bridge probe→connect→disconnect、`resolve_run_environment` 环境判定、harness.run 迭代与 done 收集，语义对齐 `schedule_engine._run_agent_once` `:466-639`），但 **v1 不改 `schedule_engine.py`**（原方案「抽取+委托」defer 到 v2，理由见 §7-2：schedule_engine 是全库事故痕迹最密的热文件，P0 稳定性优先于 DRY）。漂移风险用两道手段缓解：①`headless_run.py` 模块注释**钉死语义母本行号**（`schedule_engine.py:466-639`）并声明「改母本必须同步本文件」；②引擎测试（§3.5）把可观察契约钉死：环境只收紧、桥不可达 fail-closed、任务结束必关会话、done 终态消费。
- **执行代价（明示）与 SSH 会话复用**：逐步骤架构意味着 N 步 = N 次 LLM 调用（这是「断言确定性判停」语义的固有代价，≤20 步上限封顶）；SSH 连接代价则收敛为**每次剧本执行 1 次建连**：`headless_run` 支持「预建会话」入参——executor 先 probe→connect 一次，跨步骤复用同一 ssh_session_id，finally 统一断开；工具步骤的命令仍经该会话的 harness 全链执行。不复用会导致 20 步剧本 = 20 次串行 probe→connect→disconnect（`schedule_engine.py:511-559` 路径），评审已指认此代价，v1 必须按复用实现。
- **执行历史自建表**：headless run 不写 agent_runs（grep 已核实）也不写会话消息，历史必须落自建 `runbook_runs`（镜像 `schedule_runs`，`database.py:405-416`），并复刻两道收尾防滞留：启动收尸 `_reap_stale_runs`（`schedule_engine.py:191-214` 先例）+ CancelledError 落终态（`:419-436` 先例）——否则后端重启后执行永远停在 running，前端按钮永久置灰（SchedulesView isRunning 的同款教训）。
- **并发防重入**：per-runbook `_executing` set + 手动触发 409（`routes/schedules.py:174-178` 先例）+ 全局并发闸（上限 4，`_MAX_CONCURRENT_SCHEDULE_RUNS` 同值同注释，S7-05 事故教训 `schedule_engine.py:38-42`）。

### 3.2 数据模型与存储

新建 `backend/runbook_store.py`（C 独占），两张表，幂等建表 + 参数绑定：

```sql
CREATE TABLE IF NOT EXISTS runbooks (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,                 -- ≤100 字（对齐 schedules 命名上限 routes/schedules.py:25）
    description TEXT NOT NULL DEFAULT '',
    target_host TEXT NOT NULL DEFAULT '',      -- 连接 ID（同 schedules.target_host 语义，schedule_engine.py:473-475）
    steps_json  TEXT NOT NULL,                 -- 步骤数组 JSON，见下
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_runbooks_enabled ON runbooks(enabled, updated_at);

CREATE TABLE IF NOT EXISTS runbook_runs (
    id          TEXT PRIMARY KEY,
    runbook_id  TEXT NOT NULL REFERENCES runbooks(id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL DEFAULT '',      -- 最后一个无头 run 的 run_id（审计关联键）
    status      TEXT NOT NULL DEFAULT 'running',-- running|success|failed|stopped|stale_reaped
    current_step INTEGER NOT NULL DEFAULT 0,
    steps_state TEXT NOT NULL DEFAULT '[]',    -- 每步 {seq,status:'success|failed|blocked|skipped',command,assert_cmd,verdict,run_id,error}
    diagnosis   TEXT NOT NULL DEFAULT '',      -- 诊断 run 结论
    error       TEXT NOT NULL DEFAULT '',
    started_at  TEXT, finished_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_runbook_runs_rid ON runbook_runs(runbook_id, created_at);
```

`steps_json` 元素：`{ "command": str(≤2000), "assertCommand": str(≤2000, 可空=仅执行不断言), "expect": "通过标记行即可，不做期望值比对（v1）", "note": str(≤500) }`，单剧本 ≤20 步。**不存 memories**（decay 陷阱）；`runbook_search` 检索双写摘要进 memories 的方案 defer（两处一致性成本 > v1 收益，且检索路 `runbook_tools.py:190-194` 无需改动照常工作——只是检索不到新表里的剧本，v1 接受，文档明示）。

审计：步骤级命令审计由 harness 内置 reins→audit 自动落链（run_id 贯通，`audit_log` 查询维度 `backend/agent/audit.py:231` `idx_audit_run`，已核实）；executor 级事件以 `append_record_async` 记录（`audit.py:337-342`），`action='runbook_step'/'runbook_run'`，**conclusion 只用白名单既有值** `executed/failed/blocked`（§0.2 第 7 条，静默改写陷阱 `audit.py:265-275`），剧本结论细节放 `result` 字段。

### 3.3 API 契约

**后端（全部进新文件 `backend/routes/runbooks.py`，完全镜像 `routes/schedules.py` 的结构与防御——命名/长度上限、`_validate_fields` 式校验、409 语义、桥探活前置 `routes/schedules.py:164-173` 先例）：**

| 端点 | 语义 |
|---|---|
| `GET /api/runbooks` | 列表（含 last_run 摘要联查） |
| `POST /api/runbooks` | 创建；校验 steps_json 形状/上限（command/assertCommand/note 长度、≤20 步、command 必填非空） |
| `PATCH /api/runbooks/{id}` | 局部更新（exclude_unset + `_drop_nulls` 式防御，先例 `routes/schedules.py:71-78`） |
| `DELETE /api/runbooks/{id}` | 删除（FK 级联清历史；执行中则 409 拒绝） |
| `POST /api/runbooks/{id}/run` | 一键执行。前置：桥探活失败 503（照抄 `routes/schedules.py:164-173`）；执行中 409（`_executing` set）；触发即返回 `{ok, runbook_run_id}`，前端轮询 |
| `GET /api/runbooks/{id}/runs?limit=` | 执行历史（steps_state 全量返回） |

**无 SSE**（无头执行无流可看）；前端沿用 SchedulesView 的 runNow 后 3s 短轮询 + 激活期 30s 静默轮询节奏（`src/views/ssh/SchedulesView.vue:369,555-577` 已核实存在）。

**前端调用链（新文件 `src/api/runbook.ts`，从 `@/api/base` import `request`）：**

```
SchedulesView 顶部分段：[定时任务 | 运维剧本]        ← C 唯一的既有文件修改（~15 行）
  └─ RunbookPanel.vue（自包含新组件，卡片+编辑 modal+历史 modal，样板 SchedulesView）
       ├─ runbookApi.list() → 卡片列表（「执行」「历史」「编辑」）
       ├─ runbookApi.runNow(id) → 3s/30s 轮询 runbookApi.runs(id)
       ├─ 步骤级状态渲染：steps_state → 每步 ✓/✗/拦截/跳过 + verdict 摘要
       ├─ 失败 run「AI 诊断」→ runbook_engine 已写 diagnosis 字段（自动诊断结果）
       │   另可复用 pendingPrefill 手动追问（SchedulesView.vue:688-699 同法）
       └─ 编辑 modal：步骤增删排序 + 逐字段校验（command 必填、长度上限）
```

**入口决策**：新独立页需改 5 个共享文件（`useSshNavigation.ts` NavPage 联合 `:18-19`、SshView.vue 模板/PAGE_TOGGLES、App.vue 事件转发、TitleBar.vue emits），其中 useSshNavigation.ts 是**硬禁改文件**且 `useSshNavigation.ts:14-27` 注释自述漏改任意一处「图标不亮且类型检查可过」的教训——v1 收敛为 SchedulesView 内 tab，共享文件改动 = 0。

### 3.4 本轮实施范围

**做：**
1. `backend/agent/headless_run.py`（新）：无头 run 装配（模型解析、bridge 探活/建连/断连、`resolve_run_environment`、HarnessAgentConfig、done 收集），返回 `(done_content, error_msg, done_state, host, environment)`；**支持预建会话入参**（executor 复用同一 ssh_session_id 跨步骤，`ssh_session_id` 为空时才自建自关——语义对齐 `schedule_engine.py:511-559`）；模块注释钉死母本行号与同步责任（§3.1）。
2. `backend/runbook_store.py`（新）：两表 + CRUD + reap_stale + mark 终态。
3. `backend/runbook_engine.py`（新）：executor 全逻辑（每执行 1 次建连复用跨步骤、步骤模板、标记行正则 `_RUNBOOK_VERDICT_RE`、拦截=失败、失败即停、诊断 run、并发闸、取消落终态、`append_record_async` 剧本级事件）。
4. `backend/routes/runbooks.py`（新）：六端点。
5. `src/api/runbook.ts` + `src/components/schedules/RunbookPanel.vue`（新）+ `SchedulesView.vue` tab 挂载（最小改动）。

**明确 defer：**
- **schedule_engine 抽取/委托改造**（评审意见 2 定案移出 v1）→ v2：`schedule_engine.py` 零改动；headless_run.py 为语义对齐的独立实现，母本同步责任写在模块注释，收敛改造留待专门一轮（届时 schedule 回归测试做证据）。
- 期望值比对断言（`ops_file._verify_remote_write` 式 wc -c 回验比对，`backend/agent/tools/builtin/ops_file.py:85-111` 先例）→ v2（v1 断言=验证命令+通过标记行，已覆盖「跑完且检查通过」主语义；写盘类步骤的字节级回验由 headless_run/工具层既有逻辑承担）。
- runbook 摘要双写 memories 以进入 runbook_search 检索 → v2（一致性成本）。
- 独立导航页 RunbooksView → v2（牵 5 个共享文件，含硬禁改的 useSshNavigation.ts）。
- 定时触发的剧本（与 schedules 引擎对接）→ v2；v1 仅手动执行。
- 步骤间变量传递（前一步输出注入下一步）→ v2。

### 3.5 测试计划（新增文件与守卫点）

| 文件 | 守什么 |
|---|---|
| `backend/tests/test_runbook_store_crud_and_reap.py` | 两表 CRUD/校验；FK CASCADE；**reap_stale 把滞留 running 落 stale_reaped**（重启收尸契约）；mark 终态幂等 |
| `backend/tests/test_runbook_engine_step_verdict_stop.py` | 标记行正则判读（通过/失败/缺标记=fail-closed 判失败）；**拦截（blocked）当步骤失败而非成功**；失败即停后续步骤置 skipped；诊断 run 被触发且 prompt 携带失败现场；并发闸（同 id 二次触发拒绝）；CancelledError 落终态 |
| `backend/tests/test_runbook_engine_headless_shared.py` | headless_run 装配契约：bridge 不可达时明确报错不降级；environment 走 `resolve_run_environment` 只收紧；**预建会话模式下不自建不断开（复用由 executor finally 关闭）、自建模式下任务结束必关 SSH 会话（finally）**；`run_id` 传入 harness 供审计贯通 |
| `backend/tests/test_runbook_routes_validation_and_conflict.py` | steps_json 形状/上限 400；执行中 409；桥探活失败 503；历史 limit 夹取；SQL 全参数绑定 |
| 既有回归（冒烟，非 C 责任） | C 本轮**零改动** `schedule_engine.py`（评审意见 2 处置，§7-2），schedule 回归（`test_schedule_engine_stability.py` 等 11 个 schedule 测试文件，`backend/tests/` ls 实测）由集成者合并时照常跑，作环境健康冒烟 |
| `src/api/runbook.test.ts` | 六方法的 URL/method/body 形状与 limit 夹取 |
| `src/components/schedules/RunbookPanel.test.ts` | 步骤编辑校验（command 必填/长度/≤20 步）；执行中按钮禁用 + 轮询启停；步骤状态徽章渲染（success/failed/blocked/skipped）；历史 modal 挂 diagnosis 字段 |

---

## 4. 依赖与风险摘要（跨功能）

1. **A 依赖监控存活**：`useSshNavigation.ts:366-379` 的 monitoredTarget watch 是触发源的隐式前提（会话全关 → stop → 无样本）。A 外挂 watch 与其并行共存，双方零改动。
2. **A 审批兜底是预期行为**：审批卡 600s 无人处理按拒绝放行 run（`permission.approval_fallback_s` 默认 600，`backend/main.py:112-123` 已核实；env `CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S` 可覆盖）——告警诊断 run 弹卡时用户可能不在场，run 会带「曾被拒绝」继续。A 的 system_prompt 与用户文档必须写明。
3. **B 的 driver.py 是唯一热文件修改**：插入点单一（`_emit_done` 开头）、失败仅 warning、对 C 的无头 run 经存在性闸自动跳过——B 实现者若发现插入点漂移（如 `_emit_done` 签名变化），停下升级，不得换位置硬插。FK 强制开启（`database.py:80`）下的 prune 竞态由 undo_store 按 run 级 catch IntegrityError 兜底（§2.2）。
4. **C 的 headless_run 副本漂移风险**（评审意见 2 处置后的残留风险）：v1 不改 `schedule_engine.py`，`headless_run.py` 是语义对齐的独立实现——两份装配会在母本演进时漂移。缓解：模块注释钉死母本行号（`schedule_engine.py:466-639`）与同步责任 + 引擎测试把可观察契约钉死（环境只收紧/桥 fail-closed/会话必关/done 终态消费，§3.5）；抽取收敛在 v2 专门一轮执行（届时 schedule 回归测试做证据）。
5. **B 的 undo_restore 在 prod 的行为（2026-10-27 修订）**：交互会话（`await_approval=True`）里 dangerous 工具弹审批卡，由用户选「允许本次 / 始终允许」——「始终允许」写入主机限域的 allowlist；无人值守运行（定时任务 / headless / runbook）没人可点卡，dangerous 工具仍只接受预登记 allowlist（`prod_deny_no_allowlist`）。因此 prod 回滚在交互界面中可现场确认；若要把回滚放进定时任务/剧本，则需两步预登记。`existed=False` 的反向 `rm -f` 走同一矩阵。
5. **共享文件接线极小化**：B/C 各一条 include_router + A 一行挂载，全部集中在 §5。

## 5. sharedWiring（留给集成者统一执行）

| # | 文件 | 接线内容 | 来源功能 |
|---|---|---|---|
| W1 | `backend/main.py` | import `rollbacks` router 并 `app.include_router(rollbacks.router, prefix="/api")`（注册区 `main.py:261-268`） | B |
| W2 | `backend/main.py` | import `runbooks` router 并 `app.include_router(runbooks.router, prefix="/api")` | C |
| W3 | `src/views/ssh/SshView.vue` | setup 内一行 `useAlertTrigger()`（挂载即生效；该文件工作树已脏，由集成者合并） | A |
| W4 | `src/api/client.ts`（可选） | 若团队约定所有 api 模块经 client.ts 桶导出，则追加 `export * from './rollback'` 与 `export * from './runbook'`；不追加则消费方直接从各模块 import（AgentRunsTable 现状是经 client 引 `agentAdminApi`，`AgentRunsTable.vue:16-19`）——二选一，集成者定夺 | B/C |
| W5 | （无） | `backend/routes/agent.py`、`src/composables/useSshNavigation.ts` 本轮**零接线** | — |

## 6. 未验证项（如实声明）

以下为本设计**未运行/未核实**的部分，实现时须先核实再动手：
1. `useChatPersistence` 的会话列表合并刷新时机（`syncBackendConversations` 的调用点）未逐一追踪——A 的诊断会话在任务台列表何时可见需实现后手动验收；A 的 toast 不承诺「立即可见」。
2. `ctx.tool_calls_log` 与 undo 栈的序对齐精度（B 的 `tool` 字段来源）未做运行时验证，取不到时按空串落库（字段已可空）。
3. ~~PRAGMA foreign_keys 未读~~（评审核实补正，2026-10-27）：`database.py:80` `_create_connection()` 对每条连接 `PRAGMA foreign_keys=ON`——FK CASCADE 真实生效；伴随的 prune 竞态（存在性检查与插入之间 agent_runs 被删 → IntegrityError）已按 run 级整体 catch 设计进 undo_store.record 并有测试守卫（§2.2、§2.5）。
4. SQLite 单行 267KB（snapshot_b64）在目标设备上的写入性能未测，B 已设 512KB 截断上限兜底。
5. 全部结论来自源码阅读与 grep/git status/ls 实测（本次会话已运行），未运行任何测试套件与构建。

---

## 7. 评审意见处置（2026-10-27 评审，5 条全部成立、全部已处置）

| # | 意见 | 核实证据 | 处置 |
|---|---|---|---|
| 1 | B/high：snapshot 级回滚无执行通道（snapshot ~267KB 进不了 50000 字符 task；plan/undo-actions 不含快照；工具面无 undo_store 读通道；§2.5 缺 task_text 长度守卫） | `routes/agent.py:398` task max_length=50000（前轮已核）；`ops_common.py:241` B64_READ_MAX_BYTES=200KB（前轮已核）——意见与源码一致，成立 | **按「增加受治理读取通道」定案**：新增内置工具 `undo_restore(run_id, seq)`（§2.1 结论 1a、§2.3 工具契约、§2.4 范围 4）：meta.risk_level='dangerous'（`tools/__init__.py:29-44` + `gate.py:1000` dangerous→每次授权；prod 交互会话弹卡现场确认、无人值守只认预登记 allowlist，详见 §4-5 修订）→ 非 prod 与交互 prod 照常弹卡、无人值守 prod 需预登记（`RollbackPanel.prodAllowlistHint` 明示）；归属校验 fail-closed（ctx.run_id ∈ run_rollbacks）；快照从 undo_store 直达远端、**不进模型上下文不进 task**；分块写盘（单块 b64 ≤120_000 字符防 Linux MAX_ARG_STRLEN，`ops_file.py:91-94` ARG_MAX 危害已核实）+ `_verify_remote_write` 回验（`ops_file.py:85-111`）。plan/undo-actions 明确不含 snapshot_b64；task_text 长度与无泄漏守卫进 `test_rollback_routes_plan_and_register.py`；工具契约测试 `test_undo_restore_tool_governance.py` 新增。注册需改 `backend/agent/tools/builtin/__init__.py`（+2 行，统一清单 `:17-58,108-120` 已核实）——B 独占，A/C 不触碰 |
| 2 | C/medium：v1 体量偏满（抽取动 schedule_engine 热文件 + 完整编辑 modal + 7 测试文件；每步 probe→connect→disconnect 的代价未陈述；降级预案只覆盖抽取线） | `schedule_engine.py:466-639` 每次无头 run 自建自关会话（前轮已核）——意见成立 | **采纳「抽取移出 v1」**（§3.1、§3.4、§7-2）：`schedule_engine.py` 零改动（从 ownedFiles 移除），`headless_run.py` 为语义对齐的独立实现+母本同步责任注释+契约测试钉死；**编辑 modal 保留 v1**（否则 v1 无法定义剧本，「先定义再执行」主链路缺一环）。代价明示：N 步=N 次 LLM 调用（确定性判停的固有代价，≤20 步封顶）；SSH 连接收敛为**每执行 1 次建连**——headless_run 支持预建会话入参，executor 跨步骤复用、finally 统一断开（§3.1「执行代价」段）。抽取收敛排入 v2 专门一轮 |
| 3 | 全局/low：§6.3 称 PRAGMA foreign_keys 未读，实际 `database.py:78-87` 已设 ON；FK 下存在性检查与插入间的 prune 竞态会抛 IntegrityError，record 应 run 级 catch 并补测试 | `database.py:80` `conn.execute("PRAGMA foreign_keys=ON")`（本轮 Read 核实）——意见成立，我方 §6.3 原判断有误 | **修订**：§2.2 新增「FK 强制开启 + prune 竞态防护」（record 按 run 级整体 catch `sqlite3.IntegrityError`、整批放弃+warning+返回 False）；`test_undo_store_persistence_and_cascade.py` 补竞态用例；§6.3 改写为已核实事实 |
| 4 | 全局/low：§0.3 git status 清单漏 `src/stores/server-monitor-core.ts`（M 状态），A 以它为对齐基准，合并前行号可能漂移 | `git status --porcelain \| grep server-monitor`（本轮运行）输出两行均 M——意见成立 | **修订**：§0.3 清单补上该文件，并加集成者合并复核注记（`:134-154`、`:603-604` 2026-10-27 复核仍准确）；A 仍不修改该文件（对齐基准非修改对象） |
| 5 | B/low：回滚 run 的 conversationId 未写数据来源；应写明回滚消息落原会话、对用户可见 | `src/api/agent.ts:358` `AgentRunTrace.run.conversation_id?: string`、`AgentFullTrace.vue:117` 已加载 getRunTrace（本轮 Read 核实）；`agent_runs.conversation_id` 列 `database.py:255-256`（前轮已核）——意见成立 | **修订**：plan 响应增加 `conversation_id`（后端从 agent_runs 直读，前端可用 getRunTrace 交叉核对）；§2.3 明示回滚消息落原会话、对用户可见、原会话活动 run 时吃 409 走 onError 等待重试；`RollbackPanel.test.ts` 补 conversationId 取值守卫 |

无「不成立」条目——五条意见经源码核实均与代码事实一致，全部采纳修订；其中意见 2 在两条可选路径（移出抽取 vs 移出编辑 modal）中选择移出抽取，理由：schedule_engine 是 P0 schedules 的承载文件（S7-01/04/05 事故痕迹最密），抽取收益是 DRY、风险是回归，编辑 modal 缺失则 v1 无法创建剧本、主链路断裂——两害相权取轻。

---

## 8. 实施后终审处置（2026-10-27 独立终审：1 must + 11 should）

本轮修复（全部有测试或守卫钉住）：

| # | 处置 |
|---|---|
| M1 | **prod「拦截=blocked」失守**：拦截判定原先只认 `permission_needed`，而 prod 无人值守未预登记（`gate.py`/`guards.py` 的 `prod_deny_no_allowlist`）只发 warning、连续拒绝停机只发 `human_help_needed`——这些形态下模型末尾的「[步骤结论] 通过」可把没执行的步骤洗成 success。修法：deny 证据统一挂机器可读标记 `POLICY_DENY_MARKER`（gate/guards 的 warning payload 已补挂），`headless_run._is_deny_signal` 认标记判拦截（不匹配中文文案），`human_help_needed` 在 headless 下同归 blocked；回归用例 `test_deny_signal_covers_prod_warning_and_human_help` |
| S4 | `driver.py` 的 undo_recorded 注释失实（落库/消费当时确实无）——消费链闭环后已改写为如实描述 |
| S6 | `undo_restore._split_b64_chunks` 改 `b64decode(..., validate=True)`：默认坏字符静默丢弃、残缺内容照样通过 wc -c 回验，与「坏 base64 在此抛异常」的自述不符 |
| S7 | `headless_run` 的 `bridge.probe()` 补母本同款防御性 try + `classify_bridge_error`（唯一实质漂移点） |
| S8 | runbook 步骤审计的 environment 兜底 `or "test"` 改为留空交审计默认——prod 剧本建连前失败的步骤不再被记成测试环境 |
| S10 | `RunbookPanel.runNow` 的轮询定时器改为先登记再首拉：卸载落在 await 窗口内也能被 onUnmounted 统一清理 |
| S11 | `loadAlertConfig` 损坏兜底改为落盘重置：原先坏 JSON 每次挂载都重新解析失败，坏数据永留盘上 |
| S5 | A 的跨 tick 双发竞态：`useAlertTrigger` 增加跨 tick 单飞闸 `launchBusy`（建会话窗口内第二候选不再发起、不再 abort 先发流；runAgent 调用后即解除，由 isRunning 闸接管） |

遗留到 v2（设计级变更，不在本轮范围）：

| # | 遗留 | 原因 |
|---|---|---|
| S1 | FAILED run 的 undo 行无条件标 `auto_rolled_back`，但 `undo_all` 是 best-effort（反向命令失败仅 warning）——行状态可能与远端事实相反 | 需改 `undo_all` 返回逐项成败契约 + undo_store 状态机 + UI 文案，三处联动 |
| S2 | register（前端 HTTP）与回滚 run 首次 `undo_restore` 归属校验存在竞速；register 落后时工具 fail-closed 拒绝（安全方向正确），但用户会看到一次失败的回滚 | 需 `/agent/run` 请求体携带 rollback 关联、服务端建 run 事务内落 `run_rollbacks`，API 契约变更 |
| S3 | `undo_from_dict`/`list_rollbacks_for_source` 生产零调用（为 v2「本 run 是 X 的回滚」展示预留）；「回滚的回滚」路径无标注 | 展示功能属 v2；删除预留函数会让 v2 重新实现 |
| S9 | `RollbackPanel` 回滚 run 结束后不自动刷新条目状态（需重开轨迹页） | UX 增强，不涉正确性 |
