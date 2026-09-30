# 退役 AI 侧栏：三态收两态

- 日期：2026-09-30
- 承接：`2026-09-29-home-command-panel-design.md`。那份的 C2（侧栏审批托盘与 run 内联并存）、C3（面板与 run 屏重复画轨迹）、故事 21 的「历史任务」一半，都在这份里收口；C1（自动进入运行视图）仍单独待拍，不在本片顺手做。
- 状态：R1 施工中。**前提已被换掉一次**：本方案初稿假设"运行视图仍是独立一屏"，另一会话同期把 C1 裁决成形态 C（过程嵌进首页、标签与三态机撤销），R2/R3 已按此重写（回看改由 run 表页承担）。形态 C 带来一个新暴露的同屏重复，见 §8。

## 1. 问题陈述

首页已经能下达、运行视图已经能看过程、审批已经在两处可结算。剩下唯一的重复是右侧那块 AI 面板：它既是一份第二套上下文入口（＋菜单/模型/技能/附件 chips 都在那里又画一遍），又是唯一的"上一条指令的答复"存放处。要退役它，必须先给"回看"找地方，否则删掉的是功能不是冗余。

## 2. 事实基线（2026-09-30 逐条核过，带行号）

1. **侧栏不是一条通路，是一副视图。** 它的发送最终走 `SideChatPanel` → `ssh-ai.sendMessage`(`ssh-ai.ts:563`) → `sendAgentMessage`(`:597`) → `agentStore.runAgent` → `streamAgentRun`(`client.ts:849`) → `POST /agent/run`，与首页同一条。所以删它的发送入口不会少一条能力，只会少一份画法。
2. **多轮上下文在后端，不在面板。** `routes/agent.py:789` 写用户轮、`:1009/:1048` 写 assistant 轮进 `messages` 表；`:605` 每次 run 用 `db.get_recent_messages(conversation_id, limit=context_window)`（`context_window` 缺省 20，`AgentRunRequest`）回灌。⇒ **"追问要靠侧栏留着"是不成立的**；只要 `conversation_id` 还在传，第 2 条指令天然看得见第 1 条的答复。
3. **一条 run 的定格答复已有两处权威源。** DB 的 `messages`（含 `run_id` / `agent_trace` 列）与 `/agent/runs/{id}/trace`；前端唯一消费 trace 的地方是消息气泡内的 ▸轨迹（`AgentFullTrace.vue`）。缺的不是数据，是**一张按 run 检索的表**。
4. **退役的影响面**：`showAiPanel` 一族 31 处引用，散在 `App.vue` / `SshView.vue` / `SshAiPanel.vue` / `useSshNavigation.ts` / `ssh-ai.ts`。
5. **⚠️ 施工时序受并发约束**（本轮实测，两次核过）：另一个会话已在 home spec 里写下 **C1 裁决 = 形态 C**（运行过程嵌进首页，「执行」标签与三态机撤销，`terminalVisible = !showHome`），并已把 `useSshNavigation.ts` / `SshView.vue` / `RunView.vue` 改成那样 —— 15:00 时 typecheck 红 4 处（`SshView` 还在解构已删的 `showRun` / `openRunView`），16:2x 复测已通。**整轮改动仍未提交**，且 `--canvas-bg` / `--accent-on` / `--card-foot-*` / `--font-display` / `--wash` 五个令牌只在树里、HEAD 没有（HEAD 里引用它们的行必须带 fallback，写法见 memory 的守卫条目）。⇒ 我不碰 `RunView.vue` / `SshView.vue` / `useSshNavigation.ts`，直到形态 C 落库。

## 3. 方案概述

形态 C 已经把"过程"放进首页（指令卡 → 待放行队列 → 过程区），所以本方案不再有"给运行视图加东西"这一项。**侧栏退役唯一真正要接住的是一件事：回看**——上一条乃至更早指令的答复与轨迹。今天这个能力只长在侧栏的消息流里，它是侧栏不能被直接删掉的原因。

于是形状是：**新增一张 run 表页作为回看的主入口**（顶栏，与运维审计同排；点开某条 = 拉 `/agent/runs/{id}/trace` 的 DB 权威回放，复用现成的 `AgentFullTrace`），**首页过程区保持只服务"当前这一条"**（单 run 模型下它本来就只能显示一条），**侧栏先降级、再退役**。

顺序不可颠倒的一条原则：**每片在删任何入口之前，先把替代面建好**。"亮的空开关"的反面是"删了入口、留下一份看不见的状态"——侧栏的 chips 一旦被删而它仍在发送，用户在侧栏敲的一句话会悄悄带着首页挂的远端文件与姿态，那比重复更糟。

## 4. 迁移表

| 现在在侧栏 | 去哪 | 备注 |
|---|---|---|
| 审批托盘（`AgentRunPanel.vue:166`） | **删**。内联卡（2026-09-29 spec 的 R4 片已落，形态 C 后长在首页过程区里）+ 首页待放行队列覆盖，同一对象同一结算 | C2 就此关闭；那句"单一焦点审批"的注释已改正 |
| ＋菜单（目标 / 计划 / 技能） | 首页 ＋ 菜单是唯一 setter；状态仍读 `ssh-ai` store | 删的时机 = 侧栏不再是发送面（见下） |
| 模型 / 技能 / 附件 chips | 同上；**必须在侧栏输入框之后删**，否则隐身状态 | |
| 输入框与发送 | 首页指令卡（同一通路，见事实 1） | |
| 消息流（问答 + 每条 run 的固化答复） | **新 run 表页 + 详情回放**（R2）。这是侧栏唯一不能被就地删掉的东西 | DB 里 `messages` / `agent_runs` 已经是权威源，缺的只是那张表 |
| ▸轨迹 回放（`AgentFullTrace`） | **保留在原处**，改挂到 run 表页的详情里 | 不重造：读的还是 `/agent/runs/{id}/trace` |
| 「历史任务」= 会话列表（`HistoryPanel.vue`） | 升成 run 表（时间 / trace / 目标机 / 模型 / 审批档 / 状态 / 成本），顶栏入口 | 补故事 21 缺的那一半 |

