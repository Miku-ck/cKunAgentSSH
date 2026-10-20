# 首页重构：智能体对话工作台（任务台）

- 日期：2026-10-20
- 状态：设计定稿，已实施
- 术语：见 `CONTEXT.md` 的「任务台 / 会话 / 执行过程 / 目标主机 / 历史抽屉」
- **修订（同日，用户裁决）：本文 F 节与「不删除侧栏」那几条被推翻。** 同日追加裁决
  「删除AI tab的右侧栏目，只保留首页AI智能体功能，然后把最顶端的AI tab默认设为AI首页」，
  侧栏已整体退役（删除面与迁移面见
  `docs/superpowers/specs/2026-09-30-retire-ai-side-panel-design.md` 的 2026-10-20 终态段）。
  AI 从此只有一个界面：任务台。本文其余故事（标签条两枚按钮、历史抽屉、内联过程、空白任务）
  不受影响。
- 标注来源：用户在现有页面上的三处标注 + 逐条对齐访谈结论
  - 标注 A：首页标签的房子图标改成 AI 图标
  - 标注 B：标签条最右侧新增「历史记录」与「新建会话」两枚图标按钮
  - 标注 C：首页从单输入卡空态重构为完整的智能体对话工作区
- 预览：`.workbuddy/design/home-agent-workbench.html`
- 执行计划：`docs/superpowers/plans/2026-10-20-home-agent-workbench-plan.md`

> **规格位置说明。** 项目既有约定是 `docs/superpowers/specs/YYYY-MM-DD-*-design.md`（已有 14 份），
> 本文件跟随该约定，不新建 `docs/specs/`。

---

## 1. 问题陈述

现在的首页由 `HomeTargetTitle` + `HomeCommandCard` + `HomeApprovalQueue` + `RunView`
四块拼成（`SshView.vue:127-131`），能下达一条指令、看这一轮的过程，但不构成一个对话工作台：

1. **没有多轮对话。** 首页只有一张输入卡，没有消息流；完整聊天 UI 在右侧 460px 的
   `SideChatPanel` 里，用户必须先开一个终端、再打开面板才能继续追问。首页发完一条指令后，
   屏幕上留下的是「这一轮的过程」，上一轮的下达内容不再可见。
2. **过程与消息割裂。** `RunView` 是输入卡下方的一块独立面板，只展示当前/最后一轮；
   它既不属于某条助手消息，也无法在回看历史会话时出现。`AgentTraceInline.vue:36`
   当前甚至明确不渲染工具调用。
3. **会话管理不在主屏。** 历史记录与新建对话只在侧栏面板头部；主标签条没有入口。
   首页标签用的还是房子图标，读起来是「启动器」而不是「AI 工作台」。
4. **目标主机不在输入控件里。** 它是页面标题（`HomeTargetTitle`），用户在一个地方换目标、
   在另一个地方发送；主机名在标题里可点，却被当成"标题"理解。
5. **审批只有两处外置入口。** 逐条决策在 `RunView`，批量在家页面板的窄条队列
   （`HomeApprovalQueue`）；它们都不在消息流里该出现的位置。

首页是应用的第一屏，是这个产品的主角。值得把它改成**完整的智能体对话工作台**：
消息流 + 内联执行过程 + 常驻输入区，而不是一张一次性指令卡。

---

## 2. 方案概述

```
┌ TitleBar ─────────────────────────────── [AI ●] [Docker] … [— □ ×] ┐
├ 标签条 ─ [✦ 首页 ●] [终端 1] [终端 2] ＋新建终端 …   [历史] [新建会话] ┤
├ 任务台（首页）───────────────────────────────────────────────────────┤
│  会话标题（空态：「今天想让助手替你做什么？」）                       │
│                                                                     │
│  ┌ 消息列（单列，max-width 820）────────────────────────────────┐   │
│  │ 你：检查 nginx 502 …                                          │   │
│  │ 助手：▾ 执行中 · 第 2 步        [2 条待放行]      №3f2a…      │   │
│  │        第 1 步  2.1s                                          │   │
│  │          ✓ ssh_exec  nginx -t                                 │   │
│  │          ✓ ssh_exec  tail -n 50 …/error.log  [展开输出]       │   │
│  │        ┌ 2 条待你放行   [全部拒绝] [全部批准本次] ┐            │   │
│  │        └ ApprovalCard …                          ┘            │   │
│  │        …                                                      │   │
│  │        回答正文（流式）                                       │   │
│  └───────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌ Composer（max-width 820）─────────────────────────────────────┐   │
│  │ [目标/计划/技能/附件 chips…]                                  │   │
│  │ 输入指令…                                                      │   │
│  │ ＋  [▣ root@prod-01 ▾]   [⌂ 手动审批 ▾]      [模型 ▾] [↑/■]  │   │
│  └───────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                                        历史抽屉 ─┐
                                                        （只盖消息区）│
```

