<template>
  <div class="message-content">
    <div
      v-if="showThinkingDots"
      class="thinking-dots"
    >
      <span></span><span></span><span></span>
    </div>
    <MarkdownStream v-else-if="effectiveContent && markdownEnabled" :content="effectiveContent" />
    <pre v-else-if="effectiveContent" class="raw-text">{{ effectiveContent }}</pre>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import MarkdownStream from './MarkdownStream.vue'

const props = defineProps<{
  role: string
  content?: string
  streaming?: boolean
  isLast?: boolean
}>()

const settingsStore = useSettingsStore()
const markdownEnabled = computed(() => settingsStore.settings.chat.markdown)
const effectiveContent = computed(() => props.content || '')

const showThinkingDots = computed(() => {
  return props.streaming && props.isLast && props.role === 'assistant' && !effectiveContent.value
})
</script>

<style scoped>
.message-content {
  width: 100%;
  line-height: 1.7;
  /* 这条若不跟着变量走，它就成了气泡里最后说话的那一层：
     .message-bubble 改了字号，正文仍是裸值。回落与 MessageItem/MarkdownStream 同一档
     （store 默认 13），三处各写一个数就是注入前那一帧的字号打架。 */
  font-size: var(--chat-font-size, 13px);
  color: var(--text-primary);
  text-align: left;
}

/* 纯文本回退（markdown 渲染关闭时） */
.raw-text {
  margin: 0;
  padding: 0;
  font-family: var(--font-sans);
  font-size: inherit;
  line-height: inherit;
  color: inherit;
  white-space: pre-wrap;
  word-break: break-word;
  background: transparent;
}

/* 加载动画 */
.thinking-dots {
  display: flex;
  gap: 4px;
  padding: 8px 0;
}
.thinking-dots span {
  width: 7px; height: 7px;
  background: var(--text-tertiary);
  border-radius: 50%;
  animation: bounce 1.2s infinite ease-in-out;
}
.thinking-dots span:nth-child(1) { animation-delay: 0s; }
.thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.4s; }

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .thinking-dots span { animation: none; opacity: 0.7; }
}
</style>
