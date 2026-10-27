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
        :awaiting-user="awaitingUser"
      />
    </div>

    <div v-if="content || hasTraceAction" class="message-actions">
      <button v-if="content" class="action-btn" @click="copyContent(content)">复制</button>
      <!-- 记录面只此一处：跳到运维轨迹，按 run_id 定位这一轮的完整记录 -->
      <button
        v-if="hasTraceAction"
        class="action-btn"
        title="在运维轨迹中查看这次运行的完整记录"
        @click="openTrace"
      >
        轨迹
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
  /** run 停在澄清闸口等用户作答：AI 没有在输出，画等待提示而非呼吸点 */
  awaitingUser?: boolean
}>()

const emit = defineEmits<{ 'open-trace': [runId: string] }>()

const content = computed(() => props.message.streamingContent || props.message.content || '')
const reasoning = computed(() => props.message.streamingReasoning || props.message.reasoning || '')
const duration = computed(() => props.message.duration)
// 非流式时不传 defaultOpen，让 ThinkingBlock 回退到 settings.chat.thinkingKeepExpanded（默认折叠）
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
  gap: var(--spacing-6);
  padding: var(--spacing-12) 0;
}

.message-block.user {
  align-items: flex-end;
}

.message-block.assistant {
  align-items: flex-start;
  /* 与面板左边框留出间隙（用户消息保持贴边，见下方 .user 注释） */
  padding-left: var(--spacing-10);
}

.message-bubble {
  max-width: 92%;
  padding: var(--spacing-10) var(--spacing-14);
  border-radius: var(--radius-xl);
  /* 与 MarkdownStream 同一变量：设置里的「消息字号」必须真的落到气泡上。
     回落值取 store 的默认档 13（不是 14）—— 注入前那一帧两套字号会打架。 */
  font-size: var(--chat-font-size, 13px);
  line-height: 1.6;
}

.message-block.user .message-bubble {
  /* 宽比父容器多 1px：flex-end 对齐下左边缘与面板左边框重叠成单线，右边缘贴面板右缘，左右对称 */
  width: calc(100% + 1px);
  max-width: none;
  padding: var(--spacing-6) var(--spacing-14);
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
  gap: var(--spacing-6);
  opacity: 0;
  transition: opacity 0.15s;
  padding-left: var(--spacing-2);
}

.message-block:hover .message-actions,
.message-actions:focus-within {
  opacity: 1;
}

.action-btn {
  display: flex;
  align-items: center;
  padding: var(--spacing-4) var(--spacing-8);
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.8462rem;
  cursor: pointer;
  transition: all 0.1s;
}

.action-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.action-elapsed {
  color: var(--text-tertiary);
  font-size: 0.8462rem;
  margin-left: auto;
}
</style>
