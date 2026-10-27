# 定时任务/剧本执行历史弹窗渲染折叠 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 执行历史弹窗从「markdown 生肉平铺」升级为「单行紧凑折叠 + 渲染全文 + 失败默认展开」，定时任务与运维剧本两处经共享展示组件同步对齐。

**Architecture:** 纯展示组件 `HistoryList.vue`（不发请求）+ 纯函数层 `history-view.ts`（预览/适配/格式化）；两个父页面保留 modal 外壳与数据拉取，仅替换列表主体。后端零改动。

**Tech Stack:** Vue 3 SFC `<script setup>`、marked+DOMPurify（经 `renderMarkdown`）、Vitest（node 环境，组件层用 `?raw` 源码守卫）。

**Spec:** `docs/superpowers/specs/2026-10-27-schedule-history-render-design.md`（比选记录与边界以 spec 为准）

## Global Constraints

- 标签串联：实现涉及 spec 决策处的代码注释与守卫测试挂 `spec 2026-10-27-schedule-history-render`。
- 禁止硬编码色值，样式一律取 `src/styles/variables.css` 现有令牌（主题一致性审查口径）；本计划用到的令牌均已存在：`--bg-elevated --bg-hover --border-subtle --text-primary --text-secondary --text-tertiary --accent-green --accent-red --accent-orange --accent-text`。
- 时间戳展示两端必须过 `formatBackendTs` / `parseBackendTs`（`src/utils/format.ts`），禁止裸印后端 UTC naive 串（「时区接缝」家族教训）。
- vitest 为 node 环境且无 @vue/test-utils：组件契约用 `?raw` 源码守卫钉，行为用纯函数测试钉；测试 import 路径不带 `.ts` 后缀。
- 测试命令 `npm test`；类型检查 `npm run typecheck`。
- 共享工作树精准提交：`git add` 只列本任务文件，禁止 `git add .`；提交用约定式前缀。
- 范围红线（spec §0/§8）：run 号保持纯文本、不做轨迹跳转/自动刷新/筛选分页/焦点圈定；后端与 API 层零改动。

---

### Task 1: history-view.ts 纯函数层（预览/格式化/行适配器）

**Files:**
- Create: `src/utils/history-view.ts`
- Create: `src/utils/history-view.test.ts`
- Reference（只读不改）: `src/api/agent.ts:146-156`（ScheduleRun）、`src/api/runbook.ts:41-55`（RunbookRunRecord）、`src/utils/format.ts`（parseBackendTs）

**Interfaces:**
- Consumes: `parseBackendTs(ts: string): Date`（src/utils/format.ts 既有导出）
- Produces（Task 2/3/4 依赖的确切签名）:
  - `interface HistoryRow { id: string; started_at?: string | null; finished_at?: string | null; status: string; summary?: string; error?: string; run_id?: string; preview: string; expandable?: boolean }`
  - `mdPreviewLine(text: string): string`
  - `STATUS_LABEL_SCHEDULE: Record<string, string>` / `STATUS_LABEL_RUNBOOK: Record<string, string>`
  - `statusLabel(s: string | null | undefined, map: Record<string, string>, emptyText?: string): string`
  - `durationText(started?: string | null, finished?: string | null): string`（签名从 `(r)` 改为两参）
  - `shortId(id?: string | null): string`
  - `toScheduleRow(r: ScheduleRun): HistoryRow` / `toRunbookRow(r: RunbookRunRecord): HistoryRow`

- [ ] **Step 1: 写失败测试**

创建 `src/utils/history-view.test.ts`：

