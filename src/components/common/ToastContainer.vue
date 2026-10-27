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
  /* 必须压在一切浮层之上：原先 2000，和 BaseModal 同层，于是被确认框（3000）、
     模型弹窗（2100）、页面右上几个 9999 浮层与命令面板（10000）盖住 —— 那些
     场景里发错的提示等于没提示，用户会以为操作没生效。 */
  z-index: 11000;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-8);
  pointer-events: none;
}

.toast-item {
  min-width: 240px;
  max-width: 480px;
  padding: var(--spacing-10) var(--spacing-14);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  box-shadow: var(--shadow-lg);
  cursor: pointer;
  pointer-events: auto;
  font-size: 1rem;
  color: var(--text-primary);
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
  gap: var(--spacing-10);
}

.toast-icon {
  font-size: 1.0769rem;
  font-weight: 600;
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
}

.toast-message {
  flex: 1;
  line-height: 1.4;
}

.toast-expand-hint {
  font-size: 0.8462rem;
  color: var(--text-secondary);
  flex-shrink: 0;
  opacity: 0.7;
}

.toast-details {
  margin-top: var(--spacing-8);
  padding-top: var(--spacing-8);
  border-top: 1px dashed var(--border-subtle);
}

.toast-details-text {
  margin: 0;
  padding: var(--spacing-8) var(--spacing-10);
  /* 凹陷块走 --bg-input（同 approval-card 的 .ap-code）：rgba(0,0,0,.3) 在深色卡上
     是对的，在浅色主题的白卡上就是一块灰渍，与主题的层级约定无关地变暗 */
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.8462rem;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}

.toast-actions {
  display: flex;
  gap: var(--spacing-8);
  margin-top: var(--spacing-8);
  padding-top: var(--spacing-8);
  border-top: 1px solid var(--border-subtle);
}

.toast-action-btn {
  flex: 1;
  padding: 5px var(--spacing-10);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-primary);
  font-size: 0.9231rem;
  cursor: pointer;
  transition: all 0.15s ease;
}

.toast-action-btn:hover {
  background: var(--bg-hover);
}

.toast-action-btn.dismiss {
  opacity: 0.6;
}

/* 四档状态色一律读主题令牌：#3fb950/#f85149/#58a6ff/#d29922 是终端 ANSI 色板
   （见 useTerminalConfig.TERMINAL_DARK_THEME），它按约定固定暗色、不随主题走，
   搬到浅色/手记的 toast 上就是白纸上糊一片。 */
.toast-success { border-left: 3px solid var(--status-success-text); }
.toast-success .toast-icon { background: color-mix(in srgb, var(--status-success-text) 20%, transparent); color: var(--status-success-text); }

.toast-error { border-left: 3px solid var(--danger-text); }
.toast-error .toast-icon { background: color-mix(in srgb, var(--danger-text) 20%, transparent); color: var(--danger-text); }

.toast-info { border-left: 3px solid var(--status-info-text); }
.toast-info .toast-icon { background: color-mix(in srgb, var(--status-info-text) 20%, transparent); color: var(--status-info-text); }

.toast-warning { border-left: 3px solid var(--status-warn-text); }
.toast-warning .toast-icon { background: color-mix(in srgb, var(--status-warn-text) 20%, transparent); color: var(--status-warn-text); }

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