- **首页 = 任务台**：标签条第一个标签，恒在、不可关闭；标题条以下整块主区都是它。
- **单列任务流**：消息是主体，执行过程内联在对应助手回合里，不再有独立运行视图/标签。
- **过程默认折叠语义**：运行中展开；完成后自动收起为一行摘要 `已执行 N 步 · X.Xs`，可再展开。
- **输入区常驻底部**：空态与有消息时位置一致；目标主机、审批档、模型都在工具条里可操作。
- **历史抽屉**：标签条右侧入口，从消息视口右侧滑出；只覆盖消息区，不遮输入区。
  （`DESIGN.md` 对抽屉的禁令只开这一个口子，措辞已写入该文件。）
- **侧边快捷面板保留**：与任务台共享同一份会话/草稿/选择状态，不是第二套系统；
  新建会话也走"空白任务"语义；它不渲染逐条审批卡，只提示并引导回任务台。

### 2.1 页面状态机

| 状态 | 判据 | 主区内容 | 输入区 |
|---|---|---|---|
| 空白任务 | `currentConvId === null` 或当前会话 `messages` 为空 | 空态问句 | 可发送；发送后懒建会话 |
| 对话中（空闲） | 有会话且有消息，`agent.isRunning === false` | 标题 + 消息流 | 发送键 |
| 运行中 | `agent.isRunning === true` | 标题 + 消息流；最后一条助手消息的过程展开 | 停止键；文本可继续输入 |
| 待审批 | `agent.pendingPermissions.length > 0`（运行中的子态） | 过程内出现审批卡与批量组头 | 同上；状态点转警示 |
| 历史抽屉 | `homeHistoryOpen === true` | 消息区右侧抽屉 + 遮罩 | 不受遮挡，仍可输入 |

运行中不允许新建任务、不允许切换/删除当前会话（见 §5.7）——单 run 是全局状态，
切走会让正在等待的审批与过程失去落点。

---

## 3. 用户故事

### A. 外壳与导航

1. **AI 首页图标。** 首页标签的图标是一枚 AI 星芒（sparkle），不再是房子；文字仍为「首页」，
   标签恒在、不可关闭。这是标注 A 的验收点。
2. **右侧两枚按钮。** 标签条最右侧、滚动区之外，固定两枚图标按钮：历史记录（时钟/列表）
   与新建会话（撰写/加号）。它们不被终端标签挤走，滚动标签区时保持不动。这是标注 B 的验收点。
3. **按钮总是可发现。** 两枚按钮在任意标签页都可见；点击任一枚都先切回首页：
   历史记录打开抽屉；新建会话进入空白任务并关闭抽屉。运行中「新建会话」禁用，
   `title` 写明「运行中不可新建会话」。
4. **首页状态点。** 首页标签在有待审批 / 运行中 / 未读回复时显示状态点；优先级
   待审批 > 运行中 > 未读。颜色不是唯一语义：标签的 `title` / `aria-label` 同时给出
   「N 条待审批」「运行中」「有新回复」。运行中状态点做呼吸动画，`prefers-reduced-motion`
   下停用。
5. **顶栏 AI 徽章同源。** `TitleBar` 的 AI 按钮徽章与首页状态点读同一个 `useAgentPresence()`
   结果，优先级一致：待审批（警示色）> 运行中（呼吸）> 未读。AI 按钮的 `title` 同步带数量。
6. **侧栏的待审批提示。** 侧边面板不画逐条审批卡（保持"单一逐条决策位"）。有待审批时，
   `AgentRunPanel` 顶部出现一条「N 条待你放行 · 去任务台」，点击切回首页并关闭侧栏；
   它没有批准/拒绝按钮，不是第二个决策位。
