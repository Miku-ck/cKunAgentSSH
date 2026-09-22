# 目标续跑与计划批准门 —— 设计规格

> 写作时间：2026-09-22
> 前置阅读：`docs/plan-mode.md`（本文承接其不变量 I3「批准是一个显式的门」，并把它从未接线的 §6.2 收口）
> 归属：本文件所涉 `workflow.py` / `driver.py` / `routes/agent.py` 区域的并发写者问题已不存在——
> 经核实，`docs/plan-mode.md` §6.1（前端 plan chip）与 §7.1（goals 孤儿迁移）**均已被接上/删除**，
> 只剩 §6.2 批准门未落地，即本 spec 的主题。

---

## 0. 结论先行

Qoder 对话框的「目标」与「计划」两个能力，本仓库已各自实现约八成，缺的是三处**只有自觉、没有机制**的地方：

| 缺口 | 现状 | 本 spec 的收口 |
|---|---|---|
| **I3 批准门** | `plan_write` 的 description 写"应等待用户批准计划后再动手执行"，无任何机制承载；且未批准的 `plan.md` 已被注入成"已确定的执行步骤" | 新增 `PlanApprovalGuard` @ `PRE_TOOL_CALL`，批准状态收敛为 `PermissionGate` 单一权威源 |
| **goal 不跨 run** | `done` 事件已带出 `workflow_goal.{goal_text,self_checked,acknowledged}`，**信号已存在但无人消费** | `event_stream` 同流接续下一轮 run，带 `max_goal_turns` 与链级成本熔断 |
| **无 blocked 判定** | agent 自述"未达成"仅落为关键词粗判，无客观校验、无停手条件 | 双源一致判定（自报 ∧ 失败签名集合重复），命中转既有 `HUMAN_HELP` 路径 |

三块可独立交付，**建议实施顺序 A → C → B**：A 是纯拦截器新文件、风险最低；C 的 blocked 判定是 B 的续跑循环的止损阀，先于 B 存在才安全。

---

## 1. 现状核对（2026-09-22 实测，非沿用文档）

### 1.1 已存在且可直接复用

| 能力 | 位置 | 复用方式 |
|---|---|---|
| 三工件落盘 + 阶段推导 | `backend/agent/workflow.py:33,84` | `get_phase(conversation_id)` 判定"是否处于 plan 阶段" |
| 只读/可写二分 | `reins/plan_mode.py:classify_plan_action` | 批准门的"是否写操作"判定**直接调用它**，不新建第三张分类表 |
| 只读硬拦截 | `reins/plan_mode.py:PlanModeGuard`，`PRE_TOOL_CALL` priority 6 | 复用其只读/可写判定；两门条件独立（分工见 §2.2） |
| 审批卡全链路 | `permission_needed` 事件 → `stores/agent.ts` → `AgentRunPanel.vue` → `POST /agent/permission/grant` | 批准动作复用同一张卡，**不新增事件类型、不新增授权端点** |
| 会话级授权池 | `PermissionGate._confirmed_session[conversation_id]`，跨 run 共享 | 批准键落此处，天然满足"批准后跨 run 有效" |
| goal 自检收尾锚 | `driver.py:296-319` + `workflow.goal_self_check_prompt` | 续跑循环的判定输入 |
| 目标状态透出 | `driver.py:853-857` `done.workflow_goal` | 扩展两个字段即可（§3.2） |
| 链级摘要 | `agent_runs.context_summary`（spec §8 已落库） | 续跑轮的上下文来源（§3.4） |
| 人工介入终止路径 | `driver.py:379-382` `State.HUMAN_HELP` + `human_help_needed` | blocked 命中后的落点，复用既有求助卡片 |

### 1.2 `docs/plan-mode.md` 记录的三条"未接的线"，实测现状

