# 计划（Plan）功能 —— 设计与落点

> 写作时间：2026-09-22 17:0x
> **本文两部分**：§1–§4 是设计与落点；§5 是**已实现的部分**（只读强制层，新文件、可独立测、未注册即零行为变化）；§6 是**未接的线**与原因（工作区有另一条线在并发实现同一功能）。

---

## 0. 结论先行

- **plan 不是「一个选项」或「一段提示词」，是三者之和：权限状态 + 可评审制品 + 显式批准门。**
- 本仓库**已有一条进行中的实现**（`backend/agent/workflow.py` + `plan_write` 工具），制品形态（文件落盘）与注入方向是对的。
- **它缺的恰好是最关键的那一件：批准门。** 现在"批准"只是写在 tool description 里的一句自觉，没有任何机制承载；更严重的是 **未批准的 `plan.md` 已经被注入成"已确定的执行步骤"**。
- 缺的那一层可以完全落在**新文件**里，不需要改 `workflow.py`。

---

## 1. 主流怎么设计和实现

### 1.1 Claude Code：plan 是 permission mode

官方文档（`code.claude.com/docs/en/permission-modes`、`agent-sdk/permissions`）：

- **入口**：`Shift+Tab` 循环 / 单条 prompt 前缀 `/plan` / `claude --permission-mode plan`；项目默认可由 `permissions.defaultMode: "plan"` 设死。
- **强制只读**（原文）："Read-only tools run as in default mode. **File edits are never auto-approved in plan mode, even when an allow rule matches.**"
- **不放松安全**（原文）："Permission prompts still apply the same as default mode — plan mode is about deferring edits, not loosening safety."
- **批准 = 权限状态转换**：approving "exits plan mode and switches the session to whichever permission mode you picked"（auto / acceptEdits / 逐条审批）。四个选项里的第三个是 **"Keep planning with feedback"**，即不批就继续待在 plan。
- **制品是可编辑的文件**：plan 存 `~/.claude/plans`（设置项 `plansDirectory`）；**`Ctrl+G` 用编辑器改完再批**。
- `showClearContextOnPlanAccept`：批准时可以顺手清掉规划期的探索上下文 → 说明**规划上下文是一次性的**。
- 只读调研委托给内置的 **read-only Plan subagent**，在它自己的窗口里收集上下文 —— 避免把大段文件读进主上下文。

### 1.2 spec-kit：plan 是制品链中间的一级

`/speckit.plan` 读 `spec.md` + `constitution.md`，往模板里填出 `plan.md`：

- 章节：Technical Context（语言 / 依赖 / 存储 / 测试 / 平台 / 性能目标 / 约束 / 规模）、Constitution Check、Phase 0 `research.md`、Phase 1 `data-model.md` + `contracts/` + `quickstart.md`。
- 未知项**显式标 `NEEDS CLARIFICATION`** —— 未知是一等公民，不允许静默脑补。
- 违反 constitution → **报错**，除非在 Complexity Tracking 表里给出理由。
- 关键句（Microsoft Learn 教材）："**The plan drives task generation, so it needs to be correct before proceeding.**" → plan 是下游的输入契约，所以必须**先改对再往下走**。

### 1.3 要照抄的是这四条不变量（不是它们的 UI）

| # | 不变量 | 出处 |
|---|---|---|
| **I1** | plan 期间**不允许改状态** | 只读强制 / 只读 Plan subagent |
| **I2** | 产物是**可评审、可编辑的制品**（文件，不是聊天气泡） | `~/.claude/plans` / `plan.md` |
| **I3** | **批准是一个显式的门，批准动作本身改变权限** | approve → 切 permission mode |
| **I4** | **批准后的 plan 是执行的契约**，下游读它 | plan → tasks；每轮注入 |

---

## 2. 本仓库现状审计

### 2.1 已有的（另一条线在做，方向正确）

