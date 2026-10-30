<template>
  <!-- 节点页签：只读。集群级资源，不受命名空间选择器影响 -->
  <div class="list">
    <div v-if="error" class="list-empty err-hint">节点取数失败：{{ error.message }}</div>
    <table v-else-if="rows.length" class="ptable clickable">
      <colgroup>
        <col style="width:16%"><col style="width:12%"><col style="width:16%"><col style="width:14%">
        <col style="width:12%"><col style="width:8%"><col style="width:10%"><col style="width:12%">
      </colgroup>
      <thead>
        <tr>
          <th>名称</th><th>状态</th><th>角色</th><th>内部 IP</th>
          <th>版本</th><th>CPU</th><th>内存</th><th>就绪</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="n in rows" :key="n.name"
          :class="{ selected: selectedName === n.name }"
          tabindex="0"
          @click="emit('open', n)"
          @keydown.enter.self.prevent="emit('open', n)"
          @keydown.space.self.prevent="emit('open', n)"
        >
          <td class="mono" :title="n.name">{{ n.name }}</td>
          <td><span class="st" :class="n.ready ? 'ok' : 'bad'">{{ n.status }}</span></td>
          <td class="muted">{{ n.roles || '<none>' }}</td>
          <td class="mono muted">{{ n.internalIp || '—' }}</td>
          <td class="mono muted" :title="`${n.os}/${n.arch}`">{{ n.version }}</td>
          <td class="mono">{{ n.cpu }}<span v-if="n.cpuUsage" class="sub" :title="`实时 ${n.cpuUsage}，占可分配 ${n.cpuPerc}`"> · {{ n.cpuUsage }}<template v-if="n.cpuPerc">（{{ n.cpuPerc }}）</template></span></td>
          <td class="mono">{{ n.memory }}<span v-if="n.memUsage" class="sub" :title="`实时 ${n.memUsage}，占可分配 ${n.memPerc}`"> · {{ n.memUsage }}<template v-if="n.memPerc">（{{ n.memPerc }}）</template></span></td>
          <td class="mono muted">{{ n.pods }} Pod · {{ n.age }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="list-empty">{{ emptyText }}</div>
  </div>
</template>

<script setup lang="ts">
// K8sNodesTab.vue — 节点页签的纯视图。取数与富集都在 K8sView。
import type { K8sError, NodeRow } from '@/stores/k8s-core'

defineProps<{
  rows: NodeRow[]
  error: K8sError | undefined
  selectedName: string | null
  emptyText: string
}>()
const emit = defineEmits<{ open: [row: NodeRow] }>()
</script>
