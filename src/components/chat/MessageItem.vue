<template>
  <div class="message-block" :class="message.role">
    <!-- 历史消息固化的 Agent 执行过程前置块（与 ChatMessages.vue 顺序一致） -->
    <AgentTraceInline
      v-if="message.role === 'assistant' && message.agentTrace"
      :trace="message.agentTrace"
      :is-live="false"
    />

    <ThinkingBlock
      v-if="reasoning"
      :reasoning="reasoning"
      :streaming="isStreaming && isLast"
      :default-open="thinkingDefaultOpen"
    />

    <div class="message-bubble">
      <MessageContentRenderer
        :role="message.role"
        :content="content"
        :streaming="isStreaming"
        :is-last="isLast"
      />
    </div>

    <div v-if="content" class="message-actions">
      <button class="action-btn" @click="copyContent(content)" title="复制">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        <span>复制</span>
      </button>
      <slot name="actions" :content="content" />
      <span v-if="duration && message.role === 'assistant'" class="action-elapsed">耗时 {{ duration }}s</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ThinkingBlock from './ThinkingBlock.vue'
import MessageContentRenderer from './MessageContentRenderer.vue'
import AgentTraceInline from '@/components/agent/AgentTraceInline.vue'
import type { AgentTraceSnapshot } from '@/api/client'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content?: string
  reasoning?: string
  streamingContent?: string
  streamingReasoning?: string
  duration?: number
  agentTrace?: AgentTraceSnapshot
}

const props = defineProps<{
  message: ChatMessage
  isStreaming?: boolean
  isLast?: boolean
}>()

const content = computed(() => props.message.streamingContent || props.message.content || '')
const reasoning = computed(() => props.message.streamingReasoning || props.message.reasoning || '')
const duration = computed(() => props.message.duration)
// 非流式时不传 defaultOpen，让 ThinkingBlock 回退到 settings.chat.thinkingExpanded
const thinkingDefaultOpen = computed(() => {
  if (props.isStreaming && props.isLast) return true
  return undefined
})

function copyContent(text: string) {
  navigator.clipboard.writeText(text).catch(() => {})
}
</script>

<style scoped>
.message-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 0;
}

.message-block.user {
  align-items: flex-end;
}

.message-block.assistant {
  align-items: flex-start;
}

.message-bubble {
  max-width: 92%;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 14px;
  line-height: 1.6;
}

.message-block.user .message-bubble {
  width: 100%;
  max-width: 100%;
  padding: 4px 14px;
  background: var(--bg-active);
  color: var(--text-primary);
  border: 1px solid var(--border-default);
  border-radius: 12px;
}

.message-block.assistant .message-bubble {
  width: 100%;
  max-width: 100%;
  background: transparent;
  color: var(--text-primary);
  padding-left: 0;
}

.thinking-dots {
  display: flex;
  gap: 4px;
  padding: 8px 0;
}

.thinking-dots span {
  width: 6px;
  height: 6px;
  background: #818cf8;
  border-radius: 50%;
  animation: blink 1.4s infinite both;
}

.thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.4s; }

@keyframes blink {
  0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
  40% { opacity: 1; transform: scale(1); }
}

.message-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  opacity: 0;
  transition: opacity 0.15s;
  padding-left: 2px;
}

.message-block:hover .message-actions,
.message-actions:focus-within {
  opacity: 1;
}

.action-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.1s;
}

.action-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.action-elapsed {
  color: var(--text-tertiary);
  font-size: 11px;
  margin-left: auto;
}
</style>
