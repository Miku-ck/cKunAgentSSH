# 首页重构：智能体对话工作台 — 执行计划

- 日期：2026-10-20
- 规格：`docs/superpowers/specs/2026-10-20-home-agent-workbench-design.md`
- 预览：`.workbuddy/design/home-agent-workbench.html`
- 术语：`CONTEXT.md` 的「任务台 / 会话 / 执行过程 / 目标主机 / 历史抽屉 / 侧边快捷面板」
- 分片：S1 外壳 → S2 会话状态与历史抽屉 → S3 Composer → S4 任务台主流程 → S5 收尾

## 0. 执行前约定

1. **工作树有并行会话的未提交改动。** 开工前先 `git status --short` 记录现状；
   只按本计划列出的文件精确 `git add`，禁止 `git add -A`、`git reset --hard`、
   `git checkout -- .`、`git stash` 全量操作。发现目标文件里有不属于本片的改动时，
   先读 diff 再决定，不覆盖别人的工作。
2. **基线。** 开工前记录 `npm run typecheck`、`npm test` 的当前结果；若已有红项，
   先确认是否与本片相关，不把既有红项算成新引入。
3. **每片退出条件。** `npm run typecheck` 0 错误；`npm test` 全绿（含本片新增用例）；
   S1、S4、S5 追加 `npm run build` 成功。UI 走查用 `npm run dev` 的真实 Electron 窗口。
4. **不新增依赖、不改后端接口/数据库、不写裸色值。** 所有样式只读 `src/styles/variables.css`
   现有令牌；新文件不引入第三方库。
5. **不保留双入口。** 旧组件在对应能力迁完后删除，旧 import、旧 CSS、旧注释一并清理。

### 0.1 分片顺序与一处细化

规格的分片表为 S1 外壳 → S2 会话状态 → S3 Composer → S4 主流程 → S5 收尾。
本计划做一处细化：**`beginNewTask()` 的最小实现随 S1 落地**，否则标签条右侧的
「新建会话」按钮在第一片里只能是一个没有行为的按钮。S2 仍负责空白任务的
侧栏接线、运行中守卫、历史列表抽取、历史抽屉与全部用例。

---

## S1 外壳：AI 图标、右侧两钮、状态点

### 目标

- 首页标签换成 AI 星芒图标；标签可键盘聚焦，带状态点与动态文案。
- 标签条最右侧、滚动区之外固定「历史记录」「新建会话」两枚图标按钮。
- 顶栏 AI 按钮徽章与首页状态点读同一个 `useAgentPresence()`。
- `homeVisible` 修正未读语义：用户正看着任务台时不点亮未读。
- `beginNewTask()` 落地（persistence + store），供「新建会话」按钮使用。

### 改动文件

| 类型 | 文件 | 内容 |
|---|---|---|
| 新增 | `src/composables/useAgentPresence.ts` | 纯映射 + Pinia 包装，优先级 pending > running > unread > idle |
| 新增 | `src/composables/useAgentPresence.test.ts` | 纯映射的优先级与 pendingCount |
| 修改 | `src/composables/useChatPersistence.ts` | 新增 `beginNewTask()`：只把 `currentConvId` 置 null |
| 修改 | `src/composables/useChatPersistence.test.ts` | `beginNewTask` 不增删会话、不落盘 |
| 修改 | `src/stores/ssh-ai.ts` | `homeVisible` / `setHomeVisible`；未读 watch 条件；`beginNewTask()` |
| 修改 | `src/stores/ssh-ai.test.ts` | `beginNewTask` 的清理/保留项与运行中拒绝；`homeVisible` 清未读 |
| 修改 | `src/views/ssh/SshView.vue` | AI 图标、状态点、右侧按钮组、`homeHistoryOpen`、`homeVisible` watch |
| 修改 | `src/layouts/TitleBar.vue` | 徽章改读 `useAgentPresence()`，新增 pending 态与数量文案 |

### 实施步骤

1. **`useAgentPresence.ts`**

   ```ts
   export type AgentPresence = 'pending' | 'running' | 'unread' | 'idle'

   export function resolveAgentPresence(input: {
     pendingCount: number
     running: boolean
     unread: boolean
   }): AgentPresence
   ```

   - 纯函数先行：`pendingCount > 0 → 'pending'`，否则 `running → 'running'`，
     否则 `unread → 'unread'`，否则 `'idle'`。
   - `useAgentPresence()` 读 `agent.pendingPermissions.length`、`agent.isRunning`、
     `sshAi.unreadReply`，返回 `{ presence, pendingCount }` 两个 computed。
   - 两处消费（TitleBar、SshView）必须只读这个 composable，不各自重排优先级。

2. **`useChatPersistence.beginNewTask()`**

   ```ts
   function beginNewTask() {
     currentConvId.value = null
   }
   ```

   - 不动 `conversations`、不调用 `saveConversations`。当前会话本来就不持久化。
   - 返回并导出；调用方不需要 await。

