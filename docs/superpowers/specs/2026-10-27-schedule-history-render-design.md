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

## 2. 新增：纯函数（全部落在 src/utils/history-view.ts）

- `mdPreviewLine(text: string): string`：剥掉 markdown 标记（`**`/`#`/列表符/代码围栏/
  `---`/图片链接语法留文字）取第一行有效文本；空文本返回空串。
  **不放 src/utils/markdown.ts**：那个模块顶层执行 `DOMPurify.addHook`，node 环境
  （vitest 无 jsdom）下 import 即报错，纯函数测试跑不进去。
- `src/utils/history-view.ts` 其余导出：
  - `statusLabel(status, labelMap, emptyText?)` —— label 表由各域传入（两域取值集合不同：
    schedules 用 completed/idle，runbooks 用 success/stopped）；空值兜底文案可配：
    卡片位传「未执行」（剧本现状），历史行位传空串（状态恒非空，出现即原样透出）；
  - `durationText(started, finished)` / `shortId(run_id)`
    ——现 SchedulesView 与 RunbookPanel 各一份的重复实现合并至此；
  - `scheduleRowPreview(r: ScheduleRun): string` = `mdPreviewLine(summary) || error || ''`；
  - `runbookRowPreview(r: RunbookRunRecord): string` = `steps_state[0]?.command || error || ''`
    （剧本无 summary；步骤命令在前——若 error 优先，成功执行的剧本行预览会印出无关的
    错误文本；失败行本就默认展开，不依赖预览辨错）；
  - 行适配器 `toScheduleRow(r)` / `toRunbookRow(r)` 也在本文件（纯函数可测，
    `expandable`/`preview` 在适配器内算好，父页面 computed 只负责 map）。
  - `HistoryRow` 接口定义也放这里（组件与两域适配层共用；对 API 类型的引用用
    `import type`，编译期擦除，node 测试环境不触运行时）。
  全部纯函数，脱离组件与 DOM 可测。

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
| 历史请求失败 | 保留既有 toast + 弹窗内错误条（toFriendlyMessage 文案）与重试按钮，不再伪装成空态 |
| summary 为空 | 折叠行无预览句；running 行显示「（尚无摘要）」占位 |
| renderMarkdown 抛错 | 其内部已兜底降级为净化纯文本，不额外处理 |
| started_at/finished_at 缺失或倒挂 | durationText 返回空串，徽章行不显示耗时 |
| 未知 status 值 | statusLabel 原样透出（与现状一致） |

## 7. 测试（Vitest，node 环境）

本仓无组件挂载测试环境（vitest 为 node 环境、未装 @vue/test-utils/jsdom，先例
RunbookPanel.test.ts 头注已声明），组件层用 `?raw` 源码形状守卫钉契约，行为层由纯函数测试承担：

- `history-view.test.ts`：`mdPreviewLine` 加粗/标题/列表/代码围栏/分隔线剥离、多行取首行、
  空文本；预览回退链（summary→error→步骤命令）；耗时边界（缺失/倒挂/正常差值）；
  statusLabel 双域 label 表与空值兜底。import 路径不带 `.ts` 后缀。
- `HistoryList.guards.test.ts`（?raw）：失败行默认展开判定、可展开判定式、renderMarkdown
  用于展开区、`#extra`/`#actions` slot 存在、时间戳过 formatBackendTs、无硬编码色值。
- 接线守卫（?raw，扩入既有 RunbookPanel.test.ts 与新文件）：两面板均经 HistoryList 渲染
  历史、旧内联 `.history-list` markup 已移除、剧本 steps_state/diagnosis 仍在 #extra 里、
  历史请求失败渲染错误条 + 重试按钮。
- 命令沿用项目现有口径：`npm test`、`npm run typecheck`。

## 8. 范围外（YAGNI）

- run 号点击跳转轨迹回放、弹窗打开期自动刷新、状态筛选/加载更多、焦点圈定、
  成本(token/费用)回显、后端接口改动——均不做，留待后续反馈驱动。

## 9. 决议记录

- 范围六选一（多选）→ 用户仅选「渲染 + 折叠」，轨迹跳转/自动刷新/筛选分页被否。
- 折叠形态三选一（浏览器并排，事件流点击 A）→ A 单行紧凑。
- 剧本面板同步与否 → 用户选「同步对齐」。
- 实现结构三选一 → 方案 1 纯展示层共享组件（方案 2 连数据封装、方案 3 两处复制被否）。
- 请求失败显示修正 → 随状态图一并呈现，用户确认 ok。

## 10. 实施结果

按 `docs/superpowers/plans/2026-10-27-schedule-history-render.md` 五任务 TDD 落地，全部完成：

| 交付物 | 提交 | 验证 |
|---|---|---|
| `src/utils/history-view.ts` 纯函数层 + `history-view.test.ts` | d37dd9f | 12/12 |
| `src/components/schedules/HistoryList.vue` + `.guards.test.ts` | a422a2d | 7/7 守卫 |
| `SchedulesView.vue` 接线 + `.history.guards.test.ts` | 68ea3d0 | 5/5 守卫 |
| `RunbookPanel.vue` 接线（#extra）+ 更新 `RunbookPanel.test.ts` | 2bb2056 | 14/14 |

全量验证（HEAD=2bb2056）：`npm test` **133 files / 1634 tests 全绿**（含 `styles/variables.test.ts` 39/39，无未登记令牌）；`npm run typecheck` web+node **EXIT=0**。

### 与设计的偏差（均已在 SDD 台账留 Ruling）

1. **durationText 显示口径**：原两页用 `Math.round(ms/100)/10` 模板字面量会把 `76.0s` 吃成 `76s`，与「1 位小数」注释矛盾——共享层统一 `toFixed(1)`，显示更诚实，数值语义等价。
2. **接线层 import 剪枝**：删旧内联 markup 后 `durationText/shortId`（SchedulesView）、`shortId/parseTs/durationText/parseBackendTs`（RunbookPanel）在视图内失去调用点，strict `noUnusedLocals` 要求收窄 import；这些能力已内聚进 HistoryList/history-view，行为不变。
3. **RunbookPanel 诊断守卫断言**：slot 只回传 `row`，diagnosis 全文经 `diagnosisOf(row.id)` 反查，断言相应从 `{{ run.diagnosis }}` 改为 `diagnosisOf(row.id)`（语义等价）。
4. **展开态 watch 与 §3 口径**：§3 原写「组件内不 watch rows」，但组件先以空 rows 挂载、fetch 后填充而 `:key`（目标 id）在该转换中不变，首帧「失败行默认展开」必须靠 watch 重建集合——故保留 watch，代价是同目标内「重试」回到默认折叠（吞掉手动展开）。注释已改至与行为一致。
5. **终审修正（CodeReview M1）**：徽章色随历史行从 RunbookPanel 迁入 HistoryList 后，剧本域 `stopped`/`stale_reaped` 两终态一度退化为默认灰边（原 scoped 色不再跨组件生效）——已在 HistoryList 补齐两域并集橙色，并新增守卫钉住「两域状态键并集全部有专属色」。同批收紧：失败行默认展开加 `expandableOf` 闸（spec §1 不可展开组合不留渲染空洞）。

后端零改动（§5 达成）。手工真机冒烟（§Step 2）因需 Electron 运行环境，留待用户在本地 `npm run dev` 走查。