```ts
/**
 * history-view 纯函数守卫 —— spec 2026-10-27-schedule-history-render
 * 折叠态预览句 / 耗时 / 状态标签 / 行适配器的行为契约。node 环境直测，
 * 不 import markdown.ts（顶层 DOMPurify.addHook 在 node 下抛错，mdPreviewLine
 * 因此落在本文件——这是位置决策，移走必须连测试一起搬）。
 */
import { describe, expect, it } from 'vitest'
import {
  mdPreviewLine, statusLabel, durationText, shortId,
  toScheduleRow, toRunbookRow,
  STATUS_LABEL_SCHEDULE, STATUS_LABEL_RUNBOOK,
} from '@/utils/history-view'
import type { ScheduleRun } from '@/api/agent'
import type { RunbookRunRecord } from '@/api/runbook'

describe('mdPreviewLine', () => {
  it('剥加粗/行内代码/链接语法留文字', () => {
    expect(mdPreviewLine('**主机 10.0.0.1** 内存告警 `90%+`，见 [报告](http://x)'))
      .toBe('主机 10.0.0.1 内存告警 90%+，见 报告')
  })
  it('去标题井号与列表符', () => {
    expect(mdPreviewLine('## 发现的问题')).toBe('发现的问题')
    expect(mdPreviewLine('- **磁盘** 87%')).toBe('磁盘 87%')
    expect(mdPreviewLine('1. 主机告警')).toBe('主机告警')
  })
  it('多行取第一行有效文本，跳过空行与分隔线', () => {
    expect(mdPreviewLine('---\n\n首行摘要\n次行')).toBe('首行摘要')
  })
  it('整段代码围栏无正文 → 空串', () => {
    expect(mdPreviewLine('```\nls\n```')).toBe('')
  })
  it('空文本/纯空白 → 空串', () => {
    expect(mdPreviewLine('')).toBe('')
    expect(mdPreviewLine('   \n  ')).toBe('')
  })
})

describe('statusLabel', () => {
  it('两域 label 表各自映射；未知值原样透出', () => {
    expect(statusLabel('completed', STATUS_LABEL_SCHEDULE)).toBe('已完成')
    expect(statusLabel('success', STATUS_LABEL_RUNBOOK)).toBe('成功')
    expect(statusLabel('weird', STATUS_LABEL_SCHEDULE)).toBe('weird')
  })
  it('空值兜底可配：卡片位「未执行」，历史行位空串', () => {
    expect(statusLabel(null, STATUS_LABEL_RUNBOOK, '未执行')).toBe('未执行')
    expect(statusLabel('', STATUS_LABEL_SCHEDULE)).toBe('')
  })
})

describe('durationText', () => {
  it('正常差值保留 1 位小数', () => {
    expect(durationText('2026-10-27 07:38:00', '2026-10-27 07:39:16')).toBe('76.0s')
  })
  it('缺失/倒挂/非法 → 空串', () => {
    expect(durationText('2026-10-27 07:38:00', undefined)).toBe('')
    expect(durationText('2026-10-27 07:39:00', '2026-10-27 07:38:00')).toBe('')
    expect(durationText('garbage', '2026-10-27 07:38:00')).toBe('')
  })
})

describe('行适配器', () => {
  const baseRun: ScheduleRun = {
    id: 'r1', schedule_id: 's1', run_id: 'a33632b5ffff', status: 'completed',
    summary: '**主机 A** 内存告警\n\n细节', error: '',
    started_at: '2026-10-27 07:38:00', finished_at: '2026-10-27 07:39:16', created_at: '',
  }
  it('定时任务行：preview=summary 首行剥制；summary 空回退 error', () => {
    expect(toScheduleRow(baseRun).preview).toBe('主机 A 内存告警')
    expect(toScheduleRow({ ...baseRun, summary: '', error: 'SSH 超时' }).preview).toBe('SSH 超时')
  })
  it('剧本行：preview=步骤命令优先回退 error；expandable 由步骤/诊断决定', () => {
    const rb = {
      id: 'x1', runbook_id: 'b1', run_id: '', status: 'success', current_step: 1,
      steps_state: [{ seq: 1, status: 'success', command: 'df -h' }],
      diagnosis: '', error: '', started_at: '', created_at: '',
    } as unknown as RunbookRunRecord
    expect(toRunbookRow(rb).preview).toBe('df -h')
    expect(toRunbookRow(rb).expandable).toBe(true)
    expect(toRunbookRow({ ...rb, steps_state: [], diagnosis: '' }).expandable).toBe(false)
    // 成功剧本行即便带 error 残留，预览仍是步骤命令（spec §2 决议）
    expect(toRunbookRow({ ...rb, error: '旧错' }).preview).toBe('df -h')
  })
  it('shortId 截 8 位、空值安全', () => {
    expect(shortId('a33632b5ffff')).toBe('a33632b5')
    expect(shortId('')).toBe('')
    expect(shortId(undefined)).toBe('')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/utils/history-view.test.ts`
Expected: FAIL —「Cannot find module '@/utils/history-view'」

- [ ] **Step 3: 写实现**

创建 `src/utils/history-view.ts`：

