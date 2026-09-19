<template>
  <Teleport to="body">
    <TransitionGroup name="toast" tag="div" class="toast-container">
      <div
        v-for="item in toast.items"
        :key="item.id"
        class="toast-item"
        :class="[`toast-${item.type}`, { 'has-details': !!item.details, 'expanded': item.expanded }]"
      >
        <div class="toast-main" @click="item.details ? toast.toggleDetails(item.id) : toast.dismiss(item.id)">
          <span class="toast-icon">{{ iconMap[item.type] }}</span>
          <span class="toast-message">{{ item.message }}</span>
          <span v-if="item.details" class="toast-expand-hint">{{ item.expanded ? '▴ 收起' : '▾ 详情' }}</span>
        </div>
        <div v-if="item.expanded && item.details" class="toast-details">
          <pre class="toast-details-text">{{ item.details }}</pre>
        </div>
        <div v-if="item.actions?.length" class="toast-actions">
          <button
            v-for="action in item.actions"
            :key="action.label"
            class="toast-action-btn"
            @click="action.onClick"
          >
            {{ action.label }}
          </button>
          <button class="toast-action-btn dismiss" @click="toast.dismiss(item.id)">关闭</button>
        </div>
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
  /* 居中显示且下移：避开标题栏，下移 60px 让提示不遮挡标题栏 */
  top: 60px;
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
  max-width: 480px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--bg-elevated-solid, #1e1e1e);
  border: 1px solid var(--border-default);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  cursor: pointer;
  pointer-events: auto;
  font-size: 13px;
  color: var(--text-primary, #e0e0e0);
  font-family: var(--font-sans);
  overflow: hidden;
  transition: all 0.2s ease;
}

.has-details {
  cursor: pointer;
}

.toast-main {
  display: flex;
  align-items: center;
  gap: 10px;
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

.toast-message {
  flex: 1;
  line-height: 1.4;
}

.toast-expand-hint {
  font-size: 11px;
  color: var(--text-secondary);
  flex-shrink: 0;
  opacity: 0.7;
}

.toast-details {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--border-subtle);
}

.toast-details-text {
  margin: 0;
  padding: 8px 10px;
  background: rgba(0, 0, 0, 0.3);
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}

.toast-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
}

.toast-action-btn {
  flex: 1;
  padding: 5px 10px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.toast-action-btn:hover {
  background: rgba(255, 255, 255, 0.08);
}

.toast-action-btn.dismiss {
  opacity: 0.6;
}

.toast-success { border-left: 3px solid #3fb950; }
.toast-success .toast-icon { background: rgba(63, 185, 80, 0.2); color: #3fb950; }

.toast-error { border-left: 3px solid #f85149; }
.toast-error .toast-icon { background: rgba(248, 81, 73, 0.2); color: #f85149; }

.toast-info { border-left: 3px solid #58a6ff; }
.toast-info .toast-icon { background: rgba(88, 166, 255, 0.2); color: #58a6ff; }

.toast-warning { border-left: 3px solid #d29922; }
.toast-warning .toast-icon { background: rgba(210, 153, 34, 0.2); color: #d29922; }

/* 过渡动画 */
.toast-enter-active { animation: toast-in 0.25s ease; }
.toast-leave-active { animation: toast-out 0.2s ease; }
@keyframes toast-in {
  from { opacity: 0; transform: translateY(-8px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes toast-out {
  from { opacity: 1; transform: translateY(0); }
  to { opacity: 0; transform: translateY(-8px); }
}
</style>