- **§6.1 前端 plan chip 假启用** → **已不存在**。`SideChatPanel.vue:676-691` 有完整的两轴注释与 `isPlanMode`，`ssh-ai.ts:562` 经 `resolveRunModes(activeMode, approval)` 实际下发 `permission_mode`。
- **§7.1 goals 孤儿迁移崩新建库** → **已修复**。`database.py` 中该迁移块已删除，全仓无 `goals` 表相关代码。
- **§6.2 批准门存储未设计** → **仍未落地，本 spec §2 即其设计**。

### 1.3 顺带修正的一处文档漂移（纳入本 spec 范围）

`workflow.get_phases()` 的 docstring 仍写"列出所有有工件的**会话阶段元数据**"，实现返回的是 `base_dir` 下的子目录名（即会话 id 列表）。路由侧 `routes/workflow.py:51` 已经用 `{"conversation_ids": ...}` 这个诚实的键名消费它，所以**语义不阻塞、命名在撒谎**。本 spec 顺带把 docstring 与实现对齐。

---

## 2. 设计 A：plan 批准硬门（不变量 I3）

### 2.1 落点

新文件 `backend/agent/harness/reins/plan_approval.py`：

- `PlanApprovalGuard` — `point = ReinPoint.PRE_TOOL_CALL`，`priority = 4`，`safety_critical = True`
- 注册进 `reins/defaults.py` 的 `interceptors` 列表

### 2.2 priority 取 4 的理由（**两次修正：先否决"顺移既有编号"，再否决"取 9"**）

`PRE_TOOL_CALL` 实测编号（已逐个 grep 确认）：`LoopDetector` 5 → `PlanModeGuard` 6 → `ClarificationInterceptor` 7 → `PermissionGate` 8 → `FilesystemGuard` 10。

对话中我先提出"本门取 7、Clarification/PermissionGate 顺移"。**否决**：改动既有拦截器编号会牵连 `test_plan_mode.py` 第 7 组的生产装配断言，而且既有编号本身不是按语义排的（7 与 8 的相对顺序是历史成因），动它们等于把一处新需求摊成四处回归风险。

初稿随后写"取 9，晚于 PermissionGate(8)"。**这个数是错的，自检时抓到**：约束的本意是**在任何会弹审批卡的拦截器之前短路**，而 `ClarificationInterceptor`(7) 与 `PermissionGate`(8) 都会发 `permission_needed` / 澄清事件。取 9 意味着未批准的计划下，`service_manage stop` 这类工具级 dangerous 会先弹出一张"允许这个工具吗"的卡片——正好是本门要防止的那件事。取 9 只在"命令级卡片由 FilesystemGuard(10) 发、所以只要早于 10"这个**不完整的前提**下才成立，漏了工具级那条路径。

取 **4**：本门跑在 `PRE_TOOL_CALL` 链上**最前**，零侵入既有编号。

- **早于 5/6/7/8/10 全部**：任何审批卡、任何澄清提问都在"这份计划批过没有"得到回答之后才可能发生。
- **早于 `PlanModeGuard`(6)**：两者判定的都是"不该执行的写操作"，谁先拒不影响结果（都是拒绝），且本门在无 plan.md 时是无操作短路，所以热路径上多数 run 只会多一次 `dict` 查。
- **早于 `LoopDetector`(5)**：未批准的计划下写操作被拒**不计入循环次数**——这是对的，那是一次策略拒绝，不是模型在原地打转。若排在 5 之后，同一被拒工具反复出现会被误判为循环，把"去批准计划"这个正常交互路径升级成 run 终止。
- 与 `PlanModeGuard` 的分工：6 管"这次 run 是不是只读档位"，本门管"这份计划批过没有"。两门条件独立、互不覆盖。

顺序用**行为断言**钉住而非引用别处的常量：把本门与一个 `priority = 10` 的记录桩同注册，断言 plan 阶段未批准时桩不被调用、已批准时被调用。这正是 `test_plan_mode.py` 第 7 组已经验证过的做法。

