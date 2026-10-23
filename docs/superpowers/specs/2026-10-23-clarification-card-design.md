# 意图澄清卡去弹窗化 —— 并入审批决策面设计（2026-10-23）

## 背景与目标

意图澄清（ClarifyingWizard）目前是 Teleport 到 body 的居中模态弹窗：黑色遮罩 +
blur、`aria-modal` 对话框语义、全局 Esc 监听触发"跳过"。审批/交付早已收敛为输入区
上方的非模态决策面（ApprovalDock + ApprovalCard/DeliveryCard，共享样式
`src/styles/approval-card.css` 的 `.ap-*` 卡骨架），两套"要你拍板"的界面风格与位置
语义不一致。

目标：**澄清卡取消弹窗形态，改为与审核卡片同风格的决策卡，挂进 ApprovalDock**。
纯前端呈现层重构——store 状态机、SSE 事件契约、answer/skip 端点零改动。

## 已确认的决策

| 决策点 | 结论 |
| --- | --- |
| 卡片位置 | 挂进 ApprovalDock，渲染在权限队列之前（run 阻塞在澄清上，先答澄清才有后续审批） |
| Esc 行为 | 删除全局 Esc→跳过监听（非模态卡下全局按键是误触炸弹；与审批卡对齐，裁决只走按钮） |
| 实现路径 | 方案 A「组件搬移 + 样式归队」；否决方案 B（澄清混入 permissionRequests 队列，污染批量结算/去重口径）与方案 C（只去遮罩不动位置，风格只做到六七成） |
| 卡片顺序 | 澄清卡排在审批卡上方 |
| 新请求提醒 | 已存在（agent.ts L851 在「新建向导」分支末尾无条件 `warning`，就地刷新分支已先 break 不弹）——不新增，仅把文案对齐审批句式改为「AI 需要你澄清意图，回首页在输入框上方作答」 |

## 设计

### 1. ClarifyingWizard.vue 原地改造（组件名保留）

**删除**（弹窗专属物）：
- `Teleport to="body"`、`.clarify-overlay` 遮罩、`.clarify-dialog` 居中模态、
  `clarify-fade` 过渡、`aria-modal` 对话框语义；
- 全局 `keydown` 监听：`handleKeydown`、`watch(visible)` 挂/摘监听、`onUnmounted` 清理；
- `visible` ref 与 `watch(wizard)` 的显隐两态——非模态后直接 `v-if="wizard"` 渲染；
  `localStep`/`selectedChoice`/`customText` 的重置改在 watch 里对 wizard 非空响应
  （保留 `immediate: true`——"组件晚于事件创建"的挂载洞同样适用于卡）；
- `nextBtnRef` 抢焦点逻辑（不再模态，不抢焦点）。

**卡结构**对齐审批卡家族：

```html
<div class="ap-card ap-clarify">
  <div class="ap-head">           <!-- 问号图标 + 标题「AI 需要你澄清意图」
                                       + 进度 n / total（徽章位，沿用现 displayStep 计算） -->
  <div class="clarify-context">   <!-- 保留：命令预览 + 触发原因（信息有价值） -->
  <div class="ap-body">           <!-- 当前问题 + 选项列表（A/B/C/D 徽章 + 自定义输入） -->
  <div class="ap-actions">        <!-- 「完成 ↵」.ap-btn-grant 绿 + 「跳过」.ap-btn-deny 靠右否决位 -->
</div>
```

**样式**：卡骨架、按钮、说明行全部走共享 `approval-card.css` 的 `.ap-*`；私有样式只
保留选项列表（`.clarify-option`/`.badge`/`.custom-input`）与 context 条这些家族里没有
的元素。`.ap-clarify` 与 `.ap-perm` 同限高 `max-height: min(38vh, 340px)`，正文
`.ap-body` 内滚、头/动作栏 `flex: none` 常驻——满足卡骨架既有守卫约束。不引入裸色值，
全部 CSS 变量令牌，浅色/手记主题自动跟随。

