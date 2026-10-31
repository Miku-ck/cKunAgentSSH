<template>
  <!-- Pod 页签：异常置顶（正在崩溃的排在第一行，而不是埋在字母序里） -->
  <div class="list">
    <div v-if="error" class="list-empty err-hint">
      <div>Pod 取数失败：{{ error.message }}</div>
      <div v-if="error.hint" class="err-hint-hint">{{ error.hint }}</div>
    </div>
    <table v-else-if="rows.length" class="ptable clickable">
      <colgroup>
        <col style="width:22%"><col style="width:12%"><col style="width:7%"><col style="width:15%">
        <col style="width:8%"><col style="width:7%"><col style="width:11%"><col style="width:9%"><col style="width:9%">
      </colgroup>
      <thead>
        <tr>
          <th>名称</th><th>命名空间</th><th>就绪</th><th>状态</th>
          <th>重启</th><th>AGE</th><th>IP</th><th>节点</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="p in rows" :key="podRowKey(p)"
          :class="{ selected: selectedKey === podRowKey(p) }"
          tabindex="0"
          @click="emit('open', p)"
          @keydown.enter.self.prevent="emit('open', p)"
          @keydown.space.self.prevent="emit('open', p)"
        >
          <td class="mono ell" :title="p.name">{{ p.name }}</td>
          <td class="mono muted">{{ p.namespace }}</td>
          <td class="mono" :class="p.readyCount === p.totalCount ? 'ok' : 'bad'">{{ p.ready }}</td>
          <td><span class="st" :class="podTone(p)">{{ p.status }}</span></td>
          <td class="mono" :class="p.restarts > 0 ? 'warn' : 'muted'">{{ p.restarts }}</td>
          <td class="mono muted">{{ p.age }}</td>
          <td class="mono muted">{{ p.ip || '—' }}</td>
          <td class="mono muted ell" :title="p.node">{{ p.node || '—' }}</td>
          <td>
            <span class="c-acts" @click.stop>
              <button
                class="icon-btn act-del"
                :title="`删除 Pod：${ACTION_CONSEQUENCE.deletePod}`"
                :disabled="!!busy" @click="emit('remove', p)"
              >
                <svg v-if="busy === podRowKey(p)" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
              </button>
            </span>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else class="list-empty">{{ emptyText }}</div>
  </div>
</template>

<script setup lang="ts">
// K8sPodsTab.vue — Pod 页签的纯视图。排序、取数与动作下发都在 K8sView。
import { ACTION_CONSEQUENCE, podRowKey, podTone, type K8sError, type PodRow } from '@/stores/k8s-core'
import { useK8sHost } from './context'

defineProps<{
  rows: PodRow[]
  error: K8sError | undefined
  selectedKey: string | null
  emptyText: string
}>()
const emit = defineEmits<{
  open: [row: PodRow]
  remove: [row: PodRow]
}>()
const { busy } = useK8sHost()
</script>
