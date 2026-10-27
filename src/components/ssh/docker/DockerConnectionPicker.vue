<template>
  <div class="dk-overlay" @click.self="emit('close')">
    <div ref="dialogRef" class="dk-dialog" role="dialog" aria-modal="true" aria-label="选择 SSH 连接" tabindex="-1">
      <div class="dk-header">
        <span>选择 SSH 连接</span>
        <button class="dk-close" aria-label="关闭" @click="emit('close')">×</button>
      </div>
      <div class="dk-list">
        <div v-if="!connections.length" class="dk-empty">暂无 SSH 连接配置，请先在「远程管理」中新建连接</div>
        <div
          v-for="conn in connections"
          :key="conn.id"
          class="dk-item"
          :class="{ disabled: connecting }"
          role="button"
          tabindex="0"
          @click="emit('pick', conn)"
          @keydown.enter.prevent="emit('pick', conn)"
          @keydown.space.prevent="emit('pick', conn)"
        >
          <div class="dk-info">
            <span class="dk-name">{{ conn.name }}</span>
            <span class="dk-host">{{ conn.username }}@{{ conn.host }}:{{ conn.port }}<span v-if="connNote(conn)" class="dk-note" :title="connNote(conn)">{{ connNote(conn) }}</span></span>
          </div>
          <svg v-if="connecting && pendingId === conn.id" class="dk-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// DockerConnectionPicker.vue — 从已保存连接里挑一台连上，连上后即为 Docker 页的目标主机。
// 所有已保存连接都列出来（不止当前有会话的），因为 Docker 页允许「不预设必须有会话」。
import { onMounted, onUnmounted, ref } from 'vue'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'
import { connNote, type SSHConnection } from '@/stores/ssh'

defineProps<{
  connections: SSHConnection[]
  connecting: boolean
  pendingId: string
}>()
const emit = defineEmits<{
  close: []
  pick: [conn: SSHConnection]
}>()

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') emit('close')
}

const dialogRef = ref<HTMLElement | null>(null)
// 列表项本就该能用键盘选（原来只有 @click）；初始焦点落关闭按钮，跟其他选择面一致
const { activate, deactivate } = useModalFocusTrap(dialogRef, { initialFocus: '.dk-close' })
onMounted(() => {
  activate()
  document.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  document.removeEventListener('keydown', onKey)
  deactivate()
})
</script>
