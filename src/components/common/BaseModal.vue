<!--
  BaseModal —— 居中弹窗共享骨架（对齐 HomeHistoryDialog 的模态标准）
  Teleport to body + role=dialog/aria-modal/aria-labelledby + Tab 焦点陷阱 +
  Esc 关闭 + 卸载还原焦点；遮罩读 --scrim（随主题染色）+ blur(2px)，弹窗走
  --radius-lg / --shadow-lg；头部固定、body 独立滚动（长内容不会把底部操作滚走）。

  用法：父级用 v-if 控制挂载，标题走 title prop 或 #title slot，
  右侧头部附加（如类型开关）走 #head-right，底部按钮走 #actions。
-->
<template>
  <Teleport to="body">
    <div class="bm-overlay" @click.self="emit('close')">
      <div
        ref="dialogRef"
        class="bm-dialog"
        :class="{ 'is-wide': wide }"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        tabindex="-1"
      >
        <div class="bm-head">
          <h2 :id="titleId" class="bm-title"><slot name="title">{{ title }}</slot></h2>
          <slot name="head-right" />
        </div>
        <div class="bm-body"><slot /></div>
        <div class="bm-actions" v-if="$slots.actions"><slot name="actions" /></div>
      </div>
    </div>
  </Teleport>
</template>

<script lang="ts">
// 模块级计数器：<script setup> 顶层是每实例作用域，放这里才能保证标题 id 唯一
let _bmUid = 0
</script>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'

defineProps<{
  title?: string
  /** 宽弹窗（执行历史这类多列内容） */
  wide?: boolean
}>()

const emit = defineEmits<{ close: [] }>()

const titleId = `bm-title-${++_bmUid}`

const dialogRef = ref<HTMLElement | null>(null)
// Tab 环形与焦点还原走共享件：自绘 overlay 的老弹窗也用同一份，行为不会两边跑偏
const { activate, deactivate } = useModalFocusTrap(dialogRef)

function onWindowKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  e.stopPropagation()
  emit('close')
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
.bm-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-24);
  background: var(--scrim);
  backdrop-filter: blur(2px);
  animation: bm-fade-in 140ms ease-out;
}

.bm-dialog {
  width: 520px;
  max-width: 100%;
  max-height: min(80vh, calc(100vh - 96px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  outline: none;
  animation: bm-dialog-in 170ms cubic-bezier(0.22, 1, 0.36, 1);
}

.bm-dialog.is-wide {
  width: 680px;
}

.bm-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-10);
  min-height: 44px;
  padding: var(--spacing-10) var(--spacing-16);
  border-bottom: 1px solid var(--border-subtle);
}

.bm-title {
  margin: 0;
  min-width: 0;
  font-size: 1.0769rem;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bm-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--spacing-16);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-10);
}

.bm-actions {
  flex: 0 0 auto;
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-8);
  padding: var(--spacing-12) var(--spacing-16);
  border-top: 1px solid var(--border-subtle);
}

@keyframes bm-fade-in {
  from { opacity: 0; }
}

@keyframes bm-dialog-in {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
}

@media (prefers-reduced-motion: reduce) {
  .bm-overlay,
  .bm-dialog {
    animation: none;
  }
}
</style>