3. **`ssh-ai.beginNewTask(): boolean`**

   - `agentStore.isRunning` → 返回 `false`（UI 已禁用，store 兜底）。
   - `persistence.beginNewTask()`。
   - 清空：`sharedDraft`、`selectedSkillIds`、`pendingAttachments`、`posture = null`、
     `workflowSnapshot = { phase:'', spec:'', plan:'', goal:'' }`、`unreadReply = false`。
   - 保留：`approvalMode`、`selectedModelId`、`attachTerminalContext`。
   - 不中止正在跑的 run（运行中直接拒绝，不进入这里）。
   - 导出 `beginNewTask`。

4. **未读语义**

   - 新增 `const homeVisible = ref(false)`；`setHomeVisible(v)` 在 `v === true` 时
     清 `unreadReply`。
   - 固化 watch（`ssh-ai.ts` 原 `if (!showAiPanel.value) unreadReply.value = true`）
     改为 `if (!showAiPanel.value && !homeVisible.value) unreadReply.value = true`。
   - `SshView` 中 `watch(homeTabActive, active => { sshAi.setHomeVisible(active); if (!active) homeHistoryOpen.value = false })`，
     带 `immediate: true`，保证首帧就同步。

5. **`SshView` 标签条**

   - 首页标签图标替换为星芒 SVG（四角星 + 小星），标签仍为「首页」，恒在不可关。
   - 标签补 `role="tab"`、`:aria-selected="homeTabActive"`、`tabindex="0"`、
     `@keydown.enter.prevent="goHome"`、`@keydown.space.prevent="goHome"`。
   - 状态点：`<span v-if="presence !== 'idle'" class="home-tab-dot" :class="presence" aria-hidden="true">`；
     标签 `title` / `aria-label` 同步：待审批 `首页 · N 条待审批`、运行中 `首页 · 运行中`、
     未读 `首页 · 有新回复`。
   - `.tabs-scroll` **之外**、`.term-tabs` 内新增 `.tab-actions`：
     - 历史记录按钮：`aria-expanded="homeHistoryOpen"`、`aria-controls="home-history-drawer"`，
       点击 `homeHistoryOpen = !homeHistoryOpen`，并先 `goHome()`。
     - 新建会话按钮：`agentStore.isRunning` 时 `disabled` 且有 `aria-disabled="true"`，
       `title="运行中不可新建会话"`；点击 `sshAi.beginNewTask()` 后 `goHome()` 并关闭抽屉。
       返回 `false` 时 `toast.info('AI 正在运行中，请等待完成')`。
     - 两枚按钮用 `aria-label` 承担语义，`title` 补充当前状态。
   - `homeHistoryOpen` 由 `SshView` 持有（不要放进 `useSshNavigation`：那样每次调用都会新建一份）。
   - S1 只落状态与按钮，抽屉组件在 S2 挂载；S1 验收以「按钮存在、切回首页、状态切换」为准。

6. **`TitleBar` 徽章**

   - 用 `const { presence, pendingCount } = useAgentPresence()` 替换现有的
     `agent.isRunning` / `sshAi.unreadReply` 分支。
   - 徽章：`presence === 'pending' → .pending`、`running → .run`、`unread → .unread`。
     `.pending` 用 `--status-warn-text` 作底色，不呼吸；`.run` 保留现有呼吸动画。
   - `title`：`AI 运维助手（N 条待审批，Ctrl+Shift+B）` / `（运行中，…）` / `（有新回复，…）`。
   - 删除不再使用的 `agent` / `sshAi` import（若仅剩这里使用）。

7. **测试**

   - `resolveAgentPresence`：四档优先级；`pendingCount` 原样返回。
   - `useChatPersistence.beginNewTask`：有会话时把 `currentConvId` 置 null；会话数量与
     `localStorage` 内容不变。
   - `ssh-ai.beginNewTask`：
     - 清空草稿/技能/附件/姿态/工作流/未读；保留审批档、模型、终端现场开关；
     - `agent.isRunning === true` 时返回 false 且不改变 `currentConvId`。
   - `homeVisible`：`unreadReply` 为 true 时 `setHomeVisible(true)` 清掉；离开首页不清。
   - 未读固化条件：用占位消息 + `agent.status` 从 running 迁移到 completed 模拟固化，
     面板关且 `homeVisible === false` 时点亮；`homeVisible === true` 时不点亮。

### 验收

```bash
npx vitest run src/composables/useAgentPresence.test.ts \
  src/composables/useChatPersistence.test.ts \
  src/stores/ssh-ai.test.ts
npm run typecheck
npm run build
```

手工（Electron）：

1. 首页标签是星芒；Enter/Space 能回到首页。
2. 标签条最右侧两枚按钮在终端标签很多、横向滚动时仍固定可见。
3. 运行/待审批时首页点与顶栏徽章同时变化，`title` 带数量。
4. 「新建会话」在运行中禁用；空闲时点击后下一次发送进入新会话。

---

## S2 会话状态与历史抽屉

### 目标

- 首页有一个真正的消息视口外壳：历史抽屉只盖消息区，不盖输入区。
- 历史列表从 `HistoryPanel` 抽成共用组件，侧栏历史与任务台抽屉只有一份行样式。
- 侧栏「新建对话」改走 `beginNewTask()`，删除 500ms 人工锁与转圈。
- 运行中禁止新建/切换/删除会话，首页与侧栏同口径。