```ts
/**
 * 执行历史的纯函数层 —— spec 2026-10-27-schedule-history-render
 * 状态标签 / 耗时 / run 号截断 / 折叠态预览句 / 两域行适配器。
 * mdPreviewLine 落这里而非 markdown.ts：后者顶层执行 DOMPurify.addHook，
 * vitest node 环境 import 即抛错，纯函数没法直测。
 * 对 API 类型的引用必须 import type（编译期擦除，node 测试不触运行时）。
 */
import { parseBackendTs } from '@/utils/format'
import type { ScheduleRun } from '@/api/agent'
import type { RunbookRunRecord } from '@/api/runbook'

/** 共享展示组件 HistoryList 的行契约（spec §1） */
export interface HistoryRow {
  id: string
  started_at?: string | null
  finished_at?: string | null
  status: string
  summary?: string
  error?: string
  run_id?: string
  /** 折叠态预览句：适配层算好传入，组件不猜（spec §2） */
  preview: string
  /** 适配层追加的展开理由（剧本 = 有 steps_state/diagnosis） */
  expandable?: boolean
}

export const STATUS_LABEL_SCHEDULE: Record<string, string> = {
  idle: '未执行',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  waiting_permission: '待审批',
  pending: '待执行',
}

export const STATUS_LABEL_RUNBOOK: Record<string, string> = {
  running: '执行中',
  success: '成功',
  failed: '失败',
  stopped: '已停止',
  stale_reaped: '中断（重启收尸）',
}

/** 未知值原样透出（与两页现状一致）；emptyText 供卡片位「未执行」兜底 */
export function statusLabel(
  s: string | null | undefined,
  map: Record<string, string>,
  emptyText = '',
): string {
  if (!s) return emptyText
  return map[s] || s
}

/** 单次执行耗时（秒，1 位小数）；缺失/倒挂/非法 → 空串 */
export function durationText(started?: string | null, finished?: string | null): string {
  if (!started || !finished) return ''
  const a = parseBackendTs(started).getTime()
  const b = parseBackendTs(finished).getTime()
  if (isNaN(a) || isNaN(b) || b <= a) return ''
  return `${Math.round((b - a) / 100) / 10}s`
}

export function shortId(id?: string | null): string {
  return id ? id.slice(0, 8) : ''
}

/** 剥 markdown 标记取首行有效文本（折叠态预览句）。刻意保守：只剥行内标记 */
export function mdPreviewLine(text: string): string {
  if (!text) return ''
  let inFence = false
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (/^(```|~~~)/.test(line)) { inFence = !inFence; continue }
    if (inFence || !line || /^(-{3,}|\*{3,}|_{3,})$/.test(line)) continue
    return stripInlineMd(line)
  }
  return ''
}

function stripInlineMd(s: string): string {
  return s
    .replace(/^#{1,6}\s+/, '')            // 标题
    .replace(/^[-*+]\s+/, '')             // 无序列表符
    .replace(/^\d+[.)]\s+/, '')           // 有序列表符
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1') // 链接/图片留文字
    .replace(/`{1,3}([^`]*)`{1,3}/g, '$1') // 行内代码/围栏残留
    .replace(/\*{1,3}([^*]+)\*{1,3}/g, '$1') // 加粗/斜体
    .replace(/_{2,3}([^_]+)_{2,3}/g, '$1')   // 下划线式加粗
    .replace(/~{2}([^~]+)~{2}/g, '$1')       // 删除线
    .trim()
}

export function scheduleRowPreview(r: ScheduleRun): string {
  return mdPreviewLine(r.summary || '') || r.error || ''
}

/** 步骤命令在前：error 优先会让成功行印出无关错误文本（spec §2 决议，失败行默认展开不靠预览辨错） */
export function runbookRowPreview(r: RunbookRunRecord): string {
  return r.steps_state?.[0]?.command || r.error || ''
}

export function toScheduleRow(r: ScheduleRun): HistoryRow {
  return {
    id: r.id, started_at: r.started_at, finished_at: r.finished_at,
    status: r.status, summary: r.summary, error: r.error, run_id: r.run_id,
    preview: scheduleRowPreview(r),
  }
}

export function toRunbookRow(r: RunbookRunRecord): HistoryRow {
  return {
    id: r.id, started_at: r.started_at, finished_at: r.finished_at,
    status: r.status, error: r.error, run_id: r.run_id,
    preview: runbookRowPreview(r),
    expandable: (r.steps_state?.length ?? 0) > 0 || !!r.diagnosis,
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/utils/history-view.test.ts`
Expected: PASS（14 用例）

- [ ] **Step 5: 提交**

```bash
git add src/utils/history-view.ts src/utils/history-view.test.ts
git commit -m "feat(schedule): 执行历史纯函数层——预览剥制/状态标签/两域行适配器"
```

---

### Task 2: HistoryList.vue 共享展示组件

**Files:**
- Create: `src/components/schedules/HistoryList.vue`
- Create: `src/components/schedules/HistoryList.guards.test.ts`

