<template>
  <div class="message-block" :class="message.role">
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

    <div v-if="content || hasTraceAction" class="message-actions">
      <button v-if="content" class="action-btn" @click="copyContent(content)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        <span>复制</span>
      </button>
      <!-- 记录面只此一处：跳到运维轨迹，按 run_id 定位这一轮的完整记录 -->
      <button
        v-if="hasTraceAction"
        class="action-btn"
        title="在运维轨迹中查看这次运行的完整记录"
        @click="openTrace"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="12" height="12"><circle cx="4.5" cy="18.5" r="2"/><circle cx="19.5" cy="5.5" r="2"/><path d="M6.5 18.5h3a4 4 0 0 0 4-4v-5a4 4 0 0 1 4-4"/></svg>
        <span>轨迹</span>
      </button>
      <slot v-if="content" name="actions" :content="content" />
      <span v-if="duration && message.role === 'assistant'" class="action-elapsed">耗时 {{ duration }}s</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ThinkingBlock from './ThinkingBlock.vue'
import MessageContentRenderer from './MessageContentRenderer.vue'
import { useToastStore } from '@/stores/toast'
import type { BaseMessage } from '@/types/chat'

const toast = useToastStore()

const props = defineProps<{
  message: BaseMessage
  isStreaming?: boolean
  isLast?: boolean
}>()

const emit = defineEmits<{ 'open-trace': [runId: string] }>()

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

const traceRunId = computed(() => props.message.agentTrace?.lastRunId || '')
const hasTraceAction = computed(
  () => props.message.role === 'assistant' && !!traceRunId.value,
)

function openTrace() {
  if (traceRunId.value) emit('open-trace', traceRunId.value)
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
  /* 与面板左边框留出间隙（用户消息保持贴边，见下方 .user 注释） */
  padding-left: 10px;
}

.message-bubble {
  max-width: 92%;
  padding: 10px 14px;
  border-radius: 10px;
  /* 与 MarkdownStream 同一变量：设置里的「消息字号」必须真的落到气泡上。
     回落值取 store 的默认档 13（不是 14）—— 注入前那一帧两套字号会打架。 */
  font-size: var(--chat-font-size, 13px);
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
