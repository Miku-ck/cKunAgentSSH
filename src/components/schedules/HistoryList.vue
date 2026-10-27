<!--
  HistoryList — 执行历史共享展示组件（spec 2026-10-27-schedule-history-render）
  纯展示：不发请求、不知道 API。A 方案单行紧凑：折叠 = 元信息行 + 预览句，
  失败行默认展开，展开区 = error + renderMarkdown(summary) + #extra + #actions(仅失败)。
  展开态重置靠父页面 :key 重挂载；下方 watch 只做首帧初始化（immediate 一次性
  建「失败行默认展开」集合），不做 rows 变更重置——重挂载才是重置通道（spec §3）。
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
      <div v-if="!isExpanded(row)" class="history-preview" :title="row.preview">
        {{ row.preview || (row.status === 'running' ? '（尚无摘要）' : '') }}
      </div>
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
  // 判定式 = expandable || summary || error（spec §1）；!! 收口 undefined→boolean（strict）
  return !!row.expandable || !!row.summary || !!row.error
}

// 失败行默认展开（A 方案）。Set 整体替换保证响应式（mutating 代理亦可用，
// 但替换写法与 toggle 一致，不留两种心智）。
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
.mono { font-family: var(--font-mono); }
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
.history-summary :deep(code) { font-family: var(--font-mono); }
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
