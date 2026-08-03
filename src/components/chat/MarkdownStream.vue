<template>
  <div class="markdown-body" v-html="renderedHtml" @click="handleClick"></div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{
  content: string
}>()

const renderedHtml = computed(() => renderMarkdown(props.content || ''))

/** 事件委托：点击代码块复制按钮时复制代码到剪贴板 */
function handleClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.classList.contains('code-copy-btn')) return
  const wrapper = target.closest('.code-block-wrapper')
  const codeEl = wrapper?.querySelector('code')
  if (!codeEl) return
  const text = codeEl.textContent || ''
  navigator.clipboard.writeText(text).catch(() => {})
  target.textContent = '已复制'
  setTimeout(() => { target.textContent = '复制' }, 1500)
}
</script>

<style scoped>
.markdown-body {
  font-size: 13px;
  line-height: 1.7;
  color: #d4d4d4;
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
  color: #8b949e;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  text-transform: lowercase;
}

.markdown-body :deep(.code-copy-btn) {
  border: none;
  background: transparent;
  color: #8b949e;
  font-size: 11px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: var(--font-sans);
  transition: all 0.15s;
}

.markdown-body :deep(.code-copy-btn:hover) {
  background: rgba(255, 255, 255, 0.08);
  color: #e6edf3;
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
  color: #d4d4d4;
}

.markdown-body :deep(:not(pre) > code) {
  background: rgba(255, 255, 255, 0.08);
  padding: 2px 5px;
  border-radius: 4px;
  color: #e6e6e6;
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
  background: rgba(255, 255, 255, 0.05);
}
</style>
