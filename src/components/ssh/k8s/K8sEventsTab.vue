<template>
  <!-- 事件页签：只读，但它是「为什么红了」的答案，message 给足宽度并允许换行 -->
  <div class="list">
    <div v-if="error" class="list-empty err-hint">事件取数失败：{{ error.message }}</div>
    <table v-else-if="rows.length" class="ptable ev-table">
      <colgroup>
        <col style="width:8%"><col style="width:7%"><col style="width:12%"><col style="width:14%">
        <col style="width:31%"><col style="width:6%"><col style="width:12%"><col style="width:10%">
      </colgroup>
      <thead>
        <tr>
          <th>类型</th><th>次数</th><th>原因</th><th>对象</th>
          <th>信息</th><th>命名空间</th><th>最近</th><th>来源</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(e, i) in rows" :key="`${e.namespace}/${e.object}/${e.reason}/${e.lastSeen}/${i}`">
          <td><span class="st" :class="e.type === 'Warning' ? 'bad' : 'muted'">{{ e.type }}</span></td>
          <td class="mono" :class="e.count > 1 ? 'warn' : 'muted'">{{ e.count }}</td>
          <td class="mono">{{ e.reason || '—' }}</td>
          <td class="mono ell" :title="e.object">{{ e.object }}</td>
          <td class="ev-msg" :title="e.message">{{ e.message || '—' }}</td>
          <td class="mono muted">{{ e.namespace || '—' }}</td>
          <td class="mono muted">{{ e.lastSeen || '—' }}</td>
          <td class="mono muted ell" :title="e.source">{{ e.source || '—' }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="list-empty">{{ emptyText }}</div>
  </div>
</template>

<script setup lang="ts">
// K8sEventsTab.vue — 事件页签的纯视图。Warning/Normal 的取数范围由工具条的开关决定。
import type { EventRow, K8sError } from '@/stores/k8s-core'

defineProps<{
  rows: EventRow[]
  error: K8sError | undefined
  /** 关掉 Normal 时的空态文案：不是「没有事件」，是「没有告警事件」 */
  emptyText: string
}>()
</script>
