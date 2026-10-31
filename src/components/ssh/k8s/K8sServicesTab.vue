<template>
  <!-- 服务页签：Service 与 Ingress 合并一张表，kind 决定末列读法 -->
  <div class="list">
    <div v-if="error" class="list-empty err-hint">
      <div>服务取数失败：{{ error.message }}</div>
      <div v-if="error.hint" class="err-hint-hint">{{ error.hint }}</div>
    </div>
    <table v-else-if="rows.length" class="ptable clickable">
      <colgroup>
        <col style="width:9%"><col style="width:18%"><col style="width:12%"><col style="width:11%">
        <col style="width:13%"><col style="width:13%"><col style="width:18%"><col style="width:6%">
      </colgroup>
      <thead>
        <tr>
          <th>类型</th><th>名称</th><th>命名空间</th><th>入口类型</th>
          <th>Cluster IP</th><th>外部 IP</th><th>端口 / 规则</th><th>AGE</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="s in rows" :key="serviceRowKey(s)"
          :class="{ selected: selectedKey === serviceRowKey(s) }"
          tabindex="0"
          @click="emit('open', s)"
          @keydown.enter.self.prevent="emit('open', s)"
          @keydown.space.self.prevent="emit('open', s)"
        >
          <td class="muted">{{ s.kind === 'Ingress' ? 'Ingress' : 'Service' }}</td>
          <td class="mono ell" :title="s.name">{{ s.name }}</td>
          <td class="mono muted">{{ s.namespace }}</td>
          <td class="mono">{{ s.type }}</td>
          <td class="mono muted">{{ s.clusterIp || '—' }}</td>
          <td class="mono muted ell" :title="s.externalIp">{{ s.externalIp || '<none>' }}</td>
          <td class="mono ell" :title="s.kind === 'Ingress' ? s.rules : s.ports">{{ (s.kind === 'Ingress' ? s.rules : s.ports) || '—' }}</td>
          <td class="mono muted">{{ s.age }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="list-empty">{{ emptyText }}</div>
  </div>
</template>

<script setup lang="ts">
// K8sServicesTab.vue — 服务页签的纯视图。这一页只读：四个窄动作里没有改服务的。
import type { K8sError, ServiceRow } from '@/stores/k8s-core'
import { serviceRowKey } from '@/stores/k8s-core'

defineProps<{
  rows: ServiceRow[]
  error: K8sError | undefined
  selectedKey: string | null
  emptyText: string
}>()
const emit = defineEmits<{ open: [row: ServiceRow] }>()
</script>
