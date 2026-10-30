<template>
  <!-- 负载页签：五个 kind 合并一张表；动作只给不造成不可逆后果的那几个 -->
  <div class="list">
    <div v-if="error" class="list-empty err-hint">负载取数失败：{{ error.message }}</div>
    <table v-else-if="rows.length" class="ptable clickable">
      <colgroup>
        <col style="width:10%"><col style="width:16%"><col style="width:12%"><col style="width:9%">
        <col style="width:11%"><col style="width:9%"><col style="width:25%"><col style="width:8%">
      </colgroup>
      <thead>
        <tr>
          <th>类型</th><th>名称</th><th>命名空间</th><th>就绪</th>
          <th>状态</th><th>AGE</th><th>镜像</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="w in rows" :key="workloadRowKey(w)"
          :class="{ selected: selectedKey === workloadRowKey(w) }"
          tabindex="0"
          @click="emit('open', w)"
          @keydown.enter.self.prevent="emit('open', w)"
          @keydown.space.self.prevent="emit('open', w)"
        >
          <td class="muted">{{ w.kind }}</td>
          <td class="mono" :title="w.name">{{ w.name }}</td>
          <td class="mono muted">{{ w.namespace }}</td>
          <td class="mono" :class="readyClass(w)">{{ w.ready || '—' }}</td>
          <td><span class="st" :class="statusClass(w)">{{ w.status }}</span></td>
          <td class="mono muted">{{ w.age }}</td>
          <td class="mono ell" :title="w.image">{{ w.image || '—' }}</td>
          <td>
            <span class="c-acts" @click.stop>
              <button
                v-if="canRestart(w)" class="icon-btn act-restart"
                :title="`重启（rollout restart）：${ACTION_CONSEQUENCE.rolloutRestart}`"
                :disabled="!!busy" @click="emit('restart', w)"
              >
                <svg v-if="busy === workloadRowKey(w)" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7"/><polyline points="21 3 21 9 15 9"/></svg>
              </button>
              <button
                v-if="canScale(w)" class="icon-btn act-scale" title="改副本数（scale）"
                :disabled="!!busy" @click="emit('scale', w)"
              >
                <svg v-if="busy === workloadRowKey(w)" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/></svg>
              </button>
              <button
                v-if="w.kind === 'Job'" class="icon-btn act-del"
                :title="`删除 Job：${ACTION_CONSEQUENCE.deleteJob}`"
                :disabled="!!busy" @click="emit('remove', w)"
              >
                <svg v-if="busy === workloadRowKey(w)" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
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
// K8sWorkloadsTab.vue — 负载页签的纯视图。取数与动作下发都在 K8sView。
import { ACTION_CONSEQUENCE, SCALABLE_KINDS, shortKind, workloadRowKey, type K8sError, type WorkloadRow } from '@/stores/k8s-core'
import { useK8sHost } from './context'

defineProps<{
  rows: WorkloadRow[]
  error: K8sError | undefined
  selectedKey: string | null
  emptyText: string
}>()
const emit = defineEmits<{
  open: [row: WorkloadRow]
  restart: [row: WorkloadRow]
  scale: [row: WorkloadRow]
  remove: [row: WorkloadRow]
}>()
const { busy } = useK8sHost()

const canRestart = (w: WorkloadRow): boolean => ['Deployment', 'StatefulSet', 'DaemonSet'].includes(w.kind)
const canScale = (w: WorkloadRow): boolean => SCALABLE_KINDS.includes(shortKind(w.kind))
/** 0/0 是「缩到 0」不是「全挂了」；只有期望>0 且没齐才标红 */
function readyClass(w: WorkloadRow): string {
  if (!w.ready) return 'muted'
  return w.desired > 0 && w.readyCount < w.desired ? 'bad' : 'ok'
}
function statusClass(w: WorkloadRow): string {
  if (w.status === 'OK' || w.status === 'Completed') return 'ok'
  if (w.status === 'Failure' || w.status === 'Failed' || w.status === 'Degraded') return 'bad'
  if (w.status === 'Rolling' || w.status === 'Suspended' || w.status === 'Scaled to 0') return 'warn'
  return 'muted'
}
</script>