### 改动文件

| 类型 | 文件 | 内容 |
|---|---|---|
| 新增 | `src/components/chat/HistoryConversationList.vue` | 分组历史纯列表（groups/currentConvId/isEmpty/busy） |
| 修改 | `src/components/chat/HistoryPanel.vue` | 只保留覆盖层外壳，列表改为内嵌共用组件 |
| 新增 | `src/components/home/HomeHistoryDrawer.vue` | 右侧抽屉：遮罩、焦点、Esc、Tab 循环 |
| 新增 | `src/components/home/HomeWorkspace.vue` | **本片先搭外壳**：`.home-main` + `.home-scroll` + `.composer-wrap` |
| 修改 | `src/views/ssh/SshView.vue` | 首页内容换成 `HomeWorkspace`；抽屉状态下发 |
| 修改 | `src/components/chat/SideChatPanel.vue` | 删除 `creating`；新建按钮运行中禁用；历史 busy |
| 修改 | `src/components/ssh/SshAiPanel.vue` | `handleNewChat` 改 `beginNewTask()`；删除人工锁 |
| 修改 | `src/stores/ssh-ai.test.ts` | 若 S1 未覆盖，补运行中/清理用例 |

### 实施步骤

1. **抽取 `HistoryConversationList.vue`**

   - 从 `HistoryPanel.vue` 原样搬走 `.history-group` / `.history-panel-item*` 的结构与样式。
   - Props：`groups: ConvGroup[]`、`currentConvId: string | null`、`isEmpty: boolean`、
     `busy?: boolean`。Emits：`switch: [id]`、`delete: [id]`。
   - `busy` 为 true 时：会话主按钮与删除按钮都 `disabled`，行 `title="运行中不可切换会话"`
     / `"运行中不可删除会话"`；不做灰字说明段。
   - 空态文案统一为「还没有会话」。
   - `HistoryPanel.vue` 保留 `.history-panel-overlay` / 头部 / 关闭按钮，列表位置内嵌
     `<HistoryConversationList ... :busy="busy" @switch @delete />`，新增 `busy` prop 透传。

2. **新增 `HomeHistoryDrawer.vue`**

   - 结构：`.drawer-mask` + `<aside id="home-history-drawer" role="dialog" aria-modal="false"
     aria-label="历史记录">`，头部「历史记录」+ 关闭按钮，列表复用共用组件。
   - Props：`groups`、`currentConvId`、`isEmpty`、`busy`。Emits：`close`、`switch`、`delete`。
   - 关闭路径：再次点历史按钮（父层控制）、关闭按钮、Esc、点遮罩。
   - 打开时焦点进入抽屉（关闭按钮或 `aside`），Esc 关闭后焦点回到历史按钮（由
     `SshView` 通过 `ref` 或 `nextTick` 聚焦）。Tab 在抽屉内循环。
   - 样式：绝对定位在 `.home-main` 内；`width: min(320px, 80vw)`；左侧描边 + `--shadow-lg`；
     遮罩只盖消息区。全部使用现有令牌。

3. **`HomeWorkspace.vue` 外壳（本片的最小形态）**

   - 模板骨架（后续两片只替换内容，不改骨架）：

     ```html
     <div class="home-workspace">
       <div class="home-main">
         <div ref="scrollRef" class="home-scroll">
           <div class="home-column">…本片仍放旧首页内容…</div>
         </div>
         <HomeHistoryDrawer v-if="historyOpen" … />
       </div>
       <div class="composer-wrap">
         <HomeCommandCard /> <!-- S3 换成 HomeComposer -->
       </div>
     </div>
     ```

   - 本片 `.home-column` 里仍放 `HomeTargetTitle`、`HomeApprovalQueue`、`RunView`；
     `HomeApprovalQueue` 的 `open-run` 由 `HomeWorkspace` 内部 `RunView` ref 处理，
     `bulk` 事件冒泡给 `SshView`。
   - `.home-main { position: relative; flex: 1; min-height: 0 }`；
     `.home-scroll { height: 100%; overflow-y: auto }`；
     `.composer-wrap` 是 flex 兄弟节点，不被抽屉遮罩覆盖。
   - `HomeCommandCard` 移到底部后其菜单默认朝下会出屏：本片在 `HomeWorkspace` 里用
     `:deep(.hcc-menu) { top: auto; bottom: calc(100% + 6px) }` 临时改为朝上；
     S3 在 `HomeComposer` 内固化这条规则并删除这段 deep 覆盖。
   - `SshView` 首页块改为：

     ```html
     <HomeWorkspace
       :history-open="homeHistoryOpen"
       @close-history="homeHistoryOpen = false"
       @bulk="showBulk = true"
     />
     ```

   - 删除 `SshView` 里 `HomeTargetTitle` / `HomeCommandCard` / `HomeApprovalQueue` /
     `RunView` 的静态 import 与本片不再使用的 `runVisible`/`runRef`/`revealRun`（若 S4
     仍需这些引用，可留到 S4 一并删；以实际编译结果为准）。旧 `.home-pane` 样式在
     确认无引用后删除。