**Interfaces:**
- Consumes: Task 1 的 `HistoryRow` / `statusLabel` / `durationText` / `shortId` / `STATUS_LABEL_SCHEDULE`（仅作默认 labelMap）；既有 `renderMarkdown`（`@/utils/markdown`）、`formatBackendTs`（`@/utils/format`）
- Produces: 组件 props `{ rows: HistoryRow[]; labelMap?: Record<string, string> }`；slots `extra` / `actions`（均回传 `{ row: HistoryRow }`）。Task 3/4 按此接线。

- [ ] **Step 1: 写失败守卫**

创建 `src/components/schedules/HistoryList.guards.test.ts`（本仓无挂载测试环境，`?raw` 钉形状，先例 RunbookPanel.test.ts 头注）：

```ts
/**
 * HistoryList 源码守卫 —— spec 2026-10-27-schedule-history-render
 * 钉展示契约的存在性：A 方案判定式、渲染通道、slot、时区与令牌纪律。
 */
import { describe, expect, it } from 'vitest'
import raw from './HistoryList.vue?raw'

const src = raw.replace(/\r\n/g, '\n')

describe('HistoryList 展示契约', () => {
  it('可展开判定 = expandable || summary || error', () => {
    expect(src).toContain('expandableOf(row)')
    expect(src).toContain('row.expandable || !!row.summary || !!row.error')
  })
  it('默认展开仅失败行（A 方案：成功行有内容也默认折叠）', () => {
    expect(src).toContain("status === 'failed'")
  })
  it('展开区经 renderMarkdown 渲染、时间戳过 formatBackendTs', () => {
    expect(src).toContain('renderMarkdown(')
    expect(src).toContain('formatBackendTs(')
  })
  it('两个 slot 都存在且回传 row；actions 仅失败行渲染', () => {
    expect(src).toContain('<slot name="extra"')
    expect(src).toContain('<slot name="actions"')
    expect(src).toContain("v-if=\"row.status === 'failed'\"")
  })
  it('不可展开行不响应点击（toggle 内有守卫）', () => {
    expect(src).toContain('if (!expandableOf(row)) return')
  })
  it('running 无内容行有占位文案', () => {
    expect(src).toContain('（尚无摘要）')
  })
  it('无硬编码色值：颜色全走主题令牌', () => {
    const styleBlock = src.slice(src.indexOf('<style scoped>'))
    expect(styleBlock).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(styleBlock).not.toMatch(/\brgba?\(/)
    expect(styleBlock).toContain('var(--')
  })
})
```

- [ ] **Step 2: 跑守卫确认失败**

Run: `npx vitest run src/components/schedules/HistoryList.guards.test.ts`
Expected: FAIL — 文件不存在 / 断言找不到

- [ ] **Step 3: 写组件**

创建 `src/components/schedules/HistoryList.vue`：