## 5. 切片

- **R1 删侧栏审批托盘**（只碰 `AgentRunPanel.vue` + 它的单测；两文件都不在并发会话手里）：托盘区块、批量操作与为它服务的样式。首页队列与首页内联卡（形态 C 后仍在）覆盖同一对象同一结算。验收＝vitest 全绿 + 手工在两处各点一次批准/拒绝。**C2 就此关闭。**
- **R2 run 表页**（后端加 `GET /agent/runs` + 新 `views/ssh/RunHistoryView.vue` + 顶栏入口）：**先做它，因为它是侧栏的替代面**。表能画什么由 `agent_runs` 的列决定（`database.py:236-256`：`id / conversation_id / task / status / iteration / max_iterations / total_tokens / cost_usd / duration_ms / tool_call_count / mode / model_used / started_at / completed_at`）；**没有目标机与审批档两列**，这两格在表里就是空的，要么不画，要么补列——不许拿"当前设置里的选择"顶上去（run 归因那轮踩过的串台坑）。详情层复用 `AgentFullTrace`（读 `/agent/runs/{id}/trace`），**不许用 localStorage 拼一张假表**。后端只有 trace 单查、没有列表端点（`agent.py:1916`；`/schedules/{id}/runs` 是定时任务另一回事），这条 GET 不碰 SSE、不碰审批语义。
- **R3 侧栏降级为只读**（删输入框 / ＋菜单 / 模型·技能·附件 chips）：此时问答与回看已在 R2 里有家。
- **R4 退役容器**（`App.vue` / `SshView.vue` / `useSshNavigation.ts` / `ssh-ai.ts` 的 showAiPanel 一族 + 顶栏「AI」按钮）：**必须等形态 C 落库**——这三份文件正是对面此刻在改的。留最后一个提交做，方便整体回滚。
- **不做**：给首页过程区加"回看上一条"的切换。单 run 模型下 `agent.lastRunId` 只有一条，区里塞历史就是 R2 那张表的重复入口。

## 6. 明确不做

- 不做"问答模式 vs 指令模式"的两套 UI。语义差异由首页那一条框的既有控件承担（不带终端现场、审批档手动、`workflow_mode` 姿态）。**风险如实记着**：一句纯提问也会起一条 agent run、开一个标签——轻量感变差是这次退役的真实代价，用户已知情选定。
- 不动 `messages` / `agent_runs` 的表结构与 SSE 事件形状。
- 不在本片做 C1（自动进入运行视图）。
- 不删 `SideChatPanel.vue` 文件本体直到 R5。

## 7. 测试决策

接缝复用，不新建：R1/R4 的断言落在 `ssh-ai` store（状态只有一份）与 `agent` store 结算三态；R3 若新增"列 runs"接线，接缝在 `client.ts` 的 API 层（mock fetch），不在组件。组件层仍只由 typecheck + build + 用户眼睛验收——本仓拿不到像素证据。

## 8. 形态 C 顺带暴露的一件（要拍，我没自己动）

2026-09-29 spec §4 那条已定决策「审批在首页汇总、在 run 里就地出现，**同一对象**」的原始理由是：**"汇总解决切走就漏看"**。形态 C 把过程搬进首页、标签撤销之后，**"切走"这个场景不存在了** —— 于是首页在同一屏里同时挂着「待你放行」队列卡片与过程区里的内联审批卡，同一条待决请求在一屏内出现两次。这不是缺陷，是那条决策的前提被换掉后剩下的尾巴。

- **实测过的位置**（R1 落库后当场核）：`SshView.vue:104` 渲染 `<HomeApprovalQueue>`，紧接着 `:108` 渲染 `<RunView v-if="runVisible" />` —— 两者同在首页那一屏，队列的 `v-if` 与内联卡读的是同一个 `agent.pendingPermissions`，所以**一条待决请求在首页出现两次**。R1 删掉的是第三处（侧栏托盘），这一处是形态 C 带来的新重复，比原先的跨屏重复更显眼。
- 推荐：**内联卡留，队列降为一行计数条**（`3 条待放行 · 点一下滚过去`，复用他们已经在用的 `scrollIntoView`/`revealRun`）。理由：内联卡带着"这条命令在流程里的位置"，是故事 20 的本意；队列的独立价值本来就是"切走就漏看"的跨屏汇总，单屏单 run 下它只剩重复。
- 反面：故事 10 明写了队列那张卡要含 指令原文/会做什么/目标主机/可逆性/命令本身。若队列没了，这五条验收全部由内联卡承接（`ApprovalCard` 是同一个组件，字段已在），验收口径不变、位置变。
- 时机：与 R1 同一片做最省（都在删重复的审批画法），但它是产品判断，所以先记在这里。
