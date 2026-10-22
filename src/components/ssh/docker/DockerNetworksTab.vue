<template>
  <!-- 网络页签：整行可点开详情；内置网络（bridge/host/none）不给必然失败的删除入口 -->
  <div class="list">
    <div v-if="error" class="list-empty err-hint">网络取数失败：{{ error }}</div>
    <table v-else-if="rows.length" class="ptable clickable">
      <colgroup><col style="width:16%"><col style="width:12%"><col style="width:26%"><col style="width:12%"><col style="width:20%"><col style="width:14%"></colgroup>
      <thead><tr><th>网络 ID</th><th>驱动</th><th>名称</th><th>范围</th><th>标签</th><th>操作</th></tr></thead>
      <tbody>
        <tr
          v-for="n in rows" :key="n.id"
          :class="{ selected: selectedId === n.id }"
          @click="emit('open', n)"
        >
          <td class="mono muted" :title="n.id">{{ n.id }}</td>
          <td class="mono">{{ n.driver }}</td>
          <td class="mono" :title="n.name">{{ n.name }}</td>
          <td class="muted">{{ n.scope }}</td>
          <td class="mono label-hint" :title="n.labels ? Object.entries(n.labels).map(([k, val]) => k + '=' + val).join('\n') : ''">{{ labelHint(n.labels) }}</td>
          <td>
            <span class="c-acts" @click.stop>
              <button class="icon-btn act-del" :title="isBuiltinNetwork(n.name) ? '内置网络（bridge/host/none）不可删除' : '删除网络'" :disabled="!!busy || isBuiltinNetwork(n.name)" @click="emit('remove', n)">
                <svg v-if="busy === 'n:' + n.name" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
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
// DockerNetworksTab.vue — 网络页签的纯视图。取数、rm/prune 都在 DockerView。
import { isBuiltinNetwork, labelHint, type NetworkRow } from '@/stores/docker-core'
import { useDockerHost } from './context'

defineProps<{
  rows: NetworkRow[]
  error: string | undefined
  /** 详情弹窗开着的那一行 */
  selectedId: string | null
  emptyText: string
}>()
const emit = defineEmits<{
  open: [row: NetworkRow]
  remove: [row: NetworkRow]
}>()
const { busy } = useDockerHost()
</script>