```vue
<!--
  HistoryList — 执行历史共享展示组件（spec 2026-10-27-schedule-history-render）
  纯展示：不发请求、不知道 API。A 方案单行紧凑：折叠 = 元信息行 + 预览句，
  失败行默认展开，展开区 = error + renderMarkdown(summary) + #extra + #actions(仅失败)。
  展开态重置靠父页面 :key 重挂载，组件内不 watch rows。
-->
<template>
  <div class="history-list">
    <div
      v-for="row in rows"
      :key="row.id"
      class="history-item"
      :class="{ 'is-failed': row.status === 'failed' }"
    >
      <button
        class="history-head"
        :class="{ 'is-static': !expandableOf(row) }"
        type="button"
        @click="toggle(row)"
      >
        <span class="mono">{{ row.started_at ? formatBackendTs(row.started_at) : '—' }}</span>
        <span class="status" :class="row.status">{{ label(row.status) }}</span>
        <span class="mono" v-if="durationText(row.started_at, row.finished_at)">{{ durationText(row.started_at, row.finished_at) }}</span>
        <span class="mono" v-if="row.run_id">run {{ shortId(row.run_id) }}</span>
        <span class="chev" v-if="expandableOf(row)">{{ isExpanded(row) ? '▾' : '▸' }}</span>
      </button>
      <div
        v-if="!isExpanded(row)"
        class="history-preview mono-less"
        :title="row.preview"
      >{{ row.preview || (row.status === 'running' ? '（尚无摘要）' : '') }}</div>
      <div v-else-if="expandableOf(row)" class="history-body">
        <div class="history-error mono" v-if="row.error">{{ row.error }}</div>
        <div class="history-summary md-body" v-if="row.summary" v-html="renderMarkdown(row.summary)"></div>
        <slot name="extra" :row="row"></slot>
        <div class="history-actions" v-if="row.status === 'failed'">
          <slot name="actions" :row="row"></slot>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { renderMarkdown } from '@/utils/markdown'
import { formatBackendTs } from '@/utils/format'
import {
  durationText, shortId, statusLabel,
  STATUS_LABEL_SCHEDULE,
  type HistoryRow,
} from '@/utils/history-view'

const props = defineProps<{
  rows: HistoryRow[]
  labelMap?: Record<string, string>
}>()

function label(s: string): string {
  return statusLabel(s, props.labelMap || STATUS_LABEL_SCHEDULE)
}

function expandableOf(row: HistoryRow): boolean {
  return row.expandable || !!row.summary || !!row.error
}

// 失败行默认展开（A 方案）；Set 增删保证 Vue 响应式（mutating 方法已代理）
const expanded = ref(new Set<string>())
watch(
  () => props.rows,
  (rows) => {
    const next = new Set<string>()
    for (const r of rows) if (r.status === 'failed') next.add(r.id)
    expanded.value = next
  },
  { immediate: true },
)

function isExpanded(row: HistoryRow): boolean {
  return expanded.value.has(row.id)
}

function toggle(row: HistoryRow) {
  if (!expandableOf(row)) return
  const next = new Set(expanded.value)
  if (next.has(row.id)) next.delete(row.id)
  else next.add(row.id)
  expanded.value = next
}
</script>

<style scoped>
.history-list { display: flex; flex-direction: column; gap: 8px; }
.history-item { border: 1px solid var(--border-subtle); border-radius: 6px; padding: 6px 10px; background: var(--bg-elevated); }
.history-head {
  display: flex; align-items: center; gap: 12px; width: 100%;
  background: none; border: none; padding: 2px 0; cursor: pointer; text-align: left;
  font-size: 12px; color: var(--text-secondary);
}
.history-head:hover:not(.is-static) { background: var(--bg-hover); }
.history-head.is-static { cursor: default; }
.mono { font-family: var(--font-mono, monospace); }
.mono-less { font-family: inherit; }
.chev { margin-left: auto; color: var(--text-tertiary); }
.history-preview {
  font-size: 12px; color: var(--text-secondary); margin-top: 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.history-body { margin-top: 6px; border-left: 3px solid var(--border-subtle); padding-left: 10px; }
.history-item.is-failed .history-body { border-left-color: var(--accent-red); }
.history-error { font-size: 12px; color: var(--accent-red); margin-bottom: 4px; white-space: pre-wrap; }
.history-summary { font-size: 12px; color: var(--text-primary); max-height: 300px; overflow: auto; line-height: 1.6; }
.history-summary :deep(p) { margin: 0 0 6px; }
.history-summary :deep(ul), .history-summary :deep(ol) { margin: 0 0 6px; padding-left: 18px; }
.history-summary :deep(pre) { background: var(--bg-input); padding: 6px 8px; border-radius: 6px; overflow: auto; }
.history-summary :deep(code) { font-family: var(--font-mono, monospace); }
.history-summary :deep(h1), .history-summary :deep(h2), .history-summary :deep(h3),
.history-summary :deep(h4), .history-summary :deep(h5), .history-summary :deep(h6) {
  color: var(--text-primary); margin: 8px 0 4px; font-size: 13px;
}
.history-summary :deep(hr) { border: none; border-top: 1px solid var(--border-subtle); margin: 8px 0; }
.history-actions { margin-top: 6px; }
.status { font-size: 11px; padding: 1px 8px; border-radius: 8px; border: 1px solid var(--border-subtle); }
.status.running { color: var(--accent-text); border-color: var(--accent-primary); }
.status.completed, .status.success { color: var(--accent-green); border-color: var(--accent-green); }
.status.failed { color: var(--accent-red); border-color: var(--accent-red); }
.status.waiting_permission, .status.blocked { color: var(--accent-orange); border-color: var(--accent-orange); }
</style>
```

注：`--bg-input`、`--accent-primary` 若个别主题块未定义，以 `variables.css` 实际令牌为准替换（改前先 grep 确认两令牌在全部主题块存在；不存在则换 `--bg-elevated` / `--accent-text`）。

- [ ] **Step 4: 跑守卫确认通过**

Run: `npx vitest run src/components/schedules/HistoryList.guards.test.ts`
Expected: PASS（7 用例）

- [ ] **Step 5: 提交**

```bash
git add src/components/schedules/HistoryList.vue src/components/schedules/HistoryList.guards.test.ts
git commit -m "feat(schedule): HistoryList 共享展示组件——单行紧凑折叠+markdown 展开"
```

---

### Task 3: SchedulesView 接线（含失败态修正）

**Files:**
- Modify: `src/views/ssh/SchedulesView.vue`（历史 modal 模板 64-92 行区域；`openHistory` 669-681；`STATUS_LABEL`/`statusLabel`/`shortId` 700-714；`durationText` 738-745；样式 932-939）
- Create: `src/views/ssh/SchedulesView.history.guards.test.ts`

