<template>
  <SideChatPanel
    ref="panelRef"
    :conversations="store.conversations"
    :current-conv-id="store.currentConvId"
    :messages="store.messages"
    :selected-model-id="store.selectedModelId"
    :models="modelsStore.models"
    :grouped-conversations="store.groupedConversations"
    :agent-running="agentStore.isRunning"
    :creating="creating"
    show-message-actions
    panel-title="运维知识助手"
    placeholder="询问运维问题..."
    empty-title="运维知识助手"
    empty-desc="专注于 Linux/网络/数据库/容器运维问题"
    @close="$emit('close')"
    @send="handleSend"
    @new-chat="handleNewChat"
    @switch-conversation="handleSwitchConv"
    @delete-conversation="handleDeleteConv"
    @abort="handleAbort"
    @update:selected-model-id="store.setSelectedModel($event)"
    :upload-handler="handleUploadFiles"
  >

    <template #header-actions-extra>
      <button
        class="memory-btn"
        :class="{ active: showMemory }"
        title="Agent 记忆"
        aria-label="Agent 记忆"
        @click="showMemory = !showMemory"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4a4 4 0 0 0-4 4 3 3 0 0 0-1 5.8A3.5 3.5 0 0 0 9 20h6a3.5 3.5 0 0 0 2-6.2A3 3 0 0 0 16 8a4 4 0 0 0-4-4z"/><path d="M12 4v16M9 8H7M17 8h-2M9 14H7.5M17 14h-1.5"/></svg>
      </button>
      <AgentMemoryView v-if="showMemory" @close="showMemory = false" />
    </template>

    <template #agent-panel>
      <!-- 仅在有警告/错误/人工介入/权限请求时显示 -->
      <AgentRunPanel
        v-if="agentStore.hasHumanHelpRequest || agentStore.hasPermissionRequest || agentStore.errorMessage || agentStore.warnings.length > 0"
        class="agent-run-panel-ssh"
      />
    </template>
  </SideChatPanel>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useModelsStore } from '@/stores/models'
import { useAgentStore } from '@/stores/agent'
import SideChatPanel from '@/components/chat/SideChatPanel.vue'
import AgentRunPanel from '@/components/agent/AgentRunPanel.vue'
import AgentMemoryView from '@/components/agent/AgentMemoryView.vue'
import type { ChatAttachment } from '@/api/client'

defineEmits<{ close: [] }>()

const store = useSshAiStore()
const modelsStore = useModelsStore()
const agentStore = useAgentStore()

/** AI 面板头部"Agent 记忆"浮层开关 */
const showMemory = ref(false)

function handleAbort() {
  agentStore.abort()
}

// M18：发送被 store 早退拒绝（后端未就绪/无模型/建会话失败）时回滚组件草稿
const panelRef = ref<InstanceType<typeof SideChatPanel> | null>(null)
async function handleSend(content: string, skillIds: string[] = [], attachments: ChatAttachment[] = []) {
  const ok = await store.sendMessage(content, { skillIds, attachments })
  if (!ok) panelRef.value?.restoreDraft()
}

/** 上传对话附件：委托给 store（内部会逐个调用 /chat/upload 并 toast 失败） */
async function handleUploadFiles(files: File[]): Promise<ChatAttachment[]> {
  return store.uploadFiles(files)
}

// 新建对话防重复提交
const creating = ref(false)
async function handleNewChat() {
  if (creating.value) return
  creating.value = true
  try {
    await store.createConversation()
    // 最小锁定时间 500ms，防止快速重复点击
    await new Promise(resolve => setTimeout(resolve, 500))
  } finally {
    creating.value = false
  }
}

// 组件卸载时重置创建状态，防止状态残留。
// 不中止运行中的 Agent — Agent 运行设计为可跨面板存活（P3-4），结果固化在
// store 层 watch，不依赖组件生命周期，面板重新打开即可继续查看。
onUnmounted(() => {
  creating.value = false
})

function handleSwitchConv(id: string) {
  store.switchConversation(id)
}

async function handleDeleteConv(id: string) {
  await store.deleteConversation(id)
}

onMounted(() => {
  store.fetchDefaultModel()
})
</script>

<style scoped>
/* SSH 面板特化样式占位，通用 Agent 切换样式已在 SideChatPanel.vue 统一维护 */
/* header-actions-extra 插槽内容编译于本组件作用域，需自带 icon-btn 等效样式 */
.memory-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary, #9a9a9a);
  cursor: pointer;
}
.memory-btn:hover {
  background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  color: var(--text-primary, #d4d4d4);
}
.memory-btn.active {
  color: var(--accent-primary, #7aa2d0);
  background: var(--bg-active, rgba(255, 255, 255, 0.08));
}
</style>