4. **侧栏新建与运行守卫**

   - `SideChatPanel.vue`：
     - 删除 `creating` prop 与模板里的 spinner 分支；新建按钮
       `:disabled="agentRunning"`，`title` 为「运行中不可新建会话」。
     - `HistoryPanel` 传 `:busy="agentRunning"`；`handleSwitch` / `handleDelete` 在
       `props.agentRunning` 时直接返回（列表已禁用，这里兜底）。
   - `SshAiPanel.vue`：
     - 删除 `creating` 状态、500ms 人工锁、`onUnmounted` 里的重置。
     - `handleNewChat` 改为：

       ```ts
       if (agentStore.isRunning) { toast.info('AI 正在运行中，请等待完成'); return }
       store.beginNewTask()
       ```

     - `handleSwitchConv` / `handleDeleteConv` 在运行中提示并返回；删除会话的二次
       `ConfirmDialog` 逻辑保持不变。
   - 运行中「目标 chip 可改」「抽屉可打开」不受守卫影响。

5. **验收走查**

   - 打开抽屉：只盖消息区，底部输入区仍可见、可输入。
   - 运行中：抽屉可开，所有行与删除禁用；侧栏新建禁用；运行结束后恢复。
   - 侧栏「新建对话」不再产生空历史条目；首条消息发出后才出现条目。

### 验收

```bash
npx vitest run src/composables/useChatPersistence.test.ts src/stores/ssh-ai.test.ts
npm run typecheck
npm run build
```

手工：见上；另确认侧栏历史覆盖层与任务台抽屉的分组、当前项高亮、删除确认一致。

---

## S3 Composer

### 目标

- 用 `HomeComposer` 取代 `HomeCommandCard`：既有 ＋ 菜单/审批档/模型/chips 原样迁移，
  新增目标主机 chip、菜单与「全部 N 台」入口，运行中发送键变停止键。
- 输入区固定在任务台底部，菜单朝上展开，不被裁切。
- 删除 `HomeCommandCard`，不留第二套下达入口。

### 改动文件

| 类型 | 文件 | 内容 |
|---|---|---|
| 新增 | `src/components/home/HomeComposer.vue` | 由 `HomeCommandCard` 迁移 + 目标 chip + 停止态 |
| 修改 | `src/components/home/HomeWorkspace.vue` | composer 槽换成 `HomeComposer`，删除 deep 菜单覆盖 |
| 删除 | `src/components/home/HomeCommandCard.vue` | 迁移完成后删除 |
| 修改 | `src/views/ssh/SshView.vue` | 删除 `HomeCommandCard` import（若尚未删） |

### 实施步骤

1. **迁移既有能力**

   - 以 `HomeCommandCard.vue` 为底稿新建 `HomeComposer.vue`，保留：
     - `useHomeCommand()` 的 `draft/canSend/submit/attachFiles/removeAttachment/uploading`；
     - ＋ 菜单（目标/计划/终端现场/本地文件/远端文件/技能折叠）；
     - 审批档菜单（`APPROVAL_OPTIONS` / `resolveRunModes` / `PERM_SHIELD` / `PERM_MARKS`）；
     - 模型选择器、姿态/技能/终端现场/附件 chips、上传 input；
     - 发送快捷键（`chat.sendShortcut`）、textarea 自动增高、外点关闭。
   - 宽度改为 `max-width: 820px`；textarea placeholder 设为 `输入指令…`；
     `aria-label="输入指令"`。

2. **目标主机 chip**

   - `const { targetLabel, options, pick, canBulkDispatch } = useHomeTarget()`。
   - 底栏顺序：`＋` → 目标 chip → 分隔线 → 审批档 → chips → 弹簧 → 模型 → 发送/停止。
   - 目标 chip 显示 `targetLabel`，图标用服务器/终端样式（与预览一致），`aria-haspopup="menu"`。
   - 菜单项：`label` 主文案 + `hint` 副文案，当前项显示 ✓；点击 `pick(sessionId)` 后关闭菜单，
     **不切走首页、不聚焦终端**。
   - 菜单底部 `canBulkDispatch` 时出现「全部 N 台」，点击 `emit('bulk')` 并关闭菜单；
     `HomeWorkspace` 把 `bulk` 冒泡给 `SshView`（沿用 `showBulk`）。
   - 无会话时显示占位「服务器」，菜单只有空提示；`pick` 只接受已打开会话（`useHomeTarget` 已保证）。

3. **发送 / 停止**

   - `agent.isRunning` 时发送键替换为停止键（方块图标，`aria-label="停止"`），点击
     `agent.abort()`；输入框仍可编辑。
   - 空闲时发送键由 `canSend` 控制；空文本且无附件禁用。
   - 运行中 Enter：走 `submit()` 的既有早退 toast「AI 正在运行中，请等待完成」；
     不排队、不清草稿。
   - 发送成功后的清理沿用 `useHomeCommand.submit()`；被拒时草稿/附件保留（既有语义）。