### 2.3 生效条件（三条全满足，否则返回逐字段零差异的 `ReinDecision()`）

1. `workflow.get_phase(conversation_id) == "plan"` —— plan.md 存在
2. 本次工具调用按 `classify_plan_action` 判定为**非只读**
3. 该 plan 的批准键不在 `_confirmed_session[conversation_id]` 中

**已知的语义后果，明确采纳**：`get_phase()` 只看 plan.md 存在性，所以普通 run（用户在审批轴选了 normal、没点 plan 开关）里模型若自己调了 `plan_write`，本门会从此拦住该会话的写操作，直到用户批准卡片或删掉工件。这是把 I4"批准后的 plan 是执行的契约"贯彻到底——**写出来的计划就该被确认**。代价是可能让用户觉得"我没开计划模式怎么被拦了"，因此拒绝文案必须显式说明：当前会话存在 `plan.md`、批准它即开始执行、或不想要计划就说一声（配套提供 §2.6 的撤销入口）。

### 2.4 批准状态的存储：PermissionGate 单一权威源（**取代 `plan.meta.json` sidecar**）

`docs/plan-mode.md` §3.2 提议在工件目录写 `plan.meta.json`（`status: draft|approved|rejected` + `plan_hash`）。**本 spec 不采纳**，理由是它引入**第二份"批准状态"权威源**：

- 一旦有 sidecar，就必须回答"sidecar 说 approved、`_confirmed_session` 说没有，听谁的"——而**本项目反复出现的失败模式正是跨层键漂移**（`docs/plan-mode.md` §6.2 自己也是这么警告的）。
- 用户在权限管理面板撤销授权时，只有 `PermissionGate` 会生效，sidecar 里的 approved 会变成幽灵状态。
- sidecar 方案还要新增读写函数，等于要动 `workflow.py`（`docs/plan-mode.md` §6.2 承认"需要读写 workflow.py 的会话工件目录，先对齐归属再写"）。

采用 `PermissionGate`：

- **授权键**：`plan`（裸工具名形式，会话作用域由 `_confirmed_session` 的 dict 键提供，键值本身不需要再带 conversation_id）
- **内容哈希**：`sha256(plan.md 原文)[:16]`，随批准动作记录，用于 §2.5 的版本失效
- **改 `PermissionGate._grant_key` 一处**：新增可选参数 `raw_key: Optional[str] = None`，非空时直接返回它，绕过 `tool`/`command` 组装。**不可走 `command=<plan 正文>` 这条路**——`_grant_key` 对 command 施加的是 `safety.normalize_command_for_detection`（safety.py:117），它会剥 ANSI 序列、Unicode NFKC 归一化（全角→半角）、折叠 home 路径、剥反斜杠转义。那是为 shell 混淆攻击设计的**语义等价归一化**：两份仅全角标点不同的计划文档会被判为同一条命令。plan 正文要的是**逐字节同一性**，任何复用命令归一化的做法都会让"改了计划"检测不出来。

`raw_key` 是**唯一**的键构造入口，Guard 检查、grant 端点、卡片 payload 三处都从这里取，杜绝"自己 sha256 一遍"的第二份实现。

**取舍登记（明知不做）**：批准状态不跨进程重启——重启后 `_confirmed_session` 清空，已批准的计划需要重新批一次。这与既有 session 授权语义完全一致（`gate.py:95` 的注释即"H13 修复 A5：进程重启失效"），且方向是**多问一次而不是少问一次**。sidecar 方案唯一胜出的点是持久性，为它引入双权威源不划算。

### 2.5 版本失效

`plan_write` 工具写入 plan.md 时，顺带做两件事（改动在 `workflow_tools.py`，约 +5 行）：

1. 清 `ctx.metadata` 里的批准缓存
2. 若新内容哈希 ≠ 已批准记录的哈希 → 从 `_confirmed_session[conversation_id]` 移除该批准键

