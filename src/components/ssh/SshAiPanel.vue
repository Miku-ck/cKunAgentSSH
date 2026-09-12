<template>
  <SideChatPanel
    ref="panelRef"
    mode="ssh"
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
import type { ChatAttachment } from '@/api/client'

defineEmits<{ close: [] }>()

const store = useSshAiStore()
const modelsStore = useModelsStore()
const agentStore = useAgentStore()

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
</style>