**逻辑不动**：多步推进（`localStep`/`stepIndex`/`currentQuestion` 回落单步旧事件的
兼容）、`canAdvance` 自定义空值守卫、`recordCurrentAnswer`、run_id 匹配校验（P0-6）、
提交/跳过成功后 `clearClarification()` 立即关闭（P0-2）、`submitting` 期间按钮禁用
与"提交中…"文案。卡内自定义输入框的 `@keydown.enter="handleAdvance"` 保留（输入框
行为，非全局监听）。

### 2. 挂载搬迁

- `AgentRunPanel.vue`：删 `<ClarifyingWizard />` 与 import；头注释中"澄清向导自带
  visible 门控 + Teleport，不受整块开关影响"的条目同步删除改写，不留误导。
- `ApprovalDock.vue`：新增渲染位——`agent.clarificationWizard` 非空时在 `.ad-body`
  权限队列之前渲染澄清卡；dock 根 `v-if` 扩为
  `pending.length || deliveries.length || agent.clarificationWizard`。头区 `ad-title`
  计数逻辑不变（只统计权限卡）。
- `HomeWorkspace.vue`：更新"宿主不要再抄条件"注释块为新事实——澄清卡随 ApprovalDock
  常驻输入区上方，不再依赖 AgentRunPanel 挂载链路；原"消息为空时空态把向导带走"的
  洞随 dock 本就不在空态分支而消失。
- `agent.ts` store：`clarification_needed` 的提醒 toast 已存在（L851，位于新建向导赋值
  之后、就地刷新分支 break 之前），无需新增；只把文案从「AI 需要对不明确的操作进行
  确认」改为与审批同句式的「AI 需要你澄清意图，回首页在输入框上方作答」。其余状态机
  逻辑零改动。

### 3. 错误处理

- 提交/跳过失败：toast 报错 + 卡保留可重试（现状行为，位置随卡进 dock）。
- run 终态/四条出口清空 `clarificationWizard`：现有逻辑不动，卡随 dock `v-if` 自然
  消失；模态专属的"遮罩残留"失效面不复存在。

### 4. 测试

- 新增守卫（仿 `approval-trace-boundary.test.ts` 源码/CSS 文本断言手法，扩展进该文件）：
  1. `ApprovalDock.vue` 含澄清卡引用与 `clarificationWizard` 判据；
  2. `AgentRunPanel.vue` 不再含 `ClarifyingWizard`（防双渲染位/防搬回）；
  3. `ClarifyingWizard.vue` 不含 `Teleport`、`clarify-overlay`、`aria-modal`、`Escape`
     （防弹窗回潮）；
  4. `.ap-clarify` 限高 `min(38vh, 340px)` 存在，头/动作栏 `flex: none`。
- 保留不动：`agent.test.ts` 的 clarification 状态机测试（应全绿）。
- 验证：`npm run test` + `npm run typecheck`；后端零改动，无需跑 pytest。

## 涉及文件

| 文件 | 改动 |
| --- | --- |
| `src/components/agent/ClarifyingWizard.vue` | 去弹窗、改 `.ap-card` 卡形态 |
| `src/components/agent/ApprovalDock.vue` | 挂载澄清卡 + `v-if` 扩展 |
| `src/components/agent/AgentRunPanel.vue` | 摘除澄清卡挂载与相关注释 |
| `src/components/home/HomeWorkspace.vue` | 注释更新（无结构改动） |
| `src/stores/agent.ts` | `clarification_needed` 现有 toast 文案对齐审批句式（无新增逻辑） |
| `src/components/agent/approval-trace-boundary.test.ts` | 新增 4 条守卫断言 |

## 非目标

- 不改后端 ClarificationInterceptor、SSE 事件与 answer/skip 端点契约。
- 不动 `clarificationWizard` 状态机与多步向导的前端推进模型。
- 不碰审批卡（ApprovalCard/DeliveryCard）本身。