即"改计划 = 重新批准"。这是 `docs/plan-mode.md` §3.2 `plan_hash` 机制的原样继承，只是状态存在 gate 侧。

### 2.6 拒绝与撤销

- **拒绝**（卡片点"驳回"）：写 `_denied_commands[run_id]`，本 run 内不再弹卡，模型收到"计划已被驳回，请修改后重新提交或向用户澄清"。语义与命令拒绝同源。
- **批准后反悔**：既有权限管理面板按 §2.4 单源存储自动可见（批准键就在 `_confirmed_session` 里），`revoke` 现成可用，**不需要新接口**。
- **彻底不要计划**：`plan_write` 的 `action="clear"`（若无则本次补一个）删除 plan.md 工件，`get_phase` 立即回落，门自动不再生效。

### 2.7 I4 收口：未批准的注入必须改文案

`workflow.hydrate_for_session()`（workflow.py:189-192）现在无条件把 `[计划]` 渲染成"已确定的执行步骤（按此推进，配合任务列表进度）"。**未批准的计划被伪装成已确定的契约，比没有这个功能更危险**（`docs/plan-mode.md` §2.2 原话）。改为按批准状态二选一：

- 已批准 → 现文案不变
- 未批准 → `[计划（待批准）]` + "这是**尚未获用户批准**的候选步骤，**不得据此执行**；只可继续只读调研或向用户说明为何值得批准"

判定入参由调用方（`routes/agent.py:_load_active_goal_text`）传入，`workflow.py` **不反向依赖** `PermissionGate`（保持它是纯文件模块、无 gate 依赖）。

### 2.8 前端

`AgentRunPanel.vue` 审批卡按 payload 里的 `kind: "plan_approval"` 分支渲染：标题「批准执行计划」、正文预览 plan.md 前 ~40 行、按钮三枚（批准计划 / 驳回 / 查看完整计划）。**事件类型仍是 `permission_needed`**，不新增 SSE 协议分支——新增事件类型要求前后端两张表同步，是本仓库的老漂移源。

---

## 3. 设计 B：goal 跨 run 自动续跑

### 3.1 改动层次：编排放在路由层，不放在 driver

`routes/agent.py` 的 `event_stream()`。理由：续跑要复用的东西（模型配置解析、记忆检索、画像块探测、`run_id` 分配、`agent_runs` 建行）**全部已经在路由层备好了**，放在 driver 里意味着要么把这些能力下沉进 harness（大范围重构），要么在 driver 里反向调用路由（分层倒置）。driver 的职责止于"把结论说清楚"。

### 3.2 `done` 事件契约扩展

`workflow_goal` 增加两个字段：

```
verdict: "achieved" | "not_achieved" | "unchecked" | "none"
turn:    int   # 本 run 是这条 goal 链的第几轮，无 goal 时为 0
```

**`verdict` 的主判据换成结构化标记，关键词粗判降级为兜底。** 现在 `_goal_acknowledged`（driver.py:872-897）靠"全部达成"/"未达成"等中文子串猜结论——这套措辞表是**跨层漂移的温床**：driver 解析它、前端展示它、续跑循环还要据它决定是否再起一轮，而它同时是最容易因模型换个说法就静默失效的判据。这正是本仓库「缺陷家族：跨层键漂移」的形状。

改法：`goal_self_check_prompt`（workflow.py:211）要求 agent 自检轮必须以固定标记收尾（`[目标验收] 已达成` 或 `[目标验收] 未达成：<未满足条目>`），driver 解析标记；标记缺失才回落到 `_goal_acknowledged`，并在 `verdict` 里如实标 `"unchecked"` 而非猜测。**关键词表保留但只作兜底，且不再被前端当作结论展示**。

### 3.3 续跑循环

`event_stream` 收到 `done` 后：