**Interfaces:**
- Consumes: Task 2 组件、Task 1 `toScheduleRow`
- Produces: 无下游任务依赖本任务内部实现

- [ ] **Step 1: 写失败守卫**

创建 `src/views/ssh/SchedulesView.history.guards.test.ts`：

```ts
/**
 * SchedulesView 历史接线守卫 —— spec 2026-10-27-schedule-history-render
 * 钉：列表主体已换成 HistoryList（旧内联 markup 移除）、失败态不再伪装空态、
 * 诊断按钮经 #actions slot 且反查原始 run 对象。
 */
import { describe, expect, it } from 'vitest'
import viewRaw from './SchedulesView.vue?raw'

const src = viewRaw.replace(/\r\n/g, '\n')

describe('历史接线', () => {
  it('列表主体经 HistoryList 渲染，:key 重挂载承担展开态重置', () => {
    expect(src).toContain('<HistoryList')
    expect(src).toContain(':key="historyFor?.id"')
    expect(src).toContain(':rows="historyRows"')
  })
  it('旧内联历史列表已移除', () => {
    expect(src).not.toContain('class="history-list"')
    expect(src).not.toContain('class="history-item"')
  })
  it('请求失败渲染错误条 + 重试，不再伪装空态', () => {
    expect(src).toContain('historyError')
    expect(src).toContain('fetchHistory()')
  })
  it('AI 诊断按钮经 #actions slot，反查原始 run 对象', () => {
    expect(src).toContain('#actions="{ row }"')
    expect(src).toContain('askAiDiagnose(historyFor, rawRunOf(row.id))')
  })
  it('行数据经 toScheduleRow 适配', () => {
    expect(src).toContain('toScheduleRow')
  })
})
```

- [ ] **Step 2: 跑守卫确认失败**

Run: `npx vitest run src/views/ssh/SchedulesView.history.guards.test.ts`
Expected: FAIL — 找不到 `<HistoryList`

- [ ] **Step 3: 改模板**

`SchedulesView.vue` 中把历史 modal 的 `<div class="modal-body">…</div>` 整块（含 `.empty-small`、`.history-list` v-for 到 `.history-actions`）替换为：

```vue
        <div class="modal-body">
          <div v-if="historyError" class="history-load-error">
            <span>{{ historyError }}</span>
            <button class="btn btn-sm" @click="fetchHistory()">重试</button>
          </div>
          <div v-else-if="historyRuns.length === 0" class="empty-small">暂无执行记录</div>
          <HistoryList v-else :key="historyFor?.id" :rows="historyRows">
            <template #actions="{ row }">
              <button class="btn btn-sm" @click="askAiDiagnose(historyFor, rawRunOf(row.id))">⚡ AI 诊断此失败</button>
            </template>
          </HistoryList>
        </div>
```

- [ ] **Step 4: 改脚本**

imports 区补（跟随文件现有 import 风格）：

```ts
import HistoryList from '@/components/schedules/HistoryList.vue'
import { toScheduleRow, statusLabel as statusLabelOf, durationText, shortId } from '@/utils/history-view'
import type { HistoryRow } from '@/utils/history-view'
```

`openHistory` 整函数替换为（保留 `_historySeq` 守卫语义；失败从「toast + 伪装空列表」改为「toast + 错误条」）：

```ts
async function openHistory(s: ScheduleRecord) {
  historyFor.value = s
  historyRuns.value = []
  historyError.value = ''
  await fetchHistory()
}

async function fetchHistory() {
  const s = historyFor.value
  if (!s) return
  historyError.value = ''
  const seq = ++_historySeq
  try {
    const runs = await scheduleApi.runs(s.id)
    if (seq !== _historySeq) return
    historyRuns.value = runs
  } catch (e) {
    if (seq !== _historySeq) return
    historyError.value = toFriendlyMessage(e, '加载历史失败')
    toast.error(historyError.value)
  }
}
```

在 `historyRuns` 声明附近补：

```ts
const historyError = ref('')
const historyRows = computed<HistoryRow[]>(() => historyRuns.value.map(toScheduleRow))
/** 诊断需要原始 run（summary/error 全文）：slot 只回传 row.id，按 id 反查 */
function rawRunOf(id: string): ScheduleRun {
  return historyRuns.value.find(r => r.id === id)!
}
```

删除本地 `STATUS_LABEL` 常量与 `statusLabel`/`shortId`/`durationText` 三个本地函数
（约 700-714、738-745 行），卡片 meta 区的 `statusLabel(s.last_status)` 调用点改为：