4. **菜单方向与裁切**

   - `.composer-wrap` 不设 `overflow: hidden`；所有菜单（＋、目标、审批、模型）统一
     `top: auto; bottom: calc(100% + 6px)` 朝上展开，`z-index` 高于消息区。
   - `.home-scroll` 的滚动容器与菜单不在同一层，菜单不会被消息区滚动裁掉。
   - 960px 宽、960px 高窗口下，菜单最长 260px 仍完整可见；不允许出现横向滚动。

5. **切换与删除**

   - `HomeWorkspace` 用 `<HomeComposer @bulk="emit('bulk')" />` 替换 `HomeCommandCard`。
   - 删除 `HomeWorkspace` 里临时的 `:deep(.hcc-menu)` 覆盖与 `HomeCommandCard` import。
   - 删除 `HomeCommandCard.vue`；全仓搜索确认无引用。

6. **验证**

   - `useHomeCommand` / `useHomeTarget` 测试不改、必须全绿。
   - 手工：目标切换只改 `activeSessionId`；两台以上出现「全部 N 台」并打开既有批量弹窗；
     运行中停止键出现、发送键隐藏；菜单朝上不裁切；＋/审批/模型/chips 行为与旧卡一致。

### 验收

```bash
npx vitest run src/composables/useHomeCommand.test.ts src/composables/useHomeTarget.test.ts
npm run typecheck
npm run build
```

手工：空态与有消息时输入区位置一致；960px 无横向滚动；浅色/深色/手记主题下菜单与 chip 可读。

---

## S4 任务台主流程与内联过程

### 目标

- 首页成为真正的对话工作台：空态问句 / 会话标题 + 消息流 + 常驻输入区。
- 侧栏与首页共用同一份 live 合并逻辑（`display-messages`）。
- 执行过程内联在助手消息里：运行中展开、完成自动折叠、审批卡就地、多条批量组头。
- 中止/失败/人工/迭代上限有明确终态文案；历史重载后仍能区分「已中止」。
- 删除 `HomeTargetTitle`、`HomeApprovalQueue`、`RunView` 及其接线。

### 改动文件

| 类型 | 文件 | 内容 |
|---|---|---|
| 新增 | `src/utils/display-messages.ts` | 唯一的 live 合并纯函数 |
| 新增 | `src/utils/display-messages.test.ts` | 只合并最后一条 live temp 消息；其余引用不变 |
| 新增 | `src/utils/process-status.ts` | 终态文案/色调、工具行状态、命令摘要 |
| 新增 | `src/utils/process-status.test.ts` | 中止/失败/human_help/max_reached/完成映射 |
| 修改 | `src/api/sse.ts` | `AgentTraceSnapshot` 新增可选 `aborted?: boolean` |
| 修改 | `src/stores/agent.ts` | `snapshot()` 写入 `aborted` |
| 修改 | `src/components/agent/AgentTraceInline.vue` | 内联过程、审批流、批量组头、trace 复制、归因 |
| 修改 | `src/components/chat/MessageItem.vue` | 新增 `inlineApprovals` / `processMeta` 可选 props 并透传 |
| 修改 | `src/components/chat/SideChatPanel.vue` | `displayMessages` 改用共享纯函数 |
| 修改 | `src/components/home/HomeWorkspace.vue` | 标题/空态、消息流、审批与归因、自动滚动 |
| 修改 | `src/views/ssh/SshView.vue` | 删除旧首页组件 import 与 run 接线 |
| 删除 | `src/components/home/HomeTargetTitle.vue` | 目标能力已并入 Composer |
| 删除 | `src/components/home/HomeApprovalQueue.vue` | 批量能力已并入内联过程 |
| 删除 | `src/components/agent/RunView.vue` | 过程已内联进消息 |

### 实施步骤

1. **`display-messages.ts`**

   ```ts
   export function isLiveAgentRun(
     messages: readonly BaseMessage[], idx: number, running: boolean,
   ): boolean

   export interface LiveAgentView {
     running: boolean
     content: string
     reasoning: string
     trace: AgentTraceSnapshot
   }

   export function buildDisplayMessages(
     messages: readonly BaseMessage[], live: LiveAgentView,
   ): BaseMessage[]
   ```

   - 非运行中：原数组原引用返回。
   - 只替换最后一条（`idx === messages.length - 1`、assistant、id 以 `temp-agent-` 开头）：
     `content = live.content || m.content || m.streamingContent || ''`、
     `streamingContent = undefined`、`reasoning = live.reasoning || ''`、
     `streamingReasoning = undefined`、
     `agentTrace = { ...live.trace, lastRunId: '' }`。
   - 其余消息原引用，避免 O(n) 重渲染。
   - `SideChatPanel` 的 `displayMessages` computed 改为调用它；传入
     `agentStore.snapshot()`。

2. **`process-status.ts`**

   - `processStatusLabel(trace, isLive)`：live → `{ text:'执行中', tone:'run' }`；
     `trace.aborted` → `已中止`；`failed` → `失败`；`human_help` → `需要人工`；
     `max_reached` → `达到迭代上限`；`completed` → `已完成`；未知 → `已完成`。
   - `toolState(entry)`：`result === undefined → 'running'`；`success === false → 'failed'`；
     否则 `'done'`。
   - `headline(entry)`：从 `RunView.vue:128-139` 原样迁移（`args.command` 优先，其余
     取前三个参数摘要，截断留 `…`）。
   - 组件只消费这些纯函数，不再在模板里写映射分支。