```
verdict == "not_achieved"
  ∧ turn < max_goal_turns
  ∧ 链级成本未熔断
  ∧ 未收到停止请求
  ∧ SSE 客户端仍连接
→ 分配新 run_id、落新的 agent_runs 行、注入 §3.4 的上下文、再次 harness.run
→ 否则关闭流，行为与今日一致
```

每轮照发 `run_start` / `done`，前端按 run_id 分段。`agent_runs` 本来就是一 run 一行，**轨迹回放与审计天然分轮，不需要新表、不需要新列**。

### 3.4 续跑轮的上下文：轻量重建

**不**携带上一轮全量 `ctx.messages`。运维 run 动辄几十轮工具输出，全量携带会让第二轮直接压满窗口——而且 goal 链的语义是"带着结论继续"，不是"重放过程"。注入三件：

1. `goal.md` 原文（已有注入路径，`routes/agent.py:189`）
2. 上一轮 `agent_runs.context_summary`（spec §8 已落库，正是为此准备的）
3. `verdict` 里解析出的**未满足标准列表**——让模型每轮从"还差哪几条"出发，而不是重新推断任务目标

全部走已有的 goal 注入通道，**不新增注入通道**。

### 3.5 停止键

`POST /agent/goal/stop {conversation_id}` → 写进程级标记（一个 `set[str]`），续跑循环每轮开始前查一次。粒度是"轮之间"而非"轮之内"：轮内终止已有 `CostBudgetRein` 和 `force_terminated` 两条路，不该再造第三条。

前端在流式期间显示「目标续跑中（第 2/3 轮）· 停止」。进程重启丢停止标记是可接受的——重启同时终结了续跑循环本身。

---

## 4. 设计 C：goal 回合预算与 blocked 判定

### 4.1 双源一致，两个信号方向相反

- **agent 自报**（必要不充分）：自检轮显式声明受阻及原因。单独采信会被虚报骗过去——模型有把困难陈述成完成的倾向。
- **后端客观校验**（充分不必要）：本轮 `tool_calls_log` 的**失败签名集合**与上一轮**完全相同** → 判定无进展。签名复用 `reins/core.tool_call_signature`（已做路径 normpath、shlex 重组等规范化），**不新造第二套规范化逻辑**。

```
blocked = (自报受阻 ∧ 签名集合与上轮相同)
          ∨ (连续 N 轮签名集合相同，N 默认 2，不论是否自报)
```

第二条覆盖"模型不肯承认、原地反复重试同一批失败命令"——这正是运维场景里最费算力也最危险的形态（反复 `systemctl restart` 试图救活一个坏掉的服务）。

### 4.2 命中后：转既有路径，不新增终止态

`blocked` → `State.HUMAN_HELP` + `human_help_needed` 事件，复用 `driver.py:379-382` 的终止分支与 `AgentRunPanel.vue` 的求助卡片。**不新增 State 枚举成员**：`contracts.State` 加一个 `BLOCKED` 会让所有 `state in (FAILED, HUMAN_HELP)` 的判断点变成需要审计的清单，而语义上"blocked 就是要人"与 HUMAN_HELP 完全重合。

### 4.3 预算：`max_goal_turns` 默认 3

与 `max_cost_usd` / `max_total_tokens` 同层，但**作用域是整条 goal 链而非单个 run**。现有 `CostBudgetRein`（flow.py:414，POST_ITERATION priority 1）是 per-run 的，链级累计必须在 `event_stream` 的续跑循环里做——链本身跨多个 `ctx`，没有哪个 run 内组件能看到全局。

`AgentRunRequest` 新增 `max_goal_turns: int = Field(3, ge=1, le=10)`：值域用 `Field` 夹紧而非事后校验，与 `routes/settings.py` 既有 `le=` 惯例一致；上限 10 是为了让一次失控的续跑链不能吃掉整晚算力。

---

## 5. 验证方案（每条都要能变红）

沿用 `docs/plan-mode.md` §4/§5.2 已验证有效的模式：**反例必红、失败注入验证、零影响回归、行为断言顺序、AST 钉住字段-传参成对写点**。