```ts
function statusLabel(s: string): string {
  return statusLabelOf(s, STATUS_LABEL_SCHEDULE)
}
```

并在 import 行补 `STATUS_LABEL_SCHEDULE`。`askAiDiagnose` 签名不变（仍收 `ScheduleRun`）。

- [ ] **Step 5: 清样式**

删除 scoped 样式中已被组件取代的 5 条：`.history-list`、`.history-item`、`.history-head`、`.history-summary`、`.history-error`、`.history-actions`（保留 `.empty-small`、`.countdown`），并补错误条样式：

```css
.history-load-error {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  font-size: 12px; color: var(--accent-red);
  border: 1px solid var(--accent-red); border-radius: 6px; padding: 8px 10px;
}
```

- [ ] **Step 6: 跑守卫 + 全量测试 + 类型检查**

Run: `npx vitest run src/views/ssh/SchedulesView.history.guards.test.ts` → PASS
Run: `npm test` → 全绿（既有 `schedule-task-type` 等守卫不受影响）
Run: `npm run typecheck` → 无错误

- [ ] **Step 7: 提交**

```bash
git add src/views/ssh/SchedulesView.vue src/views/ssh/SchedulesView.history.guards.test.ts
git commit -m "feat(schedule): 定时任务历史弹窗接 HistoryList——markdown 渲染+失败态修正"
```

---

### Task 4: RunbookPanel 接线（#extra 保留步骤徽章与诊断）

**Files:**
- Modify: `src/components/schedules/RunbookPanel.vue`（历史 modal 模板 55-92 行区域；`openHistory` 435-447；`STATUS_LABEL`/`statusLabel`/`shortId`/`durationText` 449-488；历史相关 scoped 样式）
- Modify: `src/components/schedules/RunbookPanel.test.ts`（更新「历史 modal 挂 diagnosis」describe 段为 #extra 形态；新增接线守卫段）

**Interfaces:**
- Consumes: Task 2 组件、Task 1 `toRunbookRow` / `STATUS_LABEL_RUNBOOK`
- Produces: 无下游依赖

- [ ] **Step 1: 更新既有守卫并新增接线守卫（先失败）**

`RunbookPanel.test.ts` 中「历史 modal 挂 diagnosis」describe 的断言
`v-if="run.diagnosis"` / `{{ run.diagnosis }}` 改为（slot 形态）：

```ts
describe('历史 modal 挂 diagnosis', () => {
  it('自动诊断结论经 #extra slot 渲染（失败执行的落点）', () => {
    expect(src).toContain('#extra="{ row }"')
    expect(src).toContain('diagnosisOf(row.id)')
    expect(src).toContain('{{ run.diagnosis }}')
  })
})
```

并新增一段：

```ts
describe('历史接线 HistoryList', () => {
  it('列表主体经 HistoryList + toRunbookRow，旧内联列表已移除', () => {
    expect(src).toContain('<HistoryList')
    expect(src).toContain('toRunbookRow')
    expect(src).not.toContain('class="history-list"')
  })
  it('步骤徽章仍在 #extra 内渲染（四态不因重构丢失）', () => {
    expect(src).toContain('hist-steps')
    expect(src).toContain('stepStatusLabel(st.status)')
  })
  it('请求失败渲染错误条 + 重试', () => {
    expect(src).toContain('historyError')
    expect(src).toContain('fetchHistory()')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/components/schedules/RunbookPanel.test.ts`
Expected: FAIL — 找不到 `<HistoryList`

- [ ] **Step 3: 改模板与脚本**

历史 modal 的 `.modal-body` 整块替换（与 Task 3 同构；`runbookApi.runs` 域内替换 `scheduleApi`）：

```vue
          <div class="modal-body">
            <div v-if="historyError" class="history-load-error">
              <span>{{ historyError }}</span>
              <button class="btn btn-sm" @click="fetchHistory()">重试</button>
            </div>
            <div v-else-if="historyRuns.length === 0" class="empty-small">暂无执行记录</div>
            <HistoryList v-else :key="historyFor?.id" :rows="historyRows" :label-map="STATUS_LABEL_RUNBOOK">
              <template #extra="{ row }">
                <div class="hist-steps" v-if="(stepsOf(row.id) || []).length">
                  <div v-for="st in stepsOf(row.id)" :key="st.seq" class="hist-step">
                    <span class="step-badge" :class="st.status">{{ stepStatusLabel(st.status) }}</span>
                    <code class="mono hist-cmd">{{ st.command }}</code>
                    <span class="hist-verdict" v-if="st.error" :title="st.error">{{ st.error }}</span>
                  </div>
                </div>
                <div class="history-diagnosis" v-if="diagnosisOf(row.id)">
                  <div class="k">AI 诊断</div>
                  <div class="diagnosis-text">{{ diagnosisOf(row.id) }}</div>
                </div>
              </template>
            </HistoryList>
          </div>
```