- `backend/agent/workflow.py`：制品文件落盘 `~/.ckunagentic/workflow/<conversation_id>/{spec,plan,goal}.md`；`get_phase()` 按"文件是否存在"取最高阶；`hydrate_for_session()` 渲染注入段（`[规格]` / `[计划]` / `[目标]`）。
- `backend/agent/tools/builtin/workflow_tools.py`：`spec_write` / `plan_write` / `goal_write`，`risk_level=safe`，已注册进 `tools/builtin/__init__.py` 的 `_ALL_TOOL_CLASSES`。

→ 对照不变量：**I2 已满足**（制品是文件，可 diff、可编辑）。

### 2.2 缺的

| 不变量 | 现状 | 判定 |
|---|---|---|
| **I1** 只读强制 | 无。`workflow.py` 自述"不做验收机…阶段推进与成功判定交由模型 + 用户决定，本模块不越权" | ❌ **缺** |
| **I3** 批准门 | 无状态位。`plan_write` 的 description 写"应等待用户批准计划后再动手执行" —— 这是**自觉，不是机制** | ❌ **缺** |
| **I4** 批准后作契约 | `hydrate_for_session()` **每轮注入 `[计划]`，但完全不看批没批** —— 写了文件就等于"已确定的执行步骤" | ⚠️ 半成品 |
| 阶段模型 | `get_phase()` 只按文件存在性取最高阶，**没有 approved / rejected 维度** → 结构上表达不了"计划已批准" | ❌ 缺 |

**两个可复现的具体风险**

1. `plan` 阶段 agent 可以直接 `shell_exec` 跑 `systemctl restart nginx` —— 没有任何拦截点会因为"当前是 plan 阶段"而拒绝它。
2. 计划**从未被批准**，但 `hydrate_for_session()` 已把 `[计划]` 渲染成"已确定的执行步骤（按此推进）"。**未经批准的意图被伪装成已确定的契约** —— 这比没有这个功能更危险（它让人以为已经批过了）。

### 2.3 顺带发现的一个真 bug

`workflow.get_phases()` 的 docstring 说"列出所有有工件的会话**阶段元数据**（供前端导航条展示）"，实现里 `out.append(child.name)` 返回的却是 **`base_dir` 下的子目录名，即会话 id 列表**。名字、文档与语义三者不符；调用方若按"阶段"消费必然错。（✅ 已收口：docstring 先随 goal-plan spec §5.6 对齐，随后第六轮清理把无人消费的整条 `phases` 链删除，见 §7.2。）

---

## 3. 落点设计（缺的那一层）

### 3.1 先说一个会让实现必错的陷阱：`risk_level` **不是**只读/可写分类

`ToolMeta.risk_level` 是**危害**分类（`safe` / `moderate` / `dangerous`），不是**可变性**分类。反例（均实测于本仓）：

| 工具 | risk_level | 真实性质 |
|---|---|---|
| `todo_write` | safe | **写状态**（任务清单） |
| `spawn_subagent` | safe | **可间接执行任何东西** → 子 Agent 就是只读约束的逃逸口 |
| `shell_exec` | moderate | 读/写**取决于命令本身**：`df -h` 是读，`systemctl restart` 是写 |
| `file_write` / `file_edit` | moderate | 可写 |

所以「plan 模式 = 只放行 `risk_level == 'safe'`」是**错的**：它既放过了 `spawn_subagent`（逃逸），又会挡住 `cat /var/log/x`（读，但 `shell_exec` 是 moderate）。

**`safety._SAFE_VERBS` 也不能直接复用** —— 那是「免审批」口径，不是「保证无副作用」口径。它包含 `tee`（写文件）、`curl` / `wget`（下载落盘）、`find`（`-delete` / `-exec`）、`awk`（`system()`）、`journalctl`（`--vacuum-files` 删日志）、`dmesg -C`（清缓冲区）、`hostname <name>`（改名）。

> **plan 模式的承诺（"什么都没改"）比审批门（"用户同意了"）更强，所以它的白名单必须是审批白名单的严格子集，且失败方向是拒绝。**

### 3.2 三个落点（都不需要改 `workflow.py`）

**① `PlanModeGuard` @ `PRE_TOOL_CALL`，`priority = 6`，`safety_critical = True`**

