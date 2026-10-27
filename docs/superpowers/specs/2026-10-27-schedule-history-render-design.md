# 定时任务/剧本执行历史弹窗：markdown 渲染 + 单行紧凑折叠（共享展示组件）

日期：2026-10-27。标签：`spec 2026-10-27-schedule-history-render`（实现时代码注释与守卫测试用此标签串联）。
需求：定时任务「执行历史」弹窗现状粗糙——summary 是原始 markdown 却用 pre-wrap 生肉直出、
成功/失败一视同仁平铺、请求失败静默显示成「暂无执行记录」。经浏览器比选（用户选定方案 A
单行紧凑）与范围澄清，差异边界如下。

## 0. 已确认的用户决定

- **范围只做了「markdown 渲染 + 折叠」**：轨迹跳转、弹窗内自动刷新、筛选/分页、焦点圈定
  本轮明确不做（run 号保持纯文本展示）。
- **RunbookPanel（运维剧本）历史弹窗同步对齐**：两处结构几乎同构，用户选择抽共享组件
  一次设计两处复用，而非只改定时任务。
- 折叠形态三选一（浏览器并排）→ 用户选 **A 单行紧凑**：折叠态 = 元信息行 + 单行预览句，
  失败行红条默认展开。
- 实现结构三选一 → 用户选 **方案 1 纯展示层共享组件**：组件不发请求、不知道 API；
  数据拉取与 modal 外壳留在各父页面。
- 顺手修正被确认：历史请求失败从「静默空列表」改为错误条 + 重试按钮。

## 1. 新增：HistoryList.vue（src/components/schedules/）

纯展示组件。接口：

```ts
export interface HistoryRow {
  id: string
  started_at?: string | null
  finished_at?: string | null
  status: string                 // running / completed / failed / pending
  summary?: string               // markdown 原文（剧本行没有此字段）
  error?: string
  run_id?: string                // 纯文本展示，截 8 位（shortId）
  preview?: string               // 折叠态预览句，由适配层算好传入（见 §3）
  expandable?: boolean           // 适配层追加的展开理由（剧本 = 有 steps_state/diagnosis）
}
```

- props：`rows: HistoryRow[]`。两个判定分开，互不混淆：
  - **可展开判定** = `expandable || !!summary || !!error`（剧本的 steps_state/diagnosis
    由适配层计入 expandable）；不可展开行不响应点击、无手型、无箭头；
  - **默认展开判定** = `status === 'failed'`（与 A 方案比选图一致：成功行有内容也
    默认折叠）；用户点击后翻转单行展开态。
  - `rows` 为空数组不渲染（空态归父页面）。
- 可展开行若无 summary 且无 error 且无 expandable，不会出现（适配层保证 preview/展开
  理由来自同一字段集），组件层不再对剧本做特殊假设。
- 折叠态一行：时间（`formatBackendTs`）/ 状态徽章 / 耗时（`durationText`，无 finished_at
  自然为空）/ `run xxxxxxxx` / 展开箭头；下方预览句 = `row.preview`，CSS 单行 ellipsis。
  `status === 'running'` 且无 preview → 固定灰字「（尚无摘要）」。
- 整行 header 为 toggle（`<button>` 语义，键盘可达），hover 行背景提亮一档。
- 展开区：`error` 文本在上（红，等宽字体），`renderMarkdown(summary)` 全文在下
  （max-height 300px 内滚），随后两个 slot：
  - `#extra="{ row }"` — 展开区附加内容（剧本的步骤徽章、诊断结论走这里）；
  - `#actions="{ row }"` — 行尾动作区，**仅 failed 行渲染**（诊断按钮语义），父页面不注入则不占位。
- 样式全部取现有主题 CSS 变量（徽章色沿用 `--accent-green/red/orange/text` 一套），
  禁止硬编码色值（主题一致性审查口径）。时间戳两端必须过 `formatBackendTs`
  （「时区接缝」家族教训）。

## 2. 新增：纯函数

- `src/utils/markdown.ts` 补导出 `mdPreviewLine(text: string): string`：剥掉 markdown 标记
  （`**`/`#`/列表符/代码围栏/`---`/图片链接语法留文字）取第一行有效文本；空文本返回空串。
