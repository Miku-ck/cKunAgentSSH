<template>
  <Teleport to="body">
    <Transition name="confirm-fade">
      <div v-if="visible" class="confirm-overlay" @click.self="handleCancel">
        <div class="confirm-dialog" :class="{ danger }" role="alertdialog" aria-modal="true" aria-labelledby="confirm-dialog-title">
          <div class="confirm-header">
            <div class="confirm-icon" :class="{ danger }">
              <svg v-if="danger" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="8" x2="12" y2="12"/>
                <line x1="12" y1="16" x2="12.01" y2="16"/>
              </svg>
              <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
            </div>
            <h3 id="confirm-dialog-title" class="confirm-title">{{ title }}</h3>
          </div>

          <div class="confirm-body">
            <p class="confirm-message">{{ message }}</p>
          </div>

          <div class="confirm-footer">
            <button class="confirm-btn cancel" @click="handleCancel">
              {{ cancelText }}
            </button>
            <button ref="confirmBtnRef" class="confirm-btn ok" :class="{ danger }" @click="handleConfirm">
              {{ confirmText }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, nextTick, watch, onUnmounted } from 'vue'

interface ConfirmOptions {
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}

const visible = ref(false)
const title = ref('确认')
const message = ref('')
const confirmText = ref('确定')
const cancelText = ref('取消')
const danger = ref(false)
const confirmBtnRef = ref<HTMLButtonElement | null>(null)

let resolvePromise: ((value: boolean) => void) | null = null

/** 打开确认框,返回 Promise<boolean> */
function open(options: ConfirmOptions): Promise<boolean> {
  // B18 修复：并发 open 时原实现直接覆盖 resolvePromise —
  // 前一次 await 永远无人结算而永久挂起。新请求打开前先把旧 resolve 以 false 结算
  if (resolvePromise) {
    const prev = resolvePromise
    resolvePromise = null
    prev(false)
  }
  title.value = options.title ?? '确认'
  message.value = options.message
  confirmText.value = options.confirmText ?? '确定'
  cancelText.value = options.cancelText ?? '取消'
  danger.value = options.danger ?? false
  visible.value = true

  return new Promise((resolve) => {
    resolvePromise = resolve
  })
}

function close(result: boolean) {
  visible.value = false
  if (resolvePromise) {
    resolvePromise(result)
    resolvePromise = null
  }
}

function handleConfirm() {
  close(true)
}

function handleCancel() {
  close(false)
}

function handleKeydown(e: KeyboardEvent) {
  if (!visible.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    handleCancel()
  } else if (e.key === 'Enter') {
    e.preventDefault()
    e.stopPropagation()
    handleConfirm()
  }
}

watch(visible, (v) => {
  if (v) {
    window.addEventListener('keydown', handleKeydown, true)
    nextTick(() => confirmBtnRef.value?.focus())
  } else {
    window.removeEventListener('keydown', handleKeydown, true)
  }
})

// 组件卸载时确保 keydown 监听器被清理
onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown, true)
})

defineExpose({ open, close })
</script>

<style scoped>
.confirm-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 3000;
}

.confirm-dialog {
  width: 380px;
  max-width: calc(100vw - 32px);
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  font-family: var(--font-sans);
  color: var(--text-primary);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.confirm-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 20px 12px;
}

.confirm-icon {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
  color: var(--accent-text);
  display: flex;
  align-items: center;
  justify-content: center;
}

.confirm-icon.danger {
  background: var(--danger-bg);
  color: var(--danger-text);
}

.confirm-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.4;
}

.confirm-body {
  padding: 0 20px 16px;
}

.confirm-message {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
  word-wrap: break-word;
  white-space: pre-wrap;
}

.confirm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px 16px;
  border-top: 1px solid var(--border-subtle);
  /* 压暗卡脚用画布自己的色，而不是 rgba(0,0,0,.2)：深色主题下两者同值，
     浅/手记主题下黑色 20% 会把白卡脚糊成灰渍 */
  background: color-mix(in srgb, var(--bg-base) 20%, transparent);
}

.confirm-btn {
  height: 30px;
  min-width: 72px;
  padding: 0 14px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s ease;
  outline: none;
}

.confirm-btn.cancel {
  background: transparent;
  color: var(--text-secondary);
}

.confirm-btn.cancel:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
  border-color: var(--border-default);
}

.confirm-btn.ok {
  background: var(--accent-primary);
  color: var(--accent-on);
  border-color: var(--accent-primary);
}

.confirm-btn.ok:hover {
  /* 同一枚强调色的浅一档，而不是写死的 #4a8fe3：强调色选绿/橙/朱砂时，
     主按钮一 hover 就跳成蓝 —— 非 hover 态本来读 --accent-primary */
  background: color-mix(in srgb, var(--accent-primary) 85%, white);
  border-color: color-mix(in srgb, var(--accent-primary) 85%, white);
}

.confirm-btn.ok:focus {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent-primary) 40%, transparent);
}

/* 危险档沿用审批卡的三件套（--danger-bg/-border/-text）：这套令牌在浅色与手记
   都有转浅档，写死 #f85149 时白字压在浅主题的红上只有 2.4:1 */
.confirm-btn.ok.danger {
  background: var(--danger-bg);
  color: var(--danger-text);
  border-color: var(--danger-border);
}

.confirm-btn.ok.danger:hover {
  background: var(--danger-border);
  border-color: var(--danger-border);
}

.confirm-btn.ok.danger:focus {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--danger-text) 40%, transparent);
}

/* 过渡动画 */
.confirm-fade-enter-active,
.confirm-fade-leave-active {
  transition: opacity 0.15s ease;
}

.confirm-fade-enter-active .confirm-dialog,
.confirm-fade-leave-active .confirm-dialog {
  transition: transform 0.15s ease, opacity 0.15s ease;
}

.confirm-fade-enter-from,
.confirm-fade-leave-to {
  opacity: 0;
}

.confirm-fade-enter-from .confirm-dialog,
.confirm-fade-leave-to .confirm-dialog {
  transform: scale(0.96);
  opacity: 0;
}
</style>
