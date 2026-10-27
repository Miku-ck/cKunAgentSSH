<!--
  HistoryList — 执行历史共享展示组件（spec 2026-10-27-schedule-history-render）
  纯展示：不发请求、不知道 API。A 方案单行紧凑：折叠 = 元信息行 + 预览句，
  失败行默认展开，展开区 = error + renderMarkdown(summary) + #extra + #actions(仅失败)。
  展开态重置主通道是父页面 :key 重挂载（切目标）；下方 watch 对每次 rows 引用
  变化重建「失败行默认展开」集合——因为本组件先以空 rows 挂载、fetch 后填充，
  :key 在该转换中不变，首帧填充只能靠 watch（代价：同目标重试回到默认折叠）。
-->
<template>
  <div class="history-list">
    <div
      v-for="row in rows"
      :key="row.id"
      class="history-item"
      :class="{ 'is-failed': row.status === 'failed' }"
    >
      <component
        :is="expandableOf(row) ? 'button' : 'div'"
        class="history-head"
        :class="{ 'is-static': !expandableOf(row) }"
        :type="expandableOf(row) ? 'button' : undefined"
        @click="toggle(row)"
      >
        <span>{{ row.started_at ? formatBackendTs(row.started_at) : '—' }}</span>
        <span class="status" :class="row.status">{{ label(row.status) }}</span>
        <span v-if="durationText(row.started_at, row.finished_at)">{{ durationText(row.started_at, row.finished_at) }}</span>
        <span class="mono" v-if="row.run_id">run {{ shortId(row.run_id) }}</span>
        <span class="chev" v-if="expandableOf(row)">{{ isExpanded(row) ? '▾' : '▸' }}</span>
      </component>
      <div v-if="!isExpanded(row)" class="history-preview" :title="row.preview">
        {{ row.preview || (row.status === 'running' ? '（尚无摘要）' : '') }}
      </div>
      <div v-else-if="expandableOf(row)" class="history-body">
        <div class="history-error" v-if="row.error">{{ row.error }}</div>
        <div class="history-summary markdown-body" v-if="row.summary" v-html="renderMarkdown(row.summary)" @click="onMarkdownClick"></div>
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
import { useMarkdownDelegation } from '@/composables/useMarkdownDelegation'
import { formatBackendTs } from '@/utils/format'
import {
  durationText, shortId, statusLabel,
  STATUS_LABEL_SCHEDULE,
  type HistoryRow,
} from '@/utils/history-view'

const { onMarkdownClick } = useMarkdownDelegation()

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

// 失败行默认展开（A 方案），且仅限可展开行（spec §1：不可展开走折叠预览，不留渲染空洞）。
// watch 对每次 rows 引用变化重建集合：本组件在父页面先以空 rows 挂载、fetch 后填充，
// :key（目标 id）在该转换中不变，故首帧填充必须靠 watch 完成——非纯首帧 seed。
// 代价：同目标内「重试」重取会回到默认折叠态（吞掉手动展开），可接受。
// Set 整体替换保证响应式。
const expanded = ref(new Set<string>())
watch(
  () => props.rows,
  (rows) => {
    const next = new Set<string>()
    for (const r of rows) if (r.status === 'failed' && expandableOf(r)) next.add(r.id)
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
.history-list { display: flex; flex-direction: column; gap: var(--spacing-8); }
.history-item { border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: var(--spacing-6) var(--spacing-10); background: var(--bg-elevated); }
.history-head {
  display: flex; align-items: center; gap: var(--spacing-12); width: 100%;
  flex-wrap: nowrap; min-width: 0;
  background: none; border: none; padding: var(--spacing-2) 0; cursor: pointer; text-align: left;
  font-size: 0.9231rem; line-height: 1.4; color: var(--text-secondary);
}
.history-head:hover:not(.is-static) { background: var(--bg-hover); }
.history-head.is-static { cursor: default; }
.mono { font-family: var(--font-mono); white-space: nowrap; flex: 0 0 auto; }
.chev { margin-left: auto; color: var(--text-tertiary); }
.history-preview {
  font-size: 0.9231rem; color: var(--text-secondary); margin-top: var(--spacing-4);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.history-body { margin-top: var(--spacing-6); border-left: 3px solid var(--border-subtle); padding-left: var(--spacing-10); }
.history-item.is-failed .history-body { border-left-color: var(--accent-red); }
.history-error { font-size: 0.9231rem; color: var(--accent-red); margin-bottom: var(--spacing-4); white-space: pre-wrap; overflow-wrap: anywhere; }
.history-summary { font-size: 0.9231rem; color: var(--text-primary); max-height: 300px; overflow: auto; overflow-wrap: anywhere; line-height: 1.6; }
.history-actions { margin-top: var(--spacing-6); }
.status { font-size: 0.8462rem; padding: 1px var(--spacing-8); border-radius: var(--radius-lg); border: 1px solid var(--border-subtle); white-space: nowrap; flex: 0 0 auto; }
.status.running { color: var(--accent-text); border-color: var(--accent-primary); }
.status.completed, .status.success { color: var(--accent-green); border-color: var(--accent-green); }
.status.failed { color: var(--accent-red); border-color: var(--accent-red); }
.status.waiting_permission, .status.blocked, .status.stopped, .status.stale_reaped { color: var(--accent-orange); border-color: var(--accent-orange); }
</style>
