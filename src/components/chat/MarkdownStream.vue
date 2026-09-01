<template>
  <div class="markdown-body" v-html="renderedHtml" @click="handleClick"></div>
</template>

<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { renderMarkdown } from '@/utils/markdown'
import { useToastStore } from '@/stores/toast'

const props = defineProps<{
  content: string
}>()

const toast = useToastStore()

// C34 修复：流式场景下原 computed 每个 token 都全量 renderMarkdown（整条消息
// 逐 token 重渲染）— 改为 150ms 尾沿节流：中间变更只重置定时器，最后一次必须 flush
const RENDER_THROTTLE_MS = 150
const renderedHtml = ref(renderMarkdown(props.content || ''))
let _renderTimer: ReturnType<typeof setTimeout> | null = null

function scheduleRender(): void {
  if (_renderTimer !== null) return // 已有待执行的 flush，尾沿节流复用同一 timer
  _renderTimer = setTimeout(flushRender, RENDER_THROTTLE_MS)
}

function flushRender(): void {
  _renderTimer = null
  renderedHtml.value = renderMarkdown(props.content || '')
}

watch(() => props.content, (v) => {
  if (v === '') return
  scheduleRender()
})

// 组件卸载时清理未触发的定时器（卸载后内容不再变化，无需补 flush）
onUnmounted(() => {
  if (_renderTimer !== null) {
    clearTimeout(_renderTimer)
    _renderTimer = null
  }
})

/** 事件委托：点击代码块复制按钮时复制代码到剪贴板 */
function handleClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.classList.contains('code-copy-btn')) return
  const wrapper = target.closest('.code-block-wrapper')
  const codeEl = wrapper?.querySelector('code')
  if (!codeEl) return
  const text = codeEl.textContent || ''
  navigator.clipboard.writeText(text).then(() => {
    target.textContent = '已复制'
    setTimeout(() => { target.textContent = '复制' }, 1500)
  }).catch(() => {
    toast.error('复制失败，请手动选择文本复制')
  })
}
</script>

<style scoped>
.markdown-body {
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-primary);
}

.markdown-body :deep(p) {
  margin: 0.6em 0;
}

.markdown-body :deep(.code-block-wrapper) {
  position: relative;
  margin: 0.8em 0;
  border: 1px solid var(--border-subtle, #2d2d30);
  border-radius: 6px;
  overflow: hidden;
}

.markdown-body :deep(.code-block-header) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 12px;
  background: var(--bg-base);
  border-bottom: 1px solid var(--border-subtle, #2d2d30);
  height: 28px;
}

.markdown-body :deep(.code-lang) {
  font-size: 11px;
  color: var(--text-secondary);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  text-transform: lowercase;
}

.markdown-body :deep(.code-copy-btn) {
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: var(--font-sans);
  transition: all 0.15s;
}

.markdown-body :deep(.code-copy-btn:hover) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.markdown-body :deep(.code-block-wrapper pre) {
  margin: 0;
  border: none;
  border-radius: 0;
}

.markdown-body :deep(pre) {
  background: var(--bg-base);
  border: 1px solid var(--border-subtle, #2d2d30);
  border-radius: 6px;
  padding: 12px;
  overflow-x: auto;
  margin: 0.8em 0;
}

.markdown-body :deep(code) {
  font-family: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, "Courier New", monospace;
  font-size: 12px;
}

.markdown-body :deep(pre code) {
  background: transparent;
  padding: 0;
  border: none;
  color: var(--text-primary);
}

.markdown-body :deep(:not(pre) > code) {
  background: var(--bg-hover);
  padding: 2px 5px;
  border-radius: 4px;
  color: var(--text-primary);
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  padding-left: 1.6em;
  margin: 0.6em 0;
}

.markdown-body :deep(li) {
  margin: 0.25em 0;
}

.markdown-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 0.8em 0;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--border-subtle, #2d2d30);
  padding: 6px 10px;
  text-align: left;
}

.markdown-body :deep(th) {
  background: var(--bg-hover);
}
</style>
