<template>
  <Teleport to="body">
    <Transition name="confirm-fade">
      <div v-if="visible" class="confirm-overlay" @click.self="handleCancel">
        <div ref="dialogRef" class="confirm-dialog" :class="{ danger }" role="alertdialog" aria-modal="true" aria-labelledby="confirm-dialog-title" tabindex="-1">
          <div class="confirm-header">
            <h3 id="confirm-dialog-title" class="confirm-title">{{ title }}</h3>
          </div>

          <div class="confirm-body">
            <p class="confirm-message">{{ message }}</p>
          </div>

          <div class="confirm-footer">
            <button class="confirm-btn cancel" @click="handleCancel">
              {{ cancelText }}
            </button>
            <button class="confirm-btn ok" :class="{ danger }" @click="handleConfirm">
              {{ confirmText }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'

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
const dialogRef = ref<HTMLElement | null>(null)
// 确认框默认把焦点放在主按钮上（回车即确认）；关闭后焦点自动还给触发器
const { activate, deactivate } = useModalFocusTrap(dialogRef, { initialFocus: '.confirm-btn.ok' })

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
    activate()
  } else {
    window.removeEventListener('keydown', handleKeydown, true)
    deactivate()
  }
})

// 组件卸载时确保 keydown 监听器被清理
onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown, true)
  deactivate()
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
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  font-family: var(--font-sans);
  color: var(--text-primary);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  /* 容器只作为初始焦点落点（tabindex=-1），描边交给里面的按钮 */
  outline: none;
}

.confirm-header {
  display: flex;
  align-items: center;
  padding: 18px var(--spacing-20) var(--spacing-12);
}

.confirm-title {
  margin: 0;
  font-size: 1.0769rem;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.4;
}

.confirm-body {
  padding: 0 var(--spacing-20) var(--spacing-16);
}

.confirm-message {
  margin: 0;
  font-size: 1rem;
  line-height: 1.6;
  color: var(--text-secondary);
  word-wrap: break-word;
  white-space: pre-wrap;
}

.confirm-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-8);
  padding: var(--spacing-12) var(--spacing-20) var(--spacing-16);
  border-top: 1px solid var(--border-subtle);
  /* 压暗卡脚用画布自己的色，而不是 rgba(0,0,0,.2)：深色主题下两者同值，
     浅/手记主题下黑色 20% 会把白卡脚糊成灰渍 */
  background: color-mix(in srgb, var(--bg-base) 20%, transparent);
}

/* 按钮统一走项目规范 7.3 的文字链接风格（无边框/背景，颜色即语义），
   与 .btn / .btn-danger 同源；实心背景在浅主题下会糊成一块粉，
   深色主题下又和遮罩抢层次。 */
.confirm-btn {
  height: 30px;
  min-width: 72px;
  padding: 0 var(--spacing-14);
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  font-size: 0.9231rem;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s ease;
  outline: none;
}

.confirm-btn.cancel {
  color: var(--text-secondary);
}

.confirm-btn.cancel:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.confirm-btn.ok {
  color: var(--accent-primary);
  font-weight: 600;
}

.confirm-btn.ok:hover {
  background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
}

.confirm-btn.ok:focus {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent-primary) 40%, transparent);
}

/* 危险档用规范 7.4 的危险实色 --color-danger（与 .btn-danger 同源），
   不再走审批卡的 --danger-bg 浅粉底 —— 那档是为「风险徽章」设计的，
   当按钮底在白卡上几乎读不出危险 */
.confirm-btn.ok.danger {
  color: var(--color-danger);
}

.confirm-btn.ok.danger:hover {
  background: color-mix(in srgb, var(--color-danger) 10%, transparent);
}

.confirm-btn.ok.danger:focus {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-danger) 40%, transparent);
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