7. **运行中离开首页。** 切到终端标签不打断 run；首页状态点继续显示运行中/待审批；
   回到首页时过程和审批卡仍在。若 run 完成时用户不在首页且侧栏关闭，才点亮"未读"。

### B. 会话与消息

8. **空白任务。** 打开应用且没有恢复会话（或点「新建会话」）时，主区显示空态问句
   「今天想让助手替你做什么？」。目标主机不在问句里重复，只在输入区 chip 显示。
9. **首条消息才入历史。** 点「新建会话」只进入空白任务，不产生历史条目；发出第一条消息时
   才创建会话并出现在历史抽屉（复用 `_sendAgentMessageInner` 的懒建路径）。
   新建动作清空：草稿、技能、附件、姿态、工作流缓存；保留：审批档、模型、终端现场开关。
10. **标题。** 空态显示问句；有消息后同一位置换成会话标题（本地首条摘要，随后端命名更新）。
    标题行随消息流滚动，不额外增加一条固定头部。
11. **多轮对话。** 用户继续输入会追加到同一会话；上一条助手消息（含其内联过程）原样保留，
    不会被新 run 覆盖。消息按时间正序，用户右对齐、助手占满列宽；复用 `MessageItem`
    渲染 Markdown、复制、耗时与「▸轨迹」入口。
12. **会话切换。** 历史抽屉点击会话走现有 `sshAi.switchConversation`：清技能/附件、
    重新加载工作流、空消息时懒拉后端消息。草稿仍留在共享 `sharedDraft`（与侧栏一致）。
13. **自动滚动。** 设置 `chat.autoScroll` 打开时，新消息、流式内容与审批卡出现都滚到底；
    关闭时不强制。用户向上滚动的"暂停跟随"不在本片。
14. **运行中守卫。** 运行中：新建会话禁用；历史抽屉可打开但所有会话切换禁用、当前会话删除禁用；
    禁用原因通过 `title`/`aria-disabled` 说明。运行结束恢复。

### C. 输入区（Composer）

15. **常驻布局。** 输入区固定在任务台底部，空态与有消息时位置一致；消息列与输入区共用
    最大宽度（约 820px）居中，960px 宽度下不横向溢出。
16. **目标主机 chip。** 工具条中显示当前目标：远程会话为 `shell`（创建时即 `user@host`），
    本地会话为 shell 名，无会话时显示「服务器」。菜单列出已打开的会话、当前项打勾；
    选择只改 `ssh.activeSessionId`，不切走首页、不聚焦终端。两台以上时菜单底部出现
    「全部 N 台」，走现有批量下发弹窗（`SshView` 的 `showBulk`）。目标在发送时固定进该 run，
    运行中改 chip 不影响已在跑的 run。
17. **＋ 菜单。** 与现状一致：目标姿态、计划姿态、技能、终端现场、本地文件、远端文件。
    已选项在输入框上方生成可移除 chip：姿态（目标/计划共用一枚）、技能（逐个）、
    终端现场（仅当真有选区或最近命令时出现）、附件（逐个，`title` 标明本地/远端与截断）。
18. **审批档与模型。** 底栏右侧保留审批档（手动审批 / 自动审批）与模型选择；与侧栏同一档位。
    计划姿态点亮时审批按钮显示「只读计划」并禁用（`resolveRunModes` 下 plan 恒压过审批轴）。
    未配置模型沿用「尚未配置模型 · 去设置」。
19. **发送/停止。** 快捷键遵循设置 `chat.sendShortcut`（默认 Enter 发送、Shift+Enter 换行；
    shiftEnter 档反之），首页与侧栏同口径；界面不印快捷键说明。无文本且无附件时发送禁用。
    运行中 textarea 可继续输入，发送键变为停止键，点击调用 `agent.abort()`；运行中按 Enter
    给出「AI 正在运行中」提示，不排队。
20. **发送结果。** 成功清空草稿/技能/附件（姿态与审批档保留）；被拒（后端未就绪、无模型、
    运行中、建会话失败）时草稿与附件原样保留并 toast。附件预算与截断沿用现有
    `_sendAgentMessageInner` 的 50K 口径。
21. **空输入提示。** 输入框 `placeholder` 为「输入指令…」；空态问句仍承担主要引导，
    不另加解释性灰字段落。

### D. 内联执行过程与审批