- 新文件 `src/utils/history-view.ts`：
  - `statusLabel(status)` / `durationText(started, finished)` / `shortId(run_id)`
    ——现 SchedulesView 与 RunbookPanel 各一份的重复实现合并至此；
  - `scheduleRowPreview(r: ScheduleRun): string` = `mdPreviewLine(summary) || error || ''`；
  - `runbookRowPreview(r: RunbookRunRecord): string` = `error || steps_state[0]?.command || ''`
    （剧本无 summary）。
  全部纯函数，脱离组件可测。

## 3. 修改：SchedulesView.vue

- 历史 modal 的 `.history-list` 整块替换为
  `<HistoryList :key="historyFor?.id" :rows="historyRows">` + `#actions` slot
  （「⚡ AI 诊断此失败」按钮，`askAiDiagnose(historyFor, row 对应的原始 run)`——
  slot 回传 row.id，父页面保留原始 runs 数组按 id 反查，不为诊断改数据结构）。
- `historyRows` computed：`historyRuns.map(toScheduleRow)` 适配（含 preview、expandable 缺省）。
- `openHistory` 抽为可重入的 `fetchHistory()`，新增 `historyError` ref；失败渲染错误条 +
  重试按钮。`:key` 重挂载承担展开态重置（弹窗重开/切换任务回默认形态，组件内不 watch rows）。
- 删除被替代的内联 `.history-*` 样式与本地重复函数。

## 4. 修改：RunbookPanel.vue

- 同法替换列表主体；`steps_state` 徽章与 `diagnosis` 文本经 `#extra` slot 注入，
  原徽章样式随 slot 内容保留在本文件 scoped 样式中。
- `runbookRowPreview` 适配；`expandable` 由适配层赋值为
  `steps_state.length > 0 || !!diagnosis`；不注入 `#actions`（剧本无逐行诊断按钮，维持现状）。
- 删除被替代的内联 `.history-*` 样式与本地重复函数。

## 5. 后端

**零改动**：`GET /schedules/{id}/runs`、`GET /runbooks/{id}/runs` 现返回字段已够用，
limit 维持前端默认 50（分页属范围外）。

## 6. 错误处理

| 场景 | 行为 |
|---|---|
| 历史请求失败 | 弹窗内错误条（toFriendlyMessage 文案）+ 重试按钮，不再伪装成空态 |
| summary 为空 | 折叠行无预览句；running 行显示「（尚无摘要）」占位 |
| renderMarkdown 抛错 | 其内部已兜底降级为净化纯文本，不额外处理 |
| started_at/finished_at 缺失或倒挂 | durationText 返回空串，徽章行不显示耗时 |
| 未知 status 值 | statusLabel 原样透出（与现状一致） |

## 7. 测试（Vitest）

- `markdown.test.ts` 补 `mdPreviewLine`：加粗/标题/列表/代码围栏/分隔线剥离、多行取首行、空文本。
- `history-view.test.ts`：预览回退链（summary→error→步骤命令）、耗时边界、状态标签映射。
- `HistoryList.test.ts`：失败默认展开、成功默认折叠、点击 toggle、不可展开行不响应、
  `#extra`/`#actions` slot 渲染、actions 仅失败行出现。
- 源码守卫（沿用 RunbookPanel.test.ts 既有模式，挂 spec 标签）：两面板均经 HistoryList 渲染
  历史、旧内联 `.history-list` markup 已移除、剧本 steps_state/diagnosis 仍在。
- 命令沿用项目现有前端测试/类型检查口径。

## 8. 范围外（YAGNI）

- run 号点击跳转轨迹回放、弹窗打开期自动刷新、状态筛选/加载更多、焦点圈定、
  成本(token/费用)回显、后端接口改动——均不做，留待后续反馈驱动。

## 9. 决议记录

- 范围六选一（多选）→ 用户仅选「渲染 + 折叠」，轨迹跳转/自动刷新/筛选分页被否。
- 折叠形态三选一（浏览器并排，事件流点击 A）→ A 单行紧凑。
- 剧本面板同步与否 → 用户选「同步对齐」。
- 实现结构三选一 → 方案 1 纯展示层共享组件（方案 2 连数据封装、方案 3 两处复制被否）。
- 请求失败显示修正 → 随状态图一并呈现，用户确认 ok。