- priority 选 6 是为了**早于所有会弹审批卡的拦截器**（`ClarificationInterceptor` 7 / `PermissionGate` 8 / `FilesystemGuard` 10）；也刻意避开 `LoopDetector` 的 5，免得同值时顺序退化成依赖 `interceptors` 列表的注册顺序。`Reins.check()` 按 priority 升序、**首个 `intercepted=True` 即返回**（`reins/core.py:215-226`），所以 plan 模式下的写操作应当**直接拒绝**，而不是先弹一张审批卡给用户点。
- `safety_critical = True` 复用既有的**异常 fail-closed** 语义（`reins/core.py:218/237`）。
- 判定函数 `classify_plan_action(tool, args, registry) -> (allowed: bool, reason: str)`，**未知一律拒**（fail-closed）。
- **纠偏指令必须写进 `intercept_reason`**。这一点是读 `driver.py:452-482` 得到的、不是猜的：Driver 在 `PRE_TOOL_CALL` 的拦截分支只做 `result_text = f"工具调用被拦截: {intercept_reason}"` 然后回灌给模型，**该分支不消费 `inject_messages`**（`inject_messages` 只在 `PRE_ITERATION` / `POST_ITERATION` / `POST_TOOL_CALL` 被消费）。所以 `intercept_reason` 必须是一句完整的"为什么不行 + 现在该做什么"。
- 只读判定两级：
  - **工具级** `PLAN_READ_ONLY_TOOLS`：`file_read` / `file_glob` / `file_grep` / `read_image` / `log_read` / `sys_overview` / `process_inspect` / `network_probe` / `service_manage`(仅查询 action) / `web_search` / `web_fetch` / `load_skill` / `todo_write`（记录计划用，与 Claude Code 允许 TodoWrite 一致）；
  - **命令级**：`shell_exec` / `job_run` 的自由文本命令，需**同时**满足：`classify_command(cmd)[0] == 'safe'` **且**每段动词 ∈ `PLAN_READ_ONLY_VERBS`（严格子集）**且**无输出重定向（`>` / `>>`，`/dev/null` 除外）**且**无命令替换（`$(...)` / 反引号）**且**无 `sudo` / `xargs`。

**② `PlanDisciplineRein` @ `PRE_ITERATION`，`priority = 12`** —— 每轮注入纪律块

选这个注入点是因为 `driver.py:199-202`：每轮 `messages = harness.bridle.assemble(ctx)` 之后才 `messages.extend(pre_decision.inject_messages)`。**`messages` 是局部变量、不写回 `ctx`**，所以每轮注入**不会累积上下文** —— 这正是"目标每轮注入"用的同一机制，直接复用即可。

**③ 批准状态：sidecar 文件，不碰 `workflow.py` 的读写函数**

> ❌ **本小节已被推翻**：goal-plan spec（`docs/superpowers/specs/2026-09-22-goal-plan-mechanics-design.md` §2.4）裁决不做 sidecar，批准状态唯一存储是 `PermissionGate`（+`raw_key`），见 §6.2 同款标注。以下正文保留为设计过程记录。

在会话工件目录下加 `plan.meta.json`：

```json
{ "status": "draft|approved|rejected",
  "approved_at": 0, "approved_by": "user",
  "plan_hash": "sha256 of plan.md at approval time" }
```

`plan_hash` 是**批准绑定到具体版本**的机制：批准后若 `plan.md` 被改动，hash 不匹配 → 批准失效、退回 `draft`。这就是 spec-kit "plan must be correct before proceeding" 的机制化，而不是靠人记。

### 3.3 状态机

```
''  --spec_write-->  spec  --(用户确认)-->  plan(draft)
plan(draft)   --用户批准-->  plan(approved)  --动手-->  executing
plan(draft)   --用户否决-->  plan(rejected)  --修改-->  plan(draft)
plan(approved)--plan.md 被改（hash 变）-->  plan(draft)      # 批准作废
```

