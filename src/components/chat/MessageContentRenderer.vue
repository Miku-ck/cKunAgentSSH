<template>
  <div class="message-content">
    <!-- 澄清待答：后端已停在闸口、没有 token 在产出。说"在等你"，
         不画呼吸点——那三个跳动的点在说"AI 正在输出"（用户报的问题）。 -->
    <div v-if="showAwaitingHint" class="awaiting-hint">
      <span class="awaiting-dot" aria-hidden="true"></span>
      <span>等待你确认意图，AI 已暂停…</span>
    </div>
    <div
      v-else-if="showThinkingDots"
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
  /** run 停在澄清闸口等用户作答：AI 没有在输出，画等待提示而非呼吸点 */
  awaitingUser?: boolean
}>()

const settingsStore = useSettingsStore()
const markdownEnabled = computed(() => settingsStore.settings.chat.markdown)
const effectiveContent = computed(() => props.content || '')

const showThinkingDots = computed(() => {
  return props.streaming && props.isLast && props.role === 'assistant'
    && !effectiveContent.value && !props.awaitingUser
})

/** 澄清待答且本轮还没有正文：一句话讲清"在等谁"，别让气泡空着 */
const showAwaitingHint = computed(() => {
  return props.awaitingUser && props.isLast && props.role === 'assistant'
    && !effectiveContent.value
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

/* 澄清待答提示：静态一句话 + 慢呼吸的点（与呼吸点同色系但更慢、更静） */
.awaiting-hint {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 0;
  font-size: 12.5px;
  color: var(--text-tertiary);
}
.awaiting-dot {
  flex: none;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--status-warn-text);
  animation: clarify-pulse 1.8s ease-in-out infinite;
}
/* 名字不能以 await 开头（如 await-pulse）：vue-tsc 生成的虚拟代码里
   await-pulse 会被 TS 当成 await 关键字，解析器直接 Debug Failure 崩溃 */
@keyframes clarify-pulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .awaiting-dot { animation: none; opacity: 0.75; }
}
</style>