3. **`AgentTraceSnapshot.aborted`**

   - `src/api/sse.ts` 增加可选字段与注释（旧数据没有该字段时按 failed 展示）。
   - `agent.snapshot()` 写入 `aborted: aborted.value || undefined`。
   - `finalizeAgentMessage` 不需要改逻辑：它整体拷贝 snapshot。
   - 旧 localStorage/后端历史没有该字段时，`processStatusLabel` 回落到 failed，不误报完成。

4. **`AgentTraceInline.vue` 重写**

   - Props：`trace: AgentTraceSnapshot`、`isLive?: boolean`、
     `approvals?: PermissionRequest[]`、`meta?: ProcessMeta`。
     `ProcessMeta` 在组件内或 `process-status.ts` 定义：
     `{ modelName?: string; permissionLabel?: string; targetLabel?: string; runId?: string; durationMs?: number }`。
   - 数据：`model = buildTraceModel(trace.toolCalls)`；
     `stream = mergeStream(model, approvals ?? [])`。
   - 折叠：`expanded = ref(isLive)`；`watch(isLive, v => { if (!v) expanded.value = false })`。
     完成后用户再手动展开，不会被后续重渲染重置。
   - 折叠摘要：`已执行 {model.steps.length} 步` + 有耗时则 `· X.Xs`；
     运行中不折叠。
   - 运行中头部：`执行中 · 第 {activeStepIndex + 1} 步`；无工具调用时「思考中」；
     计时读 `agent.runStartedAt` 的本地 ticker（从 RunView 迁移），收尾后读 `trace.duration`。
   - 右侧：`approvals.length > 0` 显示「N 条待放行」；`meta?.runId || trace.lastRunId`
     显示整串 trace 号，点击复制并 toast「已复制，粘到运维审计的 trace 框」。
   - 过程体：
     - 按 `stream` 顺序渲染；工具项所属批次变化时输出组头 `第 n 步 · X.Xs`
       （`n` 取 `step.iteration + 1`，`step.iteration === null` 显示「未编号」；
       耗时取 `step.durationMs`，`measuredCount < eventIndexes.length` 时加「部分计时」灰字）。
     - 工具行：状态（执行中/成功/失败/未回写）、工具名、`headline` 命令（monospace）、
       单项耗时；输出默认前 3 行 + 「展开输出（N 行）」，展开后 `pre` 内部滚动。
     - 审批项：`ApprovalCard :req`；未决 ≥ 2 时在**第一条审批**前插组头
       「N 条待你放行」+「全部拒绝」「全部批准本次」。
     - 组头结算走 `useApprovalActions().settleAll('deny' | 'approve')`：
       结算中禁用；`!agent.isRunning` 时「全部批准本次」禁用（once 不可达）；
       不提供 session/always 批量授权。
     - 运行中无任何工具且无审批：保留现有「思考中」三点。
     - 收尾后无工具：`这次运行没有产生工具调用`。
   - 底部归因：仅当 `meta` 存在时渲染模型名/审批档/目标主机/耗时；模型在列表里查不到
     时不渲染内部 id（沿用 RunView 的口径）。
   - 不渲染 reasoning（外层 `MessageItem` 的 `ThinkingBlock` 已承担）。
   - 保留文件名与 `MessageItem` 接入点；样式全部用现有令牌，`prefers-reduced-motion`
     下停用呼吸/三点动画。

5. **`MessageItem.vue`**

   - 新增可选 props：`inlineApprovals?: PermissionRequest[]`、`processMeta?: ProcessMeta`。
   - 传给 `AgentTraceInline`：`:approvals="message.role === 'assistant' ? inlineApprovals : undefined"`、
     `:meta="processMeta"`。其余调用方不传，行为不变。

6. **`HomeWorkspace.vue` 完整形态**

   - 标题：
     - `messages.length === 0` → `今天想让助手替你做什么？`；
     - 否则取当前会话标题（`conversations.find(c => c.id === currentConvId)?.title`）。
     - 标题随消息流滚动，不做固定头部。
   - 消息：
     - `displayMessages = buildDisplayMessages(sshAi.messages, { running, content, reasoning, trace: agent.snapshot() })`；
     - `<MessageItem v-for ... :is-last="idx === displayMessages.length - 1"
       :is-streaming="isLiveAt(idx)" :inline-approvals="isLiveAt(idx) ? agent.pendingPermissions : undefined"
       :process-meta="metaFor(msg, isLiveAt(idx))" />`。
     - `metaFor` 只在 `isLive` 或 `msg.agentTrace.lastRunId === agent.lastRunId` 时返回：
       modelName（models store 查名）、permissionLabel（`PERMISSION_LABELS[agent.lastPermissionMode]`）、
       targetLabel（`approvalTargetLabel`）、runId（`agent.lastRunId`）、durationMs。
   - 空态：不渲染消息列表；问句居中；目标只在 Composer chip 出现。
   - 自动滚动：`chat.autoScroll` 打开时，watch 消息数、`agent.content`、`agent.reasoning`、
     `agent.pendingPermissions.length`，`nextTick` 后滚到底；关闭时不强制。
   - 抽屉与 Composer 结构保持 S2；本片不再改骨架。