- **只读强制生效区间**：`spec` / `plan(draft)` / `plan(rejected)`。
- **解除区间**：`plan(approved)` / `executing` —— 注意解除的是"plan 的额外限制"，**既有审批门照旧运行**，与 Claude Code "plan mode is about deferring edits, not loosening safety" 一致。
- **I4 的落地**：`hydrate_for_session()` 目前的注入不分批准状态，需要按 §2.2 修 —— 要么只注入 `approved`，要么在 `draft` 时明确写"**待批准**，不得据此执行"。**前者更安全**。

---

## 4. 验证方案（每条都要能变红）

| 层 | 用例 | 变红条件 |
|---|---|---|
| 纯函数 | `classify_plan_action` 表驱动，**必含反例**：`tee` / `curl -o f` / `find -delete` / `systemctl restart` / `spawn_subagent` / `todo_write` 之外的写工具 / **未注册工具** | 白名单被改成"放行一切" → 反例必须红 |
| 拦截器 | 构造 `RunContext` 直调 `check()`，断言 `intercepted is True` 且 `intercept_reason` 含纠偏指令 | 去掉 `intercepted=True` → 红 |
| **回归（最重要）** | `permission_mode != 'plan'` 时，`check()` 必须返回**全默认 `ReinDecision()`**（逐字段零差异） | 任何字段被填 → 红。这条保证"没开计划模式时行为完全不变" |
| 批准门 | 批准后改 `plan.md` → 状态回落 `draft` | 去掉 hash 校验 → 红 |
| 注入 | `draft` 状态**不得**出现"按此推进"字样 | 把状态判断去掉 → 红 |

---

## 5. 已实现：只读强制层（`backend/agent/harness/reins/plan_mode.py`）

新文件，**已接线**（见 §5.3）。对既有运行仍**零影响** —— 未开计划模式（默认 `normal`）时两个拦截器都返回全默认 `ReinDecision`，且有 4 mode × 4 tool 的参数化回归钉住这一点。

| 导出 | 作用 |
|---|---|
| `MODE_KEY` / `MODE_PLAN` | 通过 `ctx.metadata["permission_mode"] == "plan"` 启用，不动 `RunContext` 契约 |
| `PLAN_READ_ONLY_TOOLS` | 工具级白名单（14 项），**不含** `spawn_subagent`（safe 但可间接执行任意命令）、`service_manage`（按 action 分流）、`memory_add` / `block_update`（写长期记忆） |
| `PLAN_READ_ONLY_VERBS` | 命令级白名单（约 60 项），**严格真子集**于 `safety._SAFE_VERBS` |
| `classify_plan_action(tool, args, registry)` | 工具级判定入口，**fail-closed**（未知一律拒） |
| `classify_plan_command(cmd)` | 命令级判定：命令替换 / 输出重定向 / `safety.classify_command` 必须 safe / 每段动词必须在白名单 |
| `PlanModeGuard` | `PRE_TOOL_CALL`，priority 6，`safety_critical = True` |
| `PlanDisciplineRein` | `PRE_ITERATION`，priority 12，每轮注入 `PLAN_DISCIPLINE` |

### 5.1 实现里几个必须留意的点

- **纠偏文案写在 `intercept_reason` 里**，不是 `inject_messages`：`driver.py` 的 `PRE_TOOL_CALL` 拦截分支只把它拼进 `工具调用被拦截: {reason}` 回灌给模型，**不消费 `inject_messages`**（该字段只在 `PRE_ITERATION` / `POST_ITERATION` / `POST_TOOL_CALL` 被消费）。
- **判定函数抛异常按拒绝处理**，不按放行。放行方向出错 = 只读约束静默失效，比误拒危险。
- **`service_manage` 整只拒掉**，不做 action 分流：避免把工具内部知识搬进策略层。拒绝文案直接告诉模型改成 `shell_exec` + `systemctl status <name>`（后者走命令级白名单能过）。代价是多一次往返，换的是策略层不依赖工具实现。
- **`todo_write` 是唯一放行的「写」工具**：它写的是**计划自身的表示**（对应 Claude Code 在 plan 模式下允许 TodoWrite），不是目标机状态；而 `memory_add` / `block_update` 写的是**长期记忆**，拒。这条区分是刻意的。

