<template>
  <div class="home-workspace">
    <div class="home-main">
      <div ref="scrollRef" class="home-scroll">
        <div class="home-column" :class="{ 'is-empty': isEmpty }">
          <h1 v-if="isEmpty" class="home-ask">今天想让助手替你做什么？</h1>
          <template v-else>
            <div class="home-title" :title="conversationTitle">{{ conversationTitle }}</div>
            <div class="home-messages" role="log" aria-live="polite">
              <MessageItem
                v-for="(msg, idx) in displayMessages"
                :key="msg.id"
                :message="msg"
                :is-last="idx === displayMessages.length - 1"
                :is-streaming="isLiveAt(idx)"
                :inline-approvals="isLiveAt(idx) ? agentStore.pendingPermissions : undefined"
                :process-meta="metaFor(msg, isLiveAt(idx))"
              />
            </div>
          </template>
        </div>
      </div>
      <HomeHistoryDrawer
        v-if="historyOpen"
        :groups="sshAi.groupedConversations"
        :current-conv-id="sshAi.currentConvId"
        :is-empty="sshAi.conversations.length === 0"
        :busy="agentStore.isRunning"
        @close="emit('close-history')"
        @switch="switchConversation"
        @delete="deleteConversation"
      />
    </div>
    <div class="composer-wrap">
      <HomeComposer @bulk="emit('bulk')" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, ref, watch } from 'vue'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useAgentStore } from '@/stores/agent'
import { useModelsStore } from '@/stores/models'
import { useSSHStore } from '@/stores/ssh'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { approvalTargetLabel } from '@/utils/approval-display'
import { buildDisplayMessages, isLiveAgentRun } from '@/utils/display-messages'
import type { ProcessMeta } from '@/utils/process-status'
import { PERMISSION_LABELS } from '@/utils/run-modes'
import type { BaseMessage } from '@/types/chat'
// MessageItem 的依赖链带着 marked/highlight.js/DOMPurify。它是首页里的常驻组件，
// 静态引入会把整条 Markdown 栈钉进入口包；异步化后空态首屏不下载，第一条消息
// 出现时才按需加载（与 SshView 拆页同一手法）。
const MessageItem = defineAsyncComponent(() => import('@/components/chat/MessageItem.vue'))
import HomeComposer from '@/components/home/HomeComposer.vue'
import HomeHistoryDrawer from '@/components/home/HomeHistoryDrawer.vue'

defineProps<{ historyOpen: boolean }>()
const emit = defineEmits<{
  'close-history': []
  bulk: []
}>()

const sshAi = useSshAiStore()
const agentStore = useAgentStore()
const modelsStore = useModelsStore()
const ssh = useSSHStore()
const settingsStore = useSettingsStore()
const toast = useToastStore()
const { confirm } = useConfirm()

const scrollRef = ref<HTMLElement | null>(null)

const isEmpty = computed(() => sshAi.messages.length === 0)
const conversationTitle = computed(() =>
  sshAi.conversations.find(c => c.id === sshAi.currentConvId)?.title || '新会话',
)

/** live 合并与侧栏同一份纯函数；非运行中直接返回 store 的原数组，保持消息引用稳定 */
const displayMessages = computed(() => {
  if (!agentStore.isRunning) return sshAi.messages
  return buildDisplayMessages(sshAi.messages, {
    running: true,
    content: agentStore.content,
    reasoning: agentStore.reasoning,
    trace: agentStore.snapshot(),
  })
})

function isLiveAt(idx: number): boolean {
  return isLiveAgentRun(displayMessages.value, idx, agentStore.isRunning)
}

/**
 * 归因只在「这条 trace 属于最近一次 run」时给出：模型/审批档/目标都是发起时
 * 记在 agent store 的当前值，套到历史旧 run 上等于伪造当时的选择。
 * 运行中的快照清空了 lastRunId，所以 live 单独放行。
 */
function metaFor(msg: BaseMessage, live: boolean): ProcessMeta | undefined {
  const trace = msg.agentTrace
  if (!trace) return undefined
  const matchesCurrentRun = live || (!!agentStore.lastRunId && trace.lastRunId === agentStore.lastRunId)
  if (!matchesCurrentRun) return undefined
  const modelName = modelsStore.models.find(m => m.id === agentStore.lastModelId)?.name || ''
  return {
    modelName: modelName || undefined,
    permissionLabel: PERMISSION_LABELS[agentStore.lastPermissionMode],
    targetLabel: approvalTargetLabel(
      { sshSessionId: agentStore.lastSshSessionId },
      ssh.sessions,
      ssh.connections,
    ) || undefined,
    runId: agentStore.lastRunId || undefined,
    // 运行中由过程头部的本地 ticker 计时；收尾后这里补上整程耗时
    durationMs: live ? undefined : (trace.duration ? Math.round(trace.duration * 1000) : undefined),
  }
}

// ── 自动滚动：新消息、流式正文/思考、审批卡出现都跟到底 ──
async function scrollToBottom() {
  if (!settingsStore.settings.chat.autoScroll) return
  await nextTick()
  const el = scrollRef.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(() => displayMessages.value.length, () => { void scrollToBottom() }, { immediate: true })
// 切会话时消息条数可能恰好相同，length watch 不会动 —— 单独跟一次，避免停在上一段的位置
watch(() => sshAi.currentConvId, () => { void scrollToBottom() })
watch(() => agentStore.content, () => { if (agentStore.isRunning) void scrollToBottom() })
watch(() => agentStore.reasoning, () => { if (agentStore.isRunning) void scrollToBottom() })
watch(() => agentStore.pendingPermissions.length, () => { void scrollToBottom() })

function switchConversation(id: string) {
  if (agentStore.isRunning) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  void sshAi.switchConversation(id)
  emit('close-history')
}

async function deleteConversation(id: string) {
  if (agentStore.isRunning) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  const conv = sshAi.conversations.find(c => c.id === id)
  const ok = await confirm({
    title: '删除对话',
    message: `确认删除对话「${conv?.title || '未命名'}」及其全部消息与附件？此操作不可恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  await sshAi.deleteConversation(id)
}
</script>

<style scoped>
.home-workspace {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  flex-direction: column;
}

.home-main {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
}

.home-scroll {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
}

.home-column {
  width: 100%;
  max-width: 820px;
  min-height: 100%;
  margin: 0 auto;
  padding: 32px 24px 16px;
  box-sizing: border-box;
}

/* 空白任务：问句落在视口中间略偏上，输入区仍钉在底部（两态位置一致） */
.home-column.is-empty {
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  padding-bottom: 12vh;
}

.home-ask {
  margin: 0;
  text-align: center;
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
  letter-spacing: 0.2px;
}

/* 会话标题随消息流滚动，不做固定头部 */
.home-title {
  margin: 0 0 8px;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-wrap {
  flex: 0 0 auto;
  padding: 8px 24px 18px;
  display: flex;
  justify-content: center;
}
</style>