7. **`SshView` 清理**

   - 删除 `HomeTargetTitle` / `HomeApprovalQueue` / `RunView` 的 import 与异步注册。
   - 删除 `runVisible` / `runRef` / `revealRun`。
   - 首页块只留 `<HomeWorkspace ... />`；`showBulk` 仍由 `SshView` 持有。
   - 删除 `.home-pane` 等不再命中的样式；确认 `app.has-background` 规则不再引用已删类名。
   - 全仓搜索 `HomeTargetTitle|HomeApprovalQueue|RunView|revealRun|runVisible|runRef`，
     清零后删除对应文件。

8. **测试**

   - `display-messages.test.ts`：
     - 非运行中返回原数组/原引用；
     - 只合并最后一条 live temp 消息，其余引用不变；
     - `lastRunId` 被清空、content/reasoning 优先级正确；
     - 最后一条不是 temp 助手时不合并。
   - `process-status.test.ts`：
     - 中止优先于 failed 显示「已中止」；
     - failed / human_help / max_reached / completed / live；
     - `toolState` 的「未回填 ≠ 失败」；
     - `headline` 命令优先与截断。
   - `run-trace.test.ts` 不改，必须继续全绿。

### 验收

```bash
npx vitest run src/utils/display-messages.test.ts \
  src/utils/process-status.test.ts \
  src/utils/run-trace.test.ts
npm run typecheck
npm test
npm run build
```

手工（Electron）：

1. 空态问句；发送第一条 → 用户消息 + 助手消息；历史抽屉出现该会话。
2. 运行中：过程展开、工具行状态变化、审批卡按时间插入、2 条以上出现组头；
   批量拒绝/批准可用；首页点与顶栏徽章为待审批。
3. 完成：过程折叠为「已执行 N 步 · X.Xs」；展开可看输出、trace 复制；再展开状态保持。
4. 中止：过程头部「已中止」，未回填工具行显示「运行结束前未回写结果」，不再显示进行中。
5. 历史切换：旧消息保留内联过程；没有审批卡；没有伪造的模型/审批/目标归因。
6. 侧栏与首页同一会话：发送、草稿、技能/附件、消息一致；侧栏仍不出现审批卡。

---

## S5 收尾：侧栏待批提示、清理、无障碍与全量验证

### 目标

- 侧栏在有待审批时给出「N 条待你放行 · 去任务台」提示（不提供批准按钮）。
- 删除所有旧入口/旧文件/旧注释，跑通全量验证与手工走查。
- 响应式、焦点、键盘与减少动效全部落地。

### 改动文件

| 类型 | 文件 | 内容 |
|---|---|---|
| 修改 | `src/components/agent/AgentRunPanel.vue` | 顶部待批提示条，emit `open-home` |
| 修改 | `src/components/ssh/SshAiPanel.vue` | 转发 `open-home`；确认 `creating` 清理干净 |
| 修改 | `src/views/ssh/SshView.vue` | `@open-home` → 关侧栏 + `goHome()`；历史按钮聚焦 |
| 修改 | `src/composables/useSshNavigation.ts` | 更新/删除关于 RunView 的过期注释（无代码改动） |
| 修改 | 其余残留引用 | 全仓搜索旧组件名与 `creating` |
| 修改 | `src/styles/variables.css` | 仅在确需新增时间线/抽屉令牌时补；优先复用现有 |

### 实施步骤

1. **侧栏待批提示**

   - `agent.hasRunNotice` 增加 `pendingPermissions.length > 0`（现在有区块渲染了，
     不再空挂面板）；同步更新该 computed 的注释。
   - `AgentRunPanel` 顶部（`hasRunNotice` 块内最前）渲染：

     ```html
     <button v-if="agentStore.pendingPermissions.length"
       class="ap-card ap-pending" type="button" @click="emit('open-home')">
       {{ agentStore.pendingPermissions.length }} 条待你放行 · 去任务台
     </button>
     ```

     只导航，不渲染批准/拒绝按钮；样式用 `--status-warn-*`，不写裸色。
   - `AgentRunPanel` 增加 `defineEmits<{ 'open-home': [] }>()`。
   - `SshAiPanel` 的 `<AgentRunPanel @open-home="$emit('open-home')" />`，`defineEmits` 增加
     `open-home`。
   - `SshView`：`<SshAiPanel ... @open-home="onOpenHome" />`；
     `function onOpenHome() { sshAiStore.closeAiPanel(); goHome() }`。
   - 运行中侧栏仍可看过程/发送（发送被拒会提示），只有审批决策被引导回任务台。

2. **无障碍与焦点**

   - 首页标签：`role="tab"`、`aria-selected`、`tabindex=0`、Enter/Space。
   - 状态点/徽章 `aria-hidden`；语义由按钮 `title` / `aria-label` 承担。
   - 两枚标签条按钮有 `aria-label`；历史按钮 `aria-expanded` 与 `aria-controls`。
   - 抽屉：打开时焦点进入；Esc 关闭并回到历史按钮；Tab 循环；`aria-modal="false"`。
   - `prefers-reduced-motion: reduce` 下关闭状态点呼吸、思考三点、过程运行动画。
   - 所有可点行/按钮保持可见的 `:focus-visible` 样式（沿用现有 global 规则，不覆盖）。

