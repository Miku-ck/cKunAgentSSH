<template>
  <div class="message-block" :class="message.role">
    <!-- 历史消息固化的 Agent 执行过程前置块（与运行中实时轨迹同一渲染顺序） -->
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

    <div v-if="content || hasTraceAction" class="message-actions" :class="{ 'actions-open': traceOpen }">
      <button v-if="content" class="action-btn" @click="copyContent(content)" title="复制">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        <span>复制</span>
      </button>
      <!-- 会话内轨迹回放：紧跟复制，仅固化后的 Agent 运行（有 run_id）可见 -->
      <AgentFullTrace
        v-if="hasTraceAction"
        class="action-trace"
        :run-id="message.agentTrace!.lastRunId"
        @expand-change="traceOpen = $event"
      />
      <slot v-if="content" name="actions" :content="content" />
      <span v-if="duration && message.role === 'assistant'" class="action-elapsed">耗时 {{ duration }}s</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import ThinkingBlock from './ThinkingBlock.vue'
import MessageContentRenderer from './MessageContentRenderer.vue'
import AgentTraceInline from '@/components/agent/AgentTraceInline.vue'
import AgentFullTrace from '@/components/agent/AgentFullTrace.vue'
import { useToastStore } from '@/stores/toast'
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

const toast = useToastStore()

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
  navigator.clipboard.writeText(text).catch(() => {
    toast.error('复制失败，请手动选择文本复制')
  })
}

/** 轨迹回放展开状态：展开期间操作行强制可见（默认 hover 才出现，面板会随鼠标移开消失） */
const traceOpen = ref(false)
const hasTraceAction = computed(
  () => props.message.role === 'assistant' && !!props.message.agentTrace?.lastRunId,
)
</script>

<style scoped>
.message-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 0;
  /* 轨迹下拉浮层的定位锚（AgentFullTrace .ft-body 绝对定位于此，左右钉满与对话区同幅自适应） */
  position: relative;
}

.message-block.user {
  align-items: flex-end;
}

.message-block.assistant {
  align-items: flex-start;
  /* 与面板左边框留出间隙（用户消息保持贴边，见下方 .user 注释） */
  padding-left: 10px;
}

.message-bubble {
  max-width: 92%;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 14px;
  line-height: 1.6;
}

.message-block.user .message-bubble {
  /* 宽比父容器多 1px：flex-end 对齐下左边缘与面板左边框重叠成单线，右边缘贴面板右缘，左右对称 */
  width: calc(100% + 1px);
  max-width: none;
  padding: 6px 14px;
  background: transparent;
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 0;
}

.message-block.assistant .message-bubble {
  width: 100%;
  max-width: 100%;
  background: transparent;
  color: var(--text-primary);
  padding-left: 0;
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
.message-actions:focus-within,
.message-actions.actions-open {
  opacity: 1;
}

/* 轨迹入口与复制按钮同排同尺寸（定位锚在 .message-block，此处不得 relative） */
.action-trace :deep(.ft-toggle) {
  padding: 4px 8px;
  font-size: 11px;
  gap: 4px;
}
.action-trace :deep(.ft-toggle:hover) {
  background: var(--bg-hover);
  color: var(--text-primary);
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