### A. `test_plan_approval_gate.py`

| 组 | 断言 | 变红条件 |
|---|---|---|
| 门控 | plan.md 存在且未批准 → 写工具 `intercepted=True`；只读工具放行 | 去掉批准状态检查 |
| 单源 | 批准后跨 run 免拦（同 conversation_id 新 ctx） | 批准键写进了 run 作用域 |
| 失效 | 批准后改 plan.md 内容 → 回落需重批 | 去掉哈希比对 |
| **不放松安全** | 计划已批准时，prod 危险命令**仍**弹单次审批卡 | 批准状态被误用成免审批通行证 |
| 键正确性 | 两份仅全角标点不同的 plan.md → **不同**批准键（钉住"不得复用 `normalize_command_for_detection`"） | 键走了命令归一化 |
| 零影响 | 无 plan.md / 已批准 / 只读工具三种情况下 `check() == ReinDecision()` 逐字段 | 任一字段被填 |
| 顺序 | 与 priority 10 记录桩同注册，未批准时桩不被调用 | 门被排到 FilesystemGuard 之后 |
| 注入文案 | `draft` 态的 system prompt 注入含"不得据此执行"、不含"按此推进" | `hydrate_for_session` 不分批准状态 |
| 撤销 | 管理面板 `revoke` 后立刻回到未批准态（无 sidecar 幽灵） | 出现第二份状态源 |

### B. `test_goal_continuation.py`

| 组 | 断言 | 变红条件 |
|---|---|---|
| verdict 解析 | 结构化标记优先；标记缺失 → `"unchecked"` 而非关键词猜测 | 关键词表成为唯一判据 |
| 续跑 | `not_achieved` 且 `turn < max` → 起第二轮、落两条 `agent_runs` | 循环条件写反 |
| 上限 | `turn == max_goal_turns` → 不续跑并发 warning | 上限判断 off-by-one |
| 熔断 | 链级成本累计超阈 → 停，且**跨 ctx 累加**（单轮都不超、总和超） | 熔断仍 per-run |
| 停止 | `/agent/goal/stop` 后下一轮不起 | 标记查在了错误作用域 |
| 断连 | `request.is_disconnected()` → 不续跑（沿用 `routes/agent.py:852` 既有检测） | 续跑绕过断连检查 |
| 上下文 | 第二轮 system prompt 含上轮 `context_summary` 与未满足条目，不含全量 messages | 上下文携带方式退化成全量重放 |
| AST 成对 | `AgentRunRequest.max_goal_turns`（字段）与续跑循环读取点两个写点同时存在 | 只加字段不接线（`test_plan_mode.py` 第 8 组的同款陷阱） |

### C. `test_goal_blocked.py`

| 组 | 断言 | 变红条件 |
|---|---|---|
| 双源 | 仅自报 / 仅签名相同，各自单独**不**判 blocked（N 未达时） | 采信单一信号 |
| 签名 | 复用 `tool_call_signature`，同一命令换空白不算新进展 | 另造规范化 |
| 落点 | blocked → `State.HUMAN_HELP` + `human_help_needed`，且**不新增** State 成员 | 引入 BLOCKED 枚举 |
| 阈值 | 连续 N=2 轮相同才判，N=1 配置生效 | 阈值不可配 |

### 全量闸门

```
cd backend && <venv>/Scripts/python.exe -m pytest -q -p no:cacheprovider
npm run typecheck
npx vitest run
```

（按项目记忆 `project-verification-gates`：后端套件偶发卡死在 socketpair 属沙箱网络问题，重跑即可，不是回归。）

### 失败注入

每块至少两条注入脚本放 `.workbuddy/tmp/`，**还原用反向替换而非整体写回备份**——`docs/plan-mode.md` §5.3 记录过原因：整体写回会覆盖并发写者在窗口期内对同文件其他位置的改动。

