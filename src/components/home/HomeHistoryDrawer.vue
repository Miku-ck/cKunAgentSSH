<template>
  <div class="home-history-drawer">
    <div class="drawer-mask" @click="$emit('close')"></div>
    <aside
      id="home-history-drawer"
      ref="asideRef"
      class="drawer"
      role="dialog"
      aria-modal="false"
      aria-label="历史记录"
      tabindex="-1"
      @keydown="onKeydown"
    >
      <div class="drawer-head">
        <span class="drawer-title">历史记录</span>
        <button ref="closeBtn" type="button" class="drawer-close" title="关闭" aria-label="关闭" @click="$emit('close')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="drawer-list">
        <HistoryConversationList
          :groups="groups"
          :current-conv-id="currentConvId"
          :is-empty="isEmpty"
          :busy="busy"
          @switch="$emit('switch', $event)"
          @delete="$emit('delete', $event)"
        />
      </div>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import type { ConvGroup } from '@/types/chat'
import HistoryConversationList from '@/components/chat/HistoryConversationList.vue'

defineProps<{
  groups: ConvGroup[]
  currentConvId: string | null
  isEmpty: boolean
  busy?: boolean
}>()

const emit = defineEmits<{
  close: []
  switch: [id: string]
  delete: [id: string]
}>()

const asideRef = ref<HTMLElement | null>(null)
const closeBtn = ref<HTMLButtonElement | null>(null)

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function focusables(): HTMLElement[] {
  return asideRef.value
    ? Array.from(asideRef.value.querySelectorAll<HTMLElement>(FOCUSABLE))
    : []
}

/** Tab 在抽屉内循环：焦点漏到背后的消息区或标签条，就丢了「模态」这层语义。 */
function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Tab') return
  const items = focusables()
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

function onWindowKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.stopPropagation()
    emit('close')
  }
}

onMounted(() => {
  void nextTick(() => closeBtn.value?.focus())
  window.addEventListener('keydown', onWindowKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onWindowKeydown)
})
</script>

<style scoped>
/* 只盖消息区：宿主 HomeWorkspace 的 .home-main 是定位容器，composer 在它之外。 */
.home-history-drawer {
  position: absolute;
  inset: 0;
  z-index: 20;
}

.drawer-mask {
  position: absolute;
  inset: 0;
  background: color-mix(in srgb, var(--bg-terminal) 34%, transparent);
}

.drawer {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(320px, 80vw);
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border-default);
  background: var(--bg-elevated-solid);
  box-shadow: var(--shadow-lg);
}

.drawer:focus {
  outline: none;
}

.drawer-head {
  flex: 0 0 auto;
  height: 42px;
  display: flex;
  align-items: center;
  padding: 0 12px;
  border-bottom: 1px solid var(--border-subtle);
}

.drawer-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.drawer-close {
  margin-left: auto;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}

.drawer-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.drawer-close svg {
  width: 13px;
  height: 13px;
}

.drawer-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 8px 14px;
}
</style>