3. **响应式**

   - 消息列与 Composer 共用 `max-width: 820px`；外层左右 padding 至少 16px。
   - 960px 宽：`min(320px, 80vw)` 抽屉不挤压消息列到不可读；无横向滚动。
   - 空态/有消息/运行中三种高度下，Composer 固定可见，消息区独立滚动。
   - 抽屉遮罩只覆盖 `.home-main`；Composer 的菜单朝上展开不被抽屉遮住。

4. **清理**

   - 全仓搜索并删除：`HomeTargetTitle`、`HomeCommandCard`、`HomeApprovalQueue`、
     `RunView`、`revealRun`、`runVisible`、`runRef`、`creating`（SideChatPanel 相关）。
   - 删除对应文件、import、样式、注释；`useSshNavigation.ts` 中「运行视图/run 标签」
     的过期说明改为当前事实（首页内联过程），不留误导性注释。
   - 检查 `SshView` 的 `app.has-background`、窄屏规则是否仍引用已删类名。
   - 不删除 `HistoryPanel` 覆盖层：侧栏仍使用它（spec 故事 40）。
   - 不碰 `backend/`；审批安全链不在本片范围。

5. **预览对照**

   - 用浏览器打开 `.workbuddy/design/home-agent-workbench.html`，逐一对照四态：
     `empty` / `running` / `done` / `history`。
   - 实现与预览冲突时以规格为准，并回改预览（预览是版面口径，不是像素验收稿）。

6. **全量验证**

   ```bash
   npm run typecheck
   npm test
   npm run build
   ```

   手工走查（规格 §6.3 全部 7 项）：

   1. 空态：AI 图标、两枚按钮、问句、目标 chip；首条发送 → 消息出现、历史多一条。
   2. 运行中：过程展开、工具行状态、审批卡、批量组头、首页点/顶栏徽章、停止键；
      中止后「已中止」。
   3. 完成：自动折叠、步数/时长正确、展开输出、trace 复制、手动展开保持。
   4. 历史抽屉：只盖消息区、输入区可用、切换/删除确认、运行中禁用。
   5. 侧栏：发送/切换/草稿同步；待审批出现「去任务台」；侧栏无审批卡。
   6. 960px：无横向滚动；三种高度正常。
   7. 主题：深色/浅色/手记至少三套，无裸色断层、无不可读对比。

### 验收

- `npm run typecheck` 双 config 0 错误。
- `npm test` 全绿；新增用例计入，记录最终数量。
- `npm run build` 成功。
- 手工 7 项全过。

---

## 4. 新增/修改测试清单（汇总）

| 测试文件 | 覆盖 | 片 |
|---|---|---|
| `src/composables/useAgentPresence.test.ts` | 优先级、pendingCount | S1 |
| `src/composables/useChatPersistence.test.ts` | `beginNewTask` 不增删/不落盘 | S1/S2 |
| `src/stores/ssh-ai.test.ts` | `beginNewTask` 清理/保留、运行中拒绝、`homeVisible` 未读 | S1/S2 |
| `src/utils/display-messages.test.ts` | live 合并边界与引用稳定 | S4 |
| `src/utils/process-status.test.ts` | 终态文案、工具状态、命令摘要 | S4 |
| `src/utils/run-trace.test.ts` | 继续全绿，不改语义 | S4 |
| `src/composables/useHomeCommand.test.ts` | 继续全绿 | S3 |
| `src/composables/useHomeTarget.test.ts` | 继续全绿 | S3 |
| `src/composables/useApprovalActions.test.ts` | 继续全绿（组头复用） | S4 |

UI 无 jsdom，组件交互不做挂载单测；以 Electron 手工走查为准。

## 5. 风险与回退点

| 风险 | 处理 |
|---|---|
| 并行会话改动被误 stage | 每片只用 `git add <明确文件>`；提交前 `git status` 复核 |
| 底部 Composer 菜单被裁 | S2 临时 deep 覆盖朝上；S3 固化到组件；`.composer-wrap` 不设 overflow hidden |
| 抽屉遮住输入区 | 抽屉绝对定位在 `.home-main`；`.composer-wrap` 是兄弟节点；用手工场景 4 验收 |
| 运行中切会话导致审批失去落点 | S2 的禁用守卫；`beginNewTask` store 兜底返回 false |
| 历史重载把中止显示成失败 | `trace.aborted` 可选字段 + `process-status` 优先判定；旧数据回落 failed |
| 侧栏与首页消息渲染漂移 | `display-messages` 唯一实现；S4 同时替换两处 |
| 归因伪造 | 只有 `msg.agentTrace.lastRunId === agent.lastRunId` 或 live 时传 meta |
| 批量批准越权 | 组头只调 `settleAll`，其内部固定 `scope='once'`；不新增 session/always |
| 删除旧文件后残留 import | S4/S5 全仓搜索 + `npm run typecheck` + `npm run build` |
