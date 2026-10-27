<template>
  <Teleport to="body">
    <div class="hd-overlay" @click.self="$emit('close')">
      <div
        id="home-history-dialog"
        ref="dialogRef"
        class="hd-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="home-history-title"
        tabindex="-1"
      >
        <div class="hd-head">
          <h2 id="home-history-title" class="hd-title">历史记录</h2>
          <button type="button" class="hd-close" title="关闭" aria-label="关闭" @click="$emit('close')">×</button>
        </div>
        <div class="hd-list">
          <HistoryConversationList
            :groups="groups"
            :current-conv-id="currentConvId"
            :is-empty="isEmpty"
            :busy="busy"
            @switch="$emit('switch', $event)"
            @delete="$emit('delete', $event)"
          />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'
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

const dialogRef = ref<HTMLElement | null>(null)

// Tab 环形与「关掉后焦点还给打开它的按钮」走共享件。原先这两件写在本组件里，
// 且 Tab 监听绑在容器上 —— 焦点一旦漏到背后的任务台（点遮罩、点别处）就再也回不来。
const { activate, deactivate } = useModalFocusTrap(dialogRef, { initialFocus: '.hd-close' })

function onWindowKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.stopPropagation()
    emit('close')
  }
}

onMounted(() => {
  activate()
  window.addEventListener('keydown', onWindowKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onWindowKeydown)
  deactivate()
})
</script>

<style scoped>
/* 居中弹窗：Teleport 到 body + fixed 遮罩。历史记录不再占用任务台右栏 ——
   抽屉时代的「给主区/输入区预留宽度」全部作废，布局由宿主正常铺满。 */
.hd-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-24);
  background: var(--scrim);
  backdrop-filter: blur(2px);
  animation: hd-fade-in 140ms ease-out;
}

.hd-dialog {
  width: min(560px, calc(100vw - 48px));
  max-height: min(640px, calc(100vh - 96px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  /* 浮起层走 --bg-elevated 而非 -solid：渐变开启时 -solid 是派生源（永远不透明），
     用它会让弹窗绕开主题 tint —— 其他浮层都随主题染色，就它纹丝不动。 */
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-lg);
  outline: none;
  animation: hd-dialog-in 170ms cubic-bezier(0.22, 1, 0.36, 1);
}

.hd-head {
  flex: 0 0 auto;
  height: 48px;
  display: flex;
  align-items: center;
  padding: 0 var(--spacing-14);
  border-bottom: 1px solid var(--border-subtle);
}

.hd-title {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
  color: var(--text-primary);
}

.hd-close {
  margin-left: auto;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 1.3846rem;
  line-height: 1;
}

.hd-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.hd-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--spacing-6) var(--spacing-10) var(--spacing-12);
}

/* 入场：遮罩淡入、弹窗从 0.96 放大到 1。与设置弹窗同一套过渡语言。 */
@keyframes hd-fade-in {
  from { opacity: 0; }
}

@keyframes hd-dialog-in {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
}

@media (prefers-reduced-motion: reduce) {
  .hd-overlay,
  .hd-dialog {
    animation: none;
  }
}
</style>