22. **位置与折叠。** 每个助手回合的执行过程显示在回答正文上方，属于该条消息。
    运行中默认展开；run 结束自动折叠为 `已执行 N 步 · X.Xs`（N = `buildTraceModel` 的批次步数）。
    用户可在任意时刻点开/收起；完成后手动展开的状态不被后续重渲染重置。
23. **运行中头部。** 左侧显示状态与步号：`执行中 · 第 n 步`（n 取 `activeStepIndex + 1`；
    还没有工具调用时显示「思考中」）。计时读 `agent.runStartedAt` 的本地 ticker。
    右侧在有待批时显示「N 条待放行」，有 run id 时显示整串 trace 号（点击复制）。
24. **步骤。** 过程体按 iteration 分组（`buildTraceModel`），每组一行「第 n 步 · X.Xs」；
    工具行显示状态（运行中/成功/失败/未回写）、工具名、命令（monospace，Bash 类取
    `args.command`，其余取参数摘要）；输出折叠为前 3 行，可「展开输出（N 行）」。
    没有工具调用的 live 阶段显示现有「思考中」三点。
25. **审批就地。** 待批卡按时间戳与工具事件归并（`mergeStream`），出现在它该出现的位置；
    复用 `ApprovalCard` 的四档动作、风险、可逆性、目标主机与授权命令。审批卡只对当前 run
    可见：审批请求不写入 `agentTrace`，历史消息展开时没有审批卡，需要追溯去审计页/轨迹回放。
26. **批量组头。** 未决审批 ≥ 2 时，在第一条审批卡前显示组头「N 条待你放行」+
    「全部拒绝」「全部批准本次」（全部批准只授 once，不授 session/always）。
    结算中禁用重复点击；run 已结束（once 不可达）时「全部批准本次」禁用。
    逐卡与批量都走 `useApprovalActions`，首页唯一批量入口就是这里（不再有 `HomeApprovalQueue`）。
27. **归因信息。** 展开的过程底部显示本次 run 的模型名、审批档、目标主机与耗时；
    数据分别取 `agent.lastModelId` / `lastPermissionMode` / `lastSshSessionId`（发送时一次性记录），
    且仅当这条消息的 trace 属于最近一次 run（`lastRunId` 相同）时显示；历史旧 run 不伪造
    当前设置。模型在模型列表里查不到时不渲染内部 id。
28. **trace 号。** 过程头部显示整串 12 位 trace 号（不截断），点击复制并 toast 说明可粘贴到
    运维审计的 trace 筛选框。历史消息仍保留 `MessageItem` 的「▸轨迹」数据库回放入口。
29. **终态文案。** 中止显示「已中止」，失败显示「失败」，`human_help` 显示「需要人工」，
    `max_reached` 显示「达到迭代上限」；不得把中止显示成「已完成」。
    为支持历史重载后仍能区分中止，`AgentTraceSnapshot` 新增可选 `aborted?: boolean`
    （`agent.snapshot()` 写入；旧数据没有该字段时按 failed 展示）。
30. **中止后的过程。** 中止时审批卡随 store 清理消失；过程头部转「已中止」；未回填的工具行
    在固化时被标成「运行结束前未回写结果」，过程不得继续显示"进行中"。

### E. 历史抽屉

31. **入口与范围。** 标签条右侧「历史记录」按钮打开抽屉；抽屉在消息视口内右侧滑出，
    宽 `min(320px, 80vw)`，带左侧描边与阴影；遮罩只盖消息区，不盖输入区。
    再次点按钮、关闭按钮、Esc、点遮罩均可关闭。
32. **内容。** 复用分组历史数据（今天/昨天/日期，组内与组间均按更新时间倒序）；
    当前会话高亮；空态「还没有会话」。列表行与侧栏历史使用同一个列表组件，避免两套行样式。
33. **切换与删除。** 点击会话切换并关闭抽屉；删除需要 `ConfirmDialog` 二次确认，
    删除当前会话后落到列表第一条或空白任务。后端删除失败时保留列表并 toast（沿用现有语义）。
34. **运行中。** 抽屉可打开但切换与删除禁用；行上 `title` 说明「运行中不可切换会话」。
    不改变抽屉的布局或用一个灰字段落解释。
35. **焦点与键盘。** 抽屉 `role="dialog"`、`aria-modal="false"`（它不阻断输入区）；
    打开时焦点进入抽屉，Esc 关闭后焦点回到历史按钮；Tab 在抽屉内循环。