### 5.2 验证

```
cd backend && <venv>/Scripts/python.exe -m pytest tests/test_plan_mode.py -q -p no:cacheprovider
# → 127 passed
```

- 反例覆盖：`tee` / `>` `>>` 重定向 / `rm -rf` / `systemctl restart` / `find -delete` / `find -exec` / `apt update` / `curl -o` / `wget -O` / `sudo` / `sh -c` / `python3 -c` / `xargs` / `env X=y` / 命令替换 / `journalctl --vacuum-files` / `dmesg -C` / `date -s` / `hostname <name>` / `ifconfig eth0 down` / `sort -o` / `awk` / `cat /etc/shadow` / `top` / 空命令。
- **失败注入已验证**（`.workbuddy/tmp/mutation_allow_all.py`，把判定改成「一律放行」）：**60 failed / 60 passed** —— 60 条反例全部会红，不是空转断言；剩下 60 条是正例与回归，本就不该受注入影响。
- **未启用时零差异回归**：`permission_mode` 为 `normal` / 缺键 / `execute` / 空串时，两个拦截器必须 `== ReinDecision()`（逐字段），任一字段被填即红。
- **顺序用行为断言**：把 `PlanModeGuard` 与一个 `priority = 8` 的记录桩同注册，断言计划模式下**桩一次都没被调用**（即拒绝短路了后面的审批路径），非计划模式下桩**被调用**。不依赖别处类的 priority 常量。
- **表漂移断言**：`PLAN_READ_ONLY_VERBS < safety._SAFE_VERBS`（严格真子集）+ 逐项断言 `tee` / `curl` / `find` / `journalctl` / `sudo` … 不在 plan 白名单里。两张表各自漂移时这条先红。
- **第 7 组：生产装配**（`create_default_reins`）—— 上面 1~6 组手搓 reins，只能证明「判定与拦截器自身正确」；这一组证明它们**真的被装了进生产装配**、priority 顺序对、且端到端拦得住。断言落在「是不是 `plan_mode_block` 事件」而不是「整条链有没有拦」—— 后者在 `saddle=None` 时会因 `PermissionGate` fail-closed 而假通过。
- **第 8 组：HTTP 入口到 run 的通路** —— `AgentRunRequest.permission_mode`（字段）与 `harness.run(permission_mode=...)`（传参）是**两个独立写点**，只加一处会让「界面选了计划模式」静默失效且测试全绿。用 **AST** 把这一对钉在一起，并**运行时真造一次 `AgentRunRequest`** 验证 `Literal` 值域确实会 422（AST 只能证明写了注解）。

### 5.3 接线（4 处，已全部落地）

| # | 位置 | 改动 |
|---|---|---|
| 1 | `reins/defaults.py` | `interceptors.append(PlanModeGuard(saddle=saddle))` + `append(PlanDisciplineRein())`（+19 行） |
| 2 | `harness.py` `run()` | 新增可选参数 `permission_mode: str = "normal"`，写入 `ctx.metadata[MODE_KEY]`（+19 行，**唯一写入点**） |
| 3 | `routes/agent.py` `AgentRunRequest` | `permission_mode: Literal["normal","auto","plan"] = "normal"`（值域校验沿 `mode` / `environment` 惯例；`auto` = 审批轴「模型代判」，与本节的 `plan` 互斥） |
| 4 | `routes/agent.py` 的 `harness.run(...)` 调用 | `permission_mode=req.permission_mode` |

1~4 全是**新增**，没有一行删除；1+2 合计 +38 行。

**失败注入已验证两轮**（脚本在 `.workbuddy/tmp/`）：
- `mutate_plan_registration.py`：① 去掉 defaults.py 的两行注册 → **3 failed**（正是 3 条装配测试）；② 把 `harness.run` 的默认值改成 `"plan"` → **1 failed**；还原后 124 passed。
- `mutate_plan_wiring.py`：③ 只留字段不传参 → 红；④ `Literal` 退化成 `str` → 红；⑤ `run` 参数改名而 routes 侧没跟着改 → 红。还原后绿。

