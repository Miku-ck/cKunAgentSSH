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
      <!-- 意图 badge（2026-11-23 按用户截图）：用户消息携带的意图声明标记。
           从消息文本尾部推导（intent-core 唯一事实源），不改消息模型与持久化；
           用户自己打出同样的话也显示 —— 语义上他确实提出了同样的请求。 -->
      <span v-if="intentBadges.length" class="intent-badges">
        <span
          v-for="k in intentBadges"
          :key="k"
          class="intent-badge"
          :title="INTENT_SENTENCES[k]"
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="12" height="12" aria-hidden="true">
            <path v-if="k === 'plan'" d="M3 4h10M3 8h10M3 12h6" />
            <path v-else-if="k === 'spec'" d="M3 2.5h10v11H3zM5.5 5.5h5M5.5 8h5M5.5 10.5h3" />
            <!-- 刻意不用 template 标签包分支：?raw 守卫按首个内层闭合标签截取模板 -->
            <circle v-else-if="k === 'goal'" cx="8" cy="8" r="5.5" />
            <circle v-if="k === 'goal'" cx="8" cy="8" r="2" />
          </svg>
          {{ k === 'goal' ? 'Goal' : k === 'plan' ? 'Plan' : 'Spec' }}
        </span>
      </span>
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
import { INTENT_SENTENCES, extractIntents } from '@/utils/intent-core'

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
/** 用户消息携带的意图标记：从文本尾部推导（发送时拼接与推导同源 intent-core）。 */
const intentBadges = computed(() =>
  props.message.role === 'user' ? extractIntents(content.value) : [])
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

/* 意图 badge（2026-11-23 按用户截图）：绿色图标 + 文字，跟在用户消息文本后。
   绿用 --accent-green 独立语义色（不占 accent-primary 的 --accent-text 管制）。 */
.intent-badges {
  display: inline-flex;
  gap: var(--spacing-8);
  margin-left: var(--spacing-8);
  vertical-align: baseline;
}
.intent-badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  color: var(--accent-green);
  font-size: 0.9231rem;
  font-weight: 500;
  white-space: nowrap;
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