36. **无障碍。** 首页标签可键盘聚焦，Enter/Space 进入首页；两枚图标按钮有 `aria-label`；
    状态点/徽章 `aria-hidden`，语义由按钮文案承担；抽屉宽度在 960px 下不回溢。

### F. 侧边快捷面板（保留，不改形态）

> **本节已作废（同日追加裁决）**：侧栏整体退役，故事 37–41 的共享状态/同源口径由任务台独占
> 满足——只有一份状态可共享时，「两处各存一份会漂移」这条风险连载体都没有了。

37. **同源状态。** 侧栏与任务台共享 `currentConvId`、`messages`、`sharedDraft`、技能/附件、
    审批档与模型；任意一处的发送、切换、选择在另一处立即可见。侧栏仍是窄版快捷视图，
    不是第二套会话系统。
38. **新建会话。** 侧栏头部「新建对话」改走 `beginNewTask()`：进入空白任务、不产生历史条目，
    删除原先的 500ms 人工锁定与 `creating` 转圈；运行中禁用并说明原因。
39. **live 消息合并同源。** 侧栏与任务台共用同一个 `buildDisplayMessages` 纯函数
    （从 `SideChatPanel.vue:883-906` 抽出），只有最后一条 `temp-agent-` 助手消息叠加
    `agentStore` 的 content/reasoning/实时快照；其余消息保持引用稳定。
40. **侧栏历史。** 侧栏历史覆盖层继续存在（窄面板不适合抽屉），但运行中同样禁止切换；
    它与任务台抽屉读同一份 `groupedConversations`。

---

## 4. 已定决策

| 决策 | 取舍与理由 |
|---|---|
| 首页 = 主对话面，侧栏保留为快捷面板 | 用户已选。删除侧栏会丢掉窄屏/终端旁随手问的能力；保留两套却各存状态会漂移，所以强制共享 store |
| Codex / Claude Code 形态 | 单列任务流、底部常驻输入、过程内联；不做左右分栏工作台 |
| 过程内联 + 默认可折叠 | 过程属于那一轮回答；运行中展开、完成后收起，历史回看不被过程淹没 |
| 目标主机放输入区 chip | 目标可以逐条消息变化；放在会话头部会谎称整段会话一个目标。选择只改 `activeSessionId`，不跳终端 |
| 历史从右侧抽屉展开 | 用户裁决，`DESIGN.md` 的抽屉禁令为此开唯一例外；抽屉不盖输入区，切换会话时不打断输入 |
| 内联审批 + 多条组头批量 | 逐条决策只有消息流一个位置；批量只是快捷结算，不是第二个决策位。批量只授 once |
| 空白任务，发出首条才入历史 | 避免点「新建会话」就产生空条目；复用现有懒建会话路径，不新增后端 |
| 空态问句，有消息后换标题 | 空态需要一句邀请；有内容后标题承担定位。问句不再嵌目标名，避免和 chip 重复、且主机名已不可点 |
| 首页标签状态点 + 顶栏 AI 徽章 | 两处读同一个 `useAgentPresence`；优先级待审批 > 运行中 > 未读，文案同步，不只靠颜色 |
| 删除 HomeTargetTitle / HomeCommandCard / HomeApprovalQueue / RunView | 能力不是被丢掉，而是并入 HomeWorkspace / HomeComposer / 内联过程；旧入口连同组件一起删，不留两种都能进 |
| trace 整串显示、点击复制 | 12 位是防碰撞长度，截 8 位看着像全号却筛不到；复制→审计筛选是已有真实界面 |
| 不把审批写进 agentTrace | 审批卡是运行期对象；持久化需要后端契约与恢复语义，本片不做。历史审批去审计/轨迹 |
| 侧栏只提示不决策 | 保持单一决策位；侧栏的「去任务台」只是一条导航，避免同屏两张同 id 卡可点 |
| 不新增依赖、不写裸色值 | 全部用现有 SVG 与 `variables.css` 令牌；主题层不因本片改令牌名 |

---

## 5. 状态模型与接缝

### 5.1 空白任务

`currentConvId === null` 即空白任务。新增：

- `useChatPersistence.beginNewTask()`：只把 `currentConvId.value = null`；不动
  `conversations`、不写 localStorage（当前会话本就不持久化）。