注入脚本的还原用**反向替换**而非整体写回备份 —— 否则会把并发写者在窗口期内对同文件其他位置的改动一起覆盖掉。

---

## 6. 接线完成情况与仍未接的线

**后端 4 个接线点已全部落地**（见 §5.3）—— `permission_mode` 现在能从 HTTP 请求一路走到 `ctx.metadata`：`POST /api/agent/run` 带 `"permission_mode": "plan"` 即可验证只读强制生效。

**仍差一件事：§6.2 的批准门。**

### 6.1 前端「计划」chip —— 已接线（本节原为「假启用」，已失效并更正）

chip 的值现在确实进入 run 请求，链路三跳都在代码里：

| # | 位置 | 作用 |
|---|---|---|
| 1 | `src/components/chat/SideChatPanel.vue` → `emit('send', content, skillIds, attachments, mode, approval)` | chip 选中的姿态随发送载荷出组件 |
| 2 | `src/components/ssh/SshAiPanel.vue`（`handleSend` → `store.sendMessage(content, { …, mode, approval })`） | 透传给 store，不在此处解释语义 |
| 3 | `src/utils/run-modes.ts` `resolveRunModes(agentMode, approval)` | 单一口径：一次返回 `{ workflowMode, permissionMode }` 成对字段，调用方无法只取其一（`ssh-ai.ts` 里展开进 options） |
| 4 | `src/api/client.ts`（请求体 `workflow_mode` / `permission_mode` 两行） | 落成后端两个字段 |

**为什么收敛成一个函数**：`workflow_mode` 只管措辞（注入计划姿态的 system prompt 段），`permission_mode` 才是真拦住写操作的那个。只发前者 = 界面亮着、约束不存在的「亮的空开关」，而拆成两处各写一遍条件正是这种漂移最常发生的地方。

姿态为 `plan` 时 `permissionMode` 恒为 `'plan'`、审批档休眠 —— 与 §5.1 的「PlanModeGuard 在任何审批卡之前拦住写操作」对齐：计划模式下不该再弹「让模型代判」的卡。

`AgentRunPanel.vue` 的「计划已生成 / 批准 / 驳回」卡片仍**未做** —— 它依赖 §6.2 的状态，而那个状态还不存在。

### 6.2 批准门（I3）的存储未设计 —— 也是 §2.3 那个真 bug 的收口处

`plan_write` 写出 `plan.md` 之后，**没有任何动作能把模式切回可写**。更糟的是：当前 `workflow.hydrate_for_session` 只要看到 `plan.md` 存在，就把它当成「已确定的执行步骤（按此推进）」注入 —— 也就是**未批准的计划已经被当成契约在执行**。

§3.2 给的 `plan.meta.json` sidecar 方案（`status: draft|approved|rejected` + `plan_hash` 绑版本，hash 变则批准作废）需要读写 `workflow.py` 的会话工件目录。**先对齐归属再写**，否则会出现两套「批准状态」—— 这正是本仓库反复出现的失败模式（同一份「布尔 app_setting 解析」曾在 `database.py` 与 `post_processor.py` 各写一遍且语义不同，第五轮清理才收敛成 `database.bool_app_setting`）。

> ❌ **归属已裁决（goal-plan spec §2.4）**：批准状态唯一存储 = `PermissionGate`（`raw_key` 绑计划版本），sidecar 方案作废；批准门实现仍开放（README「尚未接线」口径不变）。

### 6.3 当时的现场记录（已不适用，留档）

写作时（17:03–17:07）工作区**有另一条线在并发改**同一片区域：

```
backend/agent/workflow.py                      17:0x
backend/agent/tools/builtin/workflow_tools.py  17:0x
backend/agent/tools/builtin/__init__.py        17:0x
backend/routes/agent.py                        17:0x
backend/agent/harness/driver.py                17:0x
backend/agent/harness/telemetry.py             17:0x
backend/routes/settings.py                     17:0x
```

（`main.py` 的 mtime 在两次观察之间从「含 `routes.goals` 导入」变成「已移除导入」——**观察期间文件正在被写**。）