脚本改动：

```ts
import HistoryList from '@/components/schedules/HistoryList.vue'
import { toRunbookRow, statusLabel as statusLabelOf, STATUS_LABEL_RUNBOOK, type HistoryRow } from '@/utils/history-view'

const historyError = ref('')
const historyRows = computed<HistoryRow[]>(() => historyRuns.value.map(toRunbookRow))
// HistoryRow 是窄契约：步骤/诊断全文按 id 反查原始记录（同 SchedulesView rawRunOf 模式）
function stepsOf(id: string) { return historyRuns.value.find(r => r.id === id)?.steps_state }
function diagnosisOf(id: string) { return historyRuns.value.find(r => r.id === id)?.diagnosis || '' }
```

`openHistory` 拆为 `openHistory` + `fetchHistory`（照 Task 3 Step 4 的模式，API 换 `runbookApi.runs(r.id)`，保留 `_historySeq` 守卫与 toast，新增 `historyError` 赋值）。
删除本地 `STATUS_LABEL` 与 `statusLabel`/`shortId`/`durationText`/`parseTs` 函数；卡片 meta 的
`statusLabel(r.last_status)` 调用点保留同名包装：

```ts
function statusLabel(s?: string | null): string {
  return statusLabelOf(s, STATUS_LABEL_RUNBOOK, '未执行')
}
```

（`stepStatusLabel` 保留原位——它服务 slot 内容，不属历史行契约。）

- [ ] **Step 4: 清样式**

删除被组件取代的 scoped 样式：`.history-list` / `.history-item` / `.history-head` /
`.history-summary` / `.history-error`（如有）；**保留** `.hist-steps` / `.hist-step` /
`.step-badge.*` / `.hist-cmd` / `.hist-verdict` / `.history-diagnosis` / `.diagnosis-text`
（#extra slot 内容仍用）。补 `.history-load-error`（样式同 Task 3 Step 5）。

- [ ] **Step 5: 跑测试 + 类型检查**

Run: `npx vitest run src/components/schedules/RunbookPanel.test.ts` → PASS
Run: `npm test` → 全绿
Run: `npm run typecheck` → 无错误

- [ ] **Step 6: 提交**

```bash
git add src/components/schedules/RunbookPanel.vue src/components/schedules/RunbookPanel.test.ts
git commit -m "feat(schedule): 剧本历史弹窗同步接 HistoryList"
```

---

### Task 5: 全量验证与 spec 回填

**Files:**
- Modify: `docs/superpowers/specs/2026-10-27-schedule-history-render-design.md`（文末追加实施结果节）

- [ ] **Step 1: 全量检查**

Run: `npm test` → 全绿
Run: `npm run typecheck` → 无错误
Expected: 两者零失败。若 `styles/variables.test.ts` 等既有令牌守卫报错，说明 Task 2 用了
未登记令牌——按报错替换为 variables.css 实有令牌后重跑。

- [ ] **Step 2: 手工冒烟（可选但建议）**

`npm run dev` 起应用 → 定时任务 → 历史：确认成功行折叠显示预览句、点击展开渲染 markdown、
失败行红条默认展开、诊断按钮可用；运维剧本同查 #extra 步骤徽章。

- [ ] **Step 3: spec 回填实施结果并单独提交**

spec 文末追加「## 10. 实施结果」节：列出与设计的偏差（如有）与验证命令输出摘要。
文档与代码分离提交：

```bash
git add docs/superpowers/specs/2026-10-27-schedule-history-render-design.md
git commit -m "docs(spec): 回填历史弹窗渲染折叠实施结果"
```

---

## Self-Review 记录

- **Spec 覆盖**：§1 组件→Task 2；§2 纯函数→Task 1；§3/§4 两面板接线→Task 3/4；§5 后端零改动→无任务（正确）；§6 错误处理→Task 3/4 的 historyError；§7 测试→各任务 Step 1 + Task 5。无遗漏。
- **占位符扫描**：无 TBD/「同 Task N」——Task 4 引用 Task 3 的 fetchHistory 模式时给出了完整差异（API 名与文案），其余代码块均全文。
- **类型一致性**：`HistoryRow.preview` 必填（Task 1 适配器两分支都赋值）；`durationText(started, finished)` 两参签名在 Task 1 定义、Task 2 调用点一致；`toScheduleRow`/`toRunbookRow`/`rawRunOf`/`stepsOf`/`diagnosisOf` 命名跨任务一致。
