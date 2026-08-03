<template>
  <Teleport to="body">
    <TransitionGroup name="toast" tag="div" class="toast-container">
      <div
        v-for="item in toast.items"
        :key="item.id"
        class="toast-item"
        :class="`toast-${item.type}`"
        @click="toast.dismiss(item.id)"
      >
        <span class="toast-icon">{{ iconMap[item.type] }}</span>
        <span class="toast-message">{{ item.message }}</span>
      </div>
    </TransitionGroup>
  </Teleport>
</template>

<script setup lang="ts">
import { useToastStore, type ToastType } from '@/stores/toast'

const toast = useToastStore()

const iconMap: Record<ToastType, string> = {
  success: '✓',
  error: '✕',
  info: 'ℹ',
  warning: '⚠',
}
</script>

<style scoped>
.toast-container {
  position: fixed;
  /* 中间显示且下移：避右上角，下移 80px 让提示不卡顶部遮挡标题栏 */
  top: 80px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
}

.toast-item {
  min-width: 240px;
  max-width: 420px;
  padding: 10px 14px;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: #e0e0e0;
  cursor: pointer;
  pointer-events: auto;
  font-family: var(--font-sans);
}

.toast-icon {
  font-size: 14px;
  font-weight: 600;
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: 'Consolas', monospace;
}

.toast-success { border-left: 3px solid #3fb950; }
.toast-success .toast-icon { background: rgba(63, 185, 80, 0.2); color: #3fb950; }

.toast-error { border-left: 3px solid #f85149; }
.toast-error .toast-icon { background: rgba(248, 81, 73, 0.2); color: #f85149; }

.toast-info { border-left: 3px solid #3B82F6; }
.toast-info .toast-icon { background: rgba(59, 130, 246, 0.2); color: #3B82F6; }

.toast-warning { border-left: 3px solid #F59E0B; }
.toast-warning .toast-icon { background: rgba(245, 158, 11, 0.2); color: #F59E0B; }

.toast-message {
  flex: 1;
  line-height: 1.4;
  word-break: break-word;
}

/* 过渡 — 用 opacity 避免 translateX 覆盖容器的 translateX(-50%) 居中变换 */
.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.25s ease;
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
}
</style>