- `useSshAiStore.beginNewTask(): boolean`：
  - `agent.isRunning` → 返回 `false`（UI 已禁用，store 仍兜底）；
  - 调 `persistence.beginNewTask()`；
  - 清 `sharedDraft`、`selectedSkillIds`、`pendingAttachments`、`posture = null`、
    `workflowSnapshot` 空工件、`unreadReply = false`；
  - 保留 `approvalMode`、`selectedModelId`、`attachTerminalContext`。
- 发送仍由 `useHomeCommand.submit()` → `sshAi.sendMessage()` →
  `_sendAgentMessageInner()`；该函数在 `!conv` 时懒建会话（`ssh-ai.ts:722-745`），
  本地标题取首条 30 字摘要、后端标题送 `'New Chat'` 交给 post_processor（S9-05）。
  **本片不改这条路径的语义**，只保证 `beginNewTask` 不提前建条目。
- 启动恢复：localStorage 有会话时仍恢复第一条（现状），不是每次启动都强制空白任务；
  空白任务是「新建会话」与「没有可恢复会话」时的状态。

### 5.2 状态点 / 未读

新增 `src/composables/useAgentPresence.ts`：

```ts
type AgentPresence = 'pending' | 'running' | 'unread' | 'idle'
// 优先级：pendingPermissions.length > 0 → running → unreadReply → idle
```

- `pendingCount` 一并返回，供 title/aria 文案使用。
- `ssh-ai` 新增 `homeVisible`（`setHomeVisible(v)`），由 `SshView` watch
  `homeTabActive` 同步；`setHomeVisible(true)` 清 `unreadReply`。
- 固化 watch 设置未读改为 `!showAiPanel && !homeVisible`（`ssh-ai.ts:370`）。
  这样用户正看着任务台时不会为一条已经显示的回复亮未读点。

### 5.3 消息展示（唯一一份 live 合并）

新增 `src/utils/display-messages.ts`：

- `isLiveAgentRun(messages, idx, running)`：最后一条、assistant、id 以 `temp-agent-` 开头。
- `buildDisplayMessages(messages, live)`：对 live 那条叠加 `content`/`reasoning` 与
  `{ ...snapshot(), lastRunId: '' }`；其余原引用返回。
- `SideChatPanel` 与 `HomeWorkspace` 都改用它；输出逻辑继续由 `MessageItem` 承担。
- 首页给最后一条 live 消息传 `inlineApprovals = agent.pendingPermissions`；侧栏不传。
  是否属于 live 由 `is-live` 决定，历史消息不显示审批卡。

### 5.4 内联过程

重写 `src/components/agent/AgentTraceInline.vue`，保持文件名与 `MessageItem` 的接入点：

- props：`trace`、`isLive`、`approvals?: PermissionRequest[]`、`meta?: ProcessMeta`。
- `model = buildTraceModel(trace.toolCalls)`；`stream = mergeStream(model, approvals)`。
- `expanded` 初值 = `isLive`；`watch(isLive)` 在 true→false 时收起。
- 状态文案与颜色由 `trace.aborted` / `trace.status` / `isLive` 决定；live 计时读
  `agent.runStartedAt`，完成后用 `trace.duration`。
- 审批组头在第一个 `kind === 'approval'` 前渲染；只在 `approvals.length > 1` 时出现。
- trace 复制、归因信息如上；归因 `meta` 由 `HomeWorkspace` 组装并只传给与
  `agent.lastRunId` 匹配的消息。
- `MessageItem` 新增可选 props 并透传；其余调用方不传，行为不变。

### 5.5 历史列表

抽出 `src/components/chat/HistoryConversationList.vue`（纯列表：groups/currentConvId/isEmpty/
busy，emit switch/delete）。`HistoryPanel` 保留覆盖层外壳并内嵌该列表；新的
`HomeHistoryDrawer` 用同一列表做右侧抽屉。这样行样式、分组标题、当前项高亮与禁用态只有一份。

### 5.6 目标主机

`useHomeTarget` 不变（`targetLabel` / `options` / `pick` / `canBulkDispatch`）。
`HomeComposer` 用它渲染 chip 与菜单；`bulk` 事件经 `HomeWorkspace` 冒泡到 `SshView`
打开现有批量弹窗。旧 `HomeTargetTitle` 删除。

### 5.7 运行中守卫