- 注入 1：批准门永远返回放行 → A 组门控/单源/不放松安全三条应全红
- 注入 2：续跑循环条件 `turn < max` 改成 `turn <= max` → B 组上限用例应红（off-by-one 是这类循环最可能的真实缺陷）
- 注入 3：blocked 判定去掉签名一致性、只看自报 → C 组双源用例应红

---

## 6. 范围边界与风险登记

### 6.1 明确不做（登记为取舍，非遗漏）

| 项 | 不做的原因 |
|---|---|
| 批准状态持久化（sidecar / DB 列） | 引入第二权威源；重启后重批一次，方向是多问不是少问（§2.4） |
| plan.md 解析成结构化步骤、逐步批准 | 与 `workflow.py`"文件即内容、不解析结构"的既定边界冲突；长计划会形成长审批队列 |
| goal 链的独立 DB 表 | 续跑天然复用 `agent_runs` 一轮一行；链身份用 `conversation_id` + goal 工件即可表达 |
| 新增 SSE 事件类型（`plan_approval_needed` / `goal_continuing`） | 前后端两张表同步是既有漂移源，复用 `permission_needed` + payload 分支 |
| 轮内即时终止（续跑的停止粒度） | 已有 `CostBudgetRein` 与 `force_terminated` 两条路，不造第三条 |
| `_goal_acknowledged` 关键词表删除 | 保留为兜底判据；只降级其地位，不一次性移除正在被前端消费的字段 |

### 6.2 主要风险

1. **B 的成本敞口**（最需要盯的一条）：一次请求最多跑 `max_goal_turns` 轮，每轮最多 `max_iterations`。默认 3 × 100 = 300 次 LLM 调用。默认值保守 + 链级熔断 + 停止键三重兜底，但**上线前必须在真实运维任务上量一次链级成本分布**，3 这个默认数不能拍脑袋留着。
2. **A 的误拦敞口**：普通 run 里模型自己调 `plan_write` 就会触发拦截（§2.3）。拒绝文案必须解释清楚"为什么被拦、怎么继续"，否则用户看到的是一次无来由的失败。这是**误拦**方向，不是误放——按项目既定的对称性要求，两个方向都要有意识，但此处取的是更保守的那一侧。
3. **C 的签名相等误判**：两轮都失败在**同名同参数**的工具上才叫无进展。合法的"同命令重试终于成功"（网络抖动）会被算作进展（因为它成功了），所以不会误停；但"每次都在不同的第二个工具上失败"会**判不出** blocked（签名集合不同），即漏判方向。漏判的兜底是 `max_goal_turns`，可接受。
4. **A/C 的交叉影响**：`spawn_subagent` 在 `classify_plan_action` 下可间接执行任何东西（`docs/plan-mode.md` §3.1 的头号反例）。批准门复用同一判定，因此子 Agent 的写操作是否算"写"必须由既有判定给出答案，不能在本门里另开特例。若实测发现它被判为只读，那是 `classify_plan_action` 的缺陷，应在其自身修复并补反例用例，**不在本门打补丁**。

### 6.3 交付切分

三块各自独立可测、可分别提交：

1. **A**（新文件 `plan_approval.py` + `workflow_tools.py` 哈希失效 + `hydrate_for_session` 注入文案 + 前端卡片分支）——纯增量，未启用即零影响
2. **C**（blocked 判定 + `verdict` 结构化 + `max_goal_turns` 字段）——依赖 A 之后合入，因为 C 的自报文案要与 A 的门协同
3. **B**（`event_stream` 续跑循环 + 停止端点 + 前端续跑指示）——最后做，它需要 C 的止损阀先存在才安全

---

## 7. 实施顺序建议

`A → C → B`。A 独立、零影响、可先交付把 I3 补全；C 是 B 的止损阀，先于 B 存在才让 B 的成本敞口可接受。每块按 §5 各自的测试文件先写红用例再实现。