往这些文件写会丢写，而且**在已有实现之上再起一套并行实现**正是本项目已经出现过的坏味道（同一份布尔 app_setting 解析曾在 `database.py` 与 `post_processor.py` 各写一遍，语义还不完全一致）。因此只落地了不冲突的那一层。

**当时的 3 个接线点（后端部分现已全部完成）**：

1. ✅ `backend/agent/harness/reins/defaults.py` —— 注册两个拦截器。
2. ✅ `backend/agent/harness/harness.py` + `backend/routes/agent.py` —— `run()` 新增可选参数并写入 `ctx.metadata`（**唯一写入点**）；`AgentRunRequest` 加字段并传参。`RunContext` 已有 `metadata: dict`，未改 `contracts.py`。
3. ⬜ 前端 —— 见 §6.1。

**仍未设计的部分（I3 批准门的存储）**：见 §6.2。

---

## 7. 本轮顺带发现的两个在途缺陷（不是我改出来的）

### 7.1 `database.py`：goal 回滚留下孤儿迁移，新建库直接崩 —— 已消除

`_migrate_db()` 里曾留着补齐 goal 五要素的迁移块，但全仓已经**没有任何地方创建 `goals` 表**了：

```
$ grep -rn "CREATE TABLE.*goals" backend --include=*.py
（无）
```

新建库时 `PRAGMA table_info(goals)` 返回 0 行 → 逐列 `ALTER TABLE goals ADD COLUMN …` → `sqlite3.OperationalError: no such table: goals`。

**当前状态：该迁移块已不在代码里**（`grep -n "goals" backend/database.py` 无命中），
`tests/test_round8_fixes.py` 13 passed。本节保留为缺陷记录，不再是需要裁决的开放项。

### 7.2 `workflow.get_phases()`：名字/文档与返回值不符 —— 已随第六轮清理整链删除

见 §2.3 —— docstring 曾写"阶段元数据"而实现返回会话 id 列表。goal-plan spec §5.6 已把
docstring 与实现对齐；而 `workflowApi.phases` 始终没有任何调用方，于是第六轮清理把
整条孤儿链（`src/api/client.ts` 的 `phases` 包装 + `routes/workflow.py` 的
`GET /workflow/phases` + `agent/workflow.py` 的 `get_phases()`）一并删除，缺陷就此收口。

## 8. 两处 git 记账错位（本轮真实发生，均未修）

### 8.1 `789715b`：标题写的是 docs(memory)，里面还有并发会话的 13 个文件

提交 spec §10 时，门禁检查（`git diff --cached --name-only` 恰为一个路径）与 `git commit`
之间有几秒间隙，并发会话把在途改动写进了同一个共享 index，于是这批文件被扫进我的提交：
`reins/{__init__,auto_approve,core,gate,guards}.py`、`database.py`（审批策略两新字段）、
`main.py`、`routes/settings.py`、`tests/{test_auto_approve,test_permission_approval_lifecycle,test_permission_policy_round18}.py`、
`settings/fields/ToggleSwitch.vue`、`settings/panels/AgentPanel.vue`。

内容没坏也没丢（全量 1417 passed / 0 failed），错的是归属 —— 查 reins/审批策略那条线的人
`git log` 会撞到一条标题只讲记忆文档的提交。**没拆的原因**：并发会话每隔几分钟就往这条链上
叠提交（写本节时 tip 已是 `b54b6c6`），拆 `789715b` 就是重写它正在写的链。两条线都空闲时的
正解是 `git commit-tree` 逐条重建（前四条树原样不动，只把最后一条的树换成「父树 + 我的 spec blob」），
不是 `git reset`（会把它的后续提交一起退回工作区）。

### 8.2 `339b59f`：message 末行乱码 token，且已在远端

正文末行是 `…面板会塌成一intas`（应为"塌成一坨"，句子在那里断了）。`gitee/master` 已包含这条
（`git merge-base --is-ancestor 339b59f gitee/master` → YES），修它等于重写已发布历史 +
`git push --force-with-lease gitee master`，需要两条线都停手。只影响一条 message 的可读性，
不影响代码与测试。