| 动作 | 运行中 | 理由 |
|---|---|---|
| 发送新消息 | 拒绝（发送键变停止） | 单 run；防止队列假象 |
| 新建会话（首页/侧栏） | 禁用 | 切走会让 live run 与审批失去落点 |
| 切换会话（抽屉/侧栏历史） | 禁用 | 同上；且侧栏不显示审批卡 |
| 删除当前会话 | 禁用 | 删除正在跑的消息容器 |
| 删除其他会话 | 抽屉内一并禁用（简单一致） | 避免用户误以为整个列表可操作 |
| 修改目标 chip | 允许 | 只影响下一次发送，发送时已固定 |
| 打开抽屉/侧栏 | 允许 | 只读浏览 |

### 5.8 宿主接线（`SshView.vue`）

- 首页标签图标换 AI 星芒；标签加状态点（读 `useAgentPresence`）与动态 `title`/`aria-label`。
- `.term-tabs` 内、`.tabs-scroll` 外新增右侧固定按钮组（历史/新建会话）；
  `aria-expanded` 绑 `homeHistoryOpen`。
- `homeHistoryOpen` 由 `SshView` 持有（与抽屉跨组件，且不能像 `useSshNavigation` 那样每次
  调用新建一份状态），以 prop + emit 传给 `HomeWorkspace`。
- `homeTabActive` watch → `sshAi.setHomeVisible()`；离开首页时关闭抽屉。
- 首页面板内容替换为 `<HomeWorkspace :history-open="homeHistoryOpen" @close-history=…
  @bulk="showBulk = true" />`；删除 `ShowTargetTitle`、`HomeCommandCard`、`HomeApprovalQueue`、
  `RunView`、`runVisible`、`runRef`、`revealRun` 的接线。
- 侧栏 `SshAiPanel` 新增 `@open-home` → `closeAiPanel(); goHome()`。

---

## 6. 测试决策

### 6.1 接缝与已有测试

| 接缝 | 已有测试 | 本片处置 |
|---|---|---|
| `useHomeCommand.submit` 单通路 / 透传 / 失败保留 | `src/composables/useHomeCommand.test.ts` | 不改语义，继续全绿；HomeComposer 仍只调它 |
| 目标来自 `activeSessionId`、无会话回落、全部 N 台门槛 | `src/composables/useHomeTarget.test.ts` | 不改，继续全绿 |
| 轨迹分组与审批归并 | `src/utils/run-trace.test.ts` | 不改，内联过程复用同一输出 |
| 会话持久化 | `src/composables/useChatPersistence.test.ts` | 加 `beginNewTask` 用例 |
| 会话 CRUD 暂存输入 | `src/stores/ssh-ai.test.ts` | 加 `beginNewTask`、`homeVisible`/未读用例 |
| 审批批量 | `src/composables/useApprovalActions.test.ts` | 不改，组头复用 |
| 令牌/主题 | `src/styles/variables.test.ts` | 新样式只用现有令牌 |

### 6.2 新增测试

- `src/composables/useAgentPresence.test.ts`：优先级 pending > running > unread > idle；
  pendingCount；homeVisible 清未读后的结果。
- `src/utils/display-messages.test.ts`：只合并最后一条 live temp 消息；其余引用不变；
  `lastRunId` 被清空；非运行时不合并。
- `useChatPersistence.test.ts`：`beginNewTask` 只清 `currentConvId`，不增删会话、不落盘。
- `ssh-ai.test.ts`：`beginNewTask` 清草稿/技能/附件/姿态/工作流、保留审批档/模型/终端现场；
  运行中返回 false；未读只在 `!showAiPanel && !homeVisible` 时点亮。
- 终态文案（中止/失败）作为 `AgentTraceInline` 的纯映射函数抽到
  `src/utils/process-status.ts` 并测试；组件只负责渲染。

### 6.3 手工验收（UI 无法用纯单测覆盖）

在 `npm run dev` 的真实 Electron 窗口中逐项走查：

1. 空态：AI 图标、右侧两枚按钮、问句、目标 chip；输入发送第一条 → 消息出现、历史多一条。
2. 运行中：过程展开、工具行状态变化、审批卡出现、批量组头、首页点与顶栏徽章均为警示；
   输入区发送键变停止；中止后过程显示「已中止」。
