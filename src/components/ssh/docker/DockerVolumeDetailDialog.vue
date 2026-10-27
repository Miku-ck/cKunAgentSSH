<template>
  <div class="dt-overlay" @click.self="emit('close')">
    <div ref="dialogRef" class="dt-dialog dt-dialog-auto" role="dialog" aria-modal="true" :aria-label="'卷详情：' + volume.name" tabindex="-1">
      <div class="dp-head">
        <span class="dp-name mono">{{ volume.name }}</span>
        <span class="badge">{{ volume.driver }}</span>
        <span v-if="volumeUsageInfo.size" class="badge">{{ volumeUsageInfo.size }}</span>
        <span
          v-if="typeof volumeUsageInfo.refCount === 'number'"
          class="badge"
          :class="volumeUsageInfo.refCount === 0 ? 'warn' : 'ok'"
        >{{ volumeUsageInfo.refCount === 0 ? '未使用' : volumeUsageInfo.refCount + ' 个容器' }}</span>
        <button class="icon-btn dp-close" title="关闭" aria-label="关闭" @click="emit('close')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="dp-body dp-body-auto">
        <div v-if="inspectLoading" class="load-band">docker volume inspect…</div>
        <div v-else-if="inspectError" class="logs-err">{{ inspectError.message }}</div>
        <template v-else-if="volInspect">
          <section>
            <h4>基本信息</h4>
            <div class="kv">
              <span class="k">名称</span><span class="v">{{ volInspect.name }}</span>
              <span class="k">驱动</span><span class="v">{{ volInspect.driver }}</span>
              <span class="k">创建时间</span><span class="v">{{ volInspect.createdAt || '—' }}</span>
              <span class="k">挂载点</span><span class="v">{{ volInspect.mountpoint || '—' }}</span>
              <span class="k">引用容器</span><span class="v">{{ volumeUsageInfo.refCount ?? '—' }}</span>
              <span class="k">占用空间</span><span class="v">{{ volumeUsageInfo.size || '—' }}</span>
            </div>
          </section>
          <section v-if="Object.keys(volInspect.labels).length">
            <h4>标签 <span class="logs-sub mono">docker label</span></h4>
            <div class="kv kv-wide">
              <template v-for="(val, key) in volInspect.labels" :key="key">
                <span class="k mono" :title="key">{{ key }}</span>
                <span class="v">{{ val }}</span>
              </template>
            </div>
          </section>
          <section v-if="Object.keys(volInspect.options).length">
            <h4>驱动选项</h4>
            <div class="kv kv-wide">
              <template v-for="(val, key) in volInspect.options" :key="key">
                <span class="k mono" :title="key">{{ key }}</span>
                <span class="v">{{ String(val) }}</span>
              </template>
            </div>
          </section>
          <div class="dp-actions">
            <button class="btn btn-sm btn-danger" :disabled="!!busy" @click="emit('remove', volume)">删除卷</button>
          </div>
        </template>
        <div v-else class="list-empty">无详情</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// DockerVolumeDetailDialog.vue — 卷详情：inspect 一次拉后展示元数据 + 标签 + 驱动选项，底部快捷删除。
// 用量优先级：inspect 的 UsageData（新版才给）→ 列表富集的 docker system df -v；都缺则「—」。
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'
import { parseVolumeInspect, volumeInspectCmd, volumeUsageSummary, type DockerError, type VolumeInspect, type VolumeRow } from '@/stores/docker-core'
import { useDockerHost } from './context'

const props = defineProps<{ volume: VolumeRow }>()
const emit = defineEmits<{
  close: []
  /** 弹窗内的「删除卷」：确认与执行都在父级，弹窗只表态 */
  remove: [row: VolumeRow]
}>()
const { busy, dockerExec, targetId } = useDockerHost()

const volInspect = ref<VolumeInspect | null>(null)
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

async function openVolumeDetail(v: VolumeRow): Promise<void> {
  const id = targetId.value
  inspectLoading.value = true
  try {
    const next = parseVolumeInspect(await dockerExec(volumeInspectCmd(v.name), id))
    if (!alive || targetId.value !== id) return
    volInspect.value = next
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
  void openVolumeDetail(props.volume)
})

const volumeUsageInfo = computed(() => volumeUsageSummary(props.volume, volInspect.value))
</script>
