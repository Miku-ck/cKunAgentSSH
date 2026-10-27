<template>
  <div class="dt-overlay" @click.self="emit('close')">
    <div ref="dialogRef" class="dt-dialog dt-dialog-auto" role="dialog" aria-modal="true" :aria-label="'网络详情：' + network.name" tabindex="-1">
      <div class="dp-head">
        <span class="dp-name mono">{{ network.name }}</span>
        <span class="badge">{{ network.driver }}</span>
        <span class="badge">{{ network.scope }}</span>
        <button class="icon-btn dp-close" title="关闭" aria-label="关闭" @click="emit('close')">×</button>
      </div>
      <div class="dp-body dp-body-auto">
        <div v-if="inspectLoading" class="load-band">docker network inspect…</div>
        <div v-else-if="inspectError" class="logs-err">{{ inspectError.message }}</div>
        <template v-else-if="netInspect">
          <section>
            <h4>基本信息</h4>
            <div class="kv">
              <span class="k">名称</span><span class="v">{{ netInspect.name }}</span>
              <span class="k">ID</span><span class="v">{{ netInspect.id }}</span>
              <span class="k">驱动</span><span class="v">{{ netInspect.driver }}</span>
              <span class="k">范围</span><span class="v">{{ netInspect.scope }}</span>
              <span class="k">特性</span><span class="v">
                <span v-if="netInspect.internal" class="flag">internal</span>
                <span v-if="netInspect.attachable" class="flag">attachable</span>
                <span v-if="netInspect.ingress" class="flag">ingress</span>
                <span v-if="netInspect.enableIPv6" class="flag">ipv6</span>
                <span v-if="!(netInspect.internal || netInspect.attachable || netInspect.ingress || netInspect.enableIPv6)">—</span>
              </span>
            </div>
          </section>
          <section v-if="netInspect.ipam.length">
            <h4>IPAM</h4>
            <table class="ptable sub">
              <thead><tr><th>子网</th><th>网关</th><th>范围</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in netInspect.ipam" :key="i">
                  <td class="mono">{{ c.subnet || '—' }}</td>
                  <td class="mono">{{ c.gateway || '—' }}</td>
                  <td class="mono muted">{{ c.range || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </section>
          <section>
            <h4>连接容器 <span class="logs-sub mono">{{ netInspect.containers.length }}</span></h4>
            <div v-if="!netInspect.containers.length" class="list-empty sub">当前无容器连接</div>
            <table v-else class="ptable sub">
              <thead><tr><th>容器</th><th>IPv4</th><th>MAC</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in netInspect.containers" :key="i">
                  <td class="mono">{{ c.name || '—' }}</td>
                  <td class="mono">{{ c.ipv4 || '—' }}</td>
                  <td class="mono muted">{{ c.mac || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </section>
          <section v-if="Object.keys(netInspect.labels).length">
            <h4>标签 <span class="logs-sub mono">docker label</span></h4>
            <div class="kv kv-wide">
              <template v-for="(val, key) in netInspect.labels" :key="key">
                <span class="k mono" :title="key">{{ key }}</span>
                <span class="v">{{ val }}</span>
              </template>
            </div>
          </section>
          <section v-if="Object.keys(netInspect.options).length">
            <h4>驱动选项</h4>
            <div class="kv kv-wide">
              <template v-for="(val, key) in netInspect.options" :key="key">
                <span class="k mono" :title="key">{{ key }}</span>
                <span class="v">{{ String(val) }}</span>
              </template>
            </div>
          </section>
          <div class="dp-actions">
            <button class="btn btn-sm btn-danger" :title="isBuiltinNetwork(network.name) ? '内置网络（bridge/host/none）不可删除' : '删除网络'" :disabled="!!busy || isBuiltinNetwork(network.name)" @click="emit('remove', network)">删除网络</button>
          </div>
        </template>
        <div v-else class="list-empty">无详情</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// DockerNetworkDetailDialog.vue — 网络详情：基础 + IPAM 表 + 连接容器表 + 标签 / 驱动选项，底部快捷删除。
import { onMounted, onUnmounted, ref } from 'vue'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'
import { isBuiltinNetwork, networkInspectCmd, parseNetworkInspect, type DockerError, type NetworkInspect, type NetworkRow } from '@/stores/docker-core'
import { useDockerHost } from './context'

const props = defineProps<{ network: NetworkRow }>()
const emit = defineEmits<{
  close: []
  /** 弹窗内的「删除网络」：确认与执行都在父级，弹窗只表态 */
  remove: [row: NetworkRow]
}>()
const { busy, dockerExec, targetId } = useDockerHost()

const netInspect = ref<NetworkInspect | null>(null)
const inspectLoading = ref(false)
const inspectError = ref<DockerError | null>(null)

const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef)
// 纯阅读面：打开聚焦容器、关掉还给触发器；Esc 与右上角 ✕ 同一出口
function onKeydown(e: KeyboardEvent): void { if (e.key === 'Escape') emit('close') }

// 回包前必须确认「弹窗还开着、宿主没换」：卸载后的慢回包不该再写这份状态。
let alive = true
onUnmounted(() => {
  alive = false
  window.removeEventListener('keydown', onKeydown)
  deactivate()
})

async function openNetworkDetail(n: NetworkRow): Promise<void> {
  const id = targetId.value
  inspectLoading.value = true
  try {
    const next = parseNetworkInspect(await dockerExec(networkInspectCmd(n.name), id))
    if (!alive || targetId.value !== id) return
    netInspect.value = next
  } catch (e) {
    if (!alive || targetId.value !== id) return
    inspectError.value = e as DockerError
  } finally {
    if (alive && targetId.value === id) inspectLoading.value = false
  }
}
onMounted(() => {
  activate()
  window.addEventListener('keydown', onKeydown)
  void openNetworkDetail(props.network)
})
</script>