3. 完成：过程自动折叠、摘要步数/时长正确；展开可看输出、trace 复制；再次展开保持。
4. 历史抽屉：只盖消息区、输入区可继续输入；切换会话、删除确认；运行中切换/删除禁用。
5. 侧栏：发送/切换/草稿与首页同步；待审批时出现「去任务台」；侧栏不出现审批卡。
6. 960px 窗口：无横向滚动；抽屉不遮输入区；空态/有消息/运行中三种高度下都正常。
7. 主题：切换现有 7 套主题（至少深色/浅色/手记），无裸色断层、无对比不可读。

### 6.4 完成判据

- `npm run typecheck` 双 config 0 错误。
- `npm test` 全绿（记录基线数量，新增用例计入）。
- `npm run build` 成功。
- 上述手工验收全过；预览稿与实现的关键状态一一对应。

---

## 7. 明确不做

- **不做多 run 并发。** 全局仍是单 run；批量审批只结算当前队列。
- **不做消息分支/编辑/重新生成、不做消息树。** 会话仍是线性消息数组。
- **不持久化审批卡，不把审批写进 `agentTrace`。** 历史审批去审计/trace 回放。
- **不为历史旧 run 回填模型/审批/目标归因。** 只有最近一次 run 显示，查不到就不显示。
- **不做自动跳转。** 从侧栏下达后不强制切到任务台；状态点/徽章负责提示。
- **不新增后端接口、数据库迁移或依赖。**
- **不删除侧栏、不改侧栏宽度/拖拽行为。**
- **不把计划/目标工件画进任务台。** 工作流仍走侧栏的阶段菜单，`＋` 菜单只保留姿态。
- **不做拖拽上传、粘贴附件、消息重排等现有 Composer 没有的交互。**
- **不改主题令牌名与主题机制；不新增第三层背景。**
- **不写解释性灰字段落。** 输入语义由问句/placeholder/chip title 承担。

---

## 8. 执行分片依据

垂直切片，每片结束时应用可运行、可验收：

| 片 | 主题 | 关键产出 | 依赖 |
|---|---|---|---|
| S1 | 外壳 | AI 图标、右侧历史/新建按钮、首页状态点、TitleBar 徽章同源、`homeVisible` 未读语义 | — |
| S2 | 会话状态 | `beginNewTask`（persistence + store）、侧栏新建改造、运行中切换/删除守卫、历史列表抽取 + `HomeHistoryDrawer` | S1（抽屉入口） |
| S3 | Composer | `HomeComposer`（目标 chip、停止键、既有 ＋/审批/模型/chips 迁移），替换 `HomeTargetTitle` / `HomeCommandCard` | S2（空白任务） |
| S4 | 任务台主流程 | `HomeWorkspace`（标题/空态/消息流/滚动）、`display-messages`、`AgentTraceInline` 内联过程 + 审批 + 批量组头、`aborted` 字段、删除 `HomeApprovalQueue` / `RunView` | S3 |
| S5 | 收尾 | 侧栏待批提示、侧栏 live 合并同源、响应式/无障碍、删旧文件与 import、预览对照、全量验证 | S1–S4 |

详细文件清单、步骤与每片验收命令见执行计划。

---

## 9. 补充说明

- **预览口径**：`.workbuddy/design/home-agent-workbench.html` 是版面与交互口径，不是像素验收稿；
  实现以本文故事与现有设计令牌为准，预览与实现冲突时以本文为准并修正预览。
- **运行视图退役**：`RunView.vue` 删除后，`useSshNavigation` 里与 run 标签有关的残留（若有）
  一并清理；`agent-panel` 槽位继续留给侧栏的异常/澄清面板。
- **消息渲染**：首页不另写消息气泡；`MessageItem` 的现有样式在更宽的 820px 列里直接成立，
  用户消息仍是右对齐边框块、助手无气泡。若实现中发现宽度导致阅读行过长，调列宽而不是重写消息组件。
- **审批安全链**：本片不碰 `backend/agent/harness/reins/gate.py` 的自动审批语义（S2-01 仍是
  独立后端缺陷）；前端不宣称修复了「拒绝过的命令被自动放行」。
- **性能**：首页消息列表与侧栏共用同一份 live 合并纯函数，其余消息保持引用稳定；
  不为每帧流式输出重建整个列表（沿用 SideChatPanel 现有的"只覆盖最后一条"策略）。
