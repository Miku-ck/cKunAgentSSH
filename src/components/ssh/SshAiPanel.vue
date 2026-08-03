<template>
  <SideChatPanel
    mode="ssh"
    :conversations="store.conversations"
    :current-conv-id="store.currentConvId"
    :messages="store.messages"
    :streaming="store.streaming"
    :selected-model-id="store.selectedModelId"
    :models="chatStore.models"
    :grouped-conversations="store.groupedConversations"
    :agent-running="agentStore.isRunning"
    show-agent-toggle
    v-model:agent-mode="agentMode"
    show-message-actions
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
import { onMounted, toRef } from 'vue'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useChatStore } from '@/stores/chat'
import { useAgentIntegration } from '@/composables/useAgentIntegration'
import SideChatPanel from '@/components/chat/SideChatPanel.vue'
import AgentRunPanel from '@/components/agent/AgentRunPanel.vue'

defineEmits<{ close: [] }>()

const store = useSshAiStore()
const chatStore = useChatStore()

const { agentStore, agentMode, handleAbort: agentHandleAbort } = useAgentIntegration({
  mode: 'ssh',
  agentModeRef: toRef(store, 'agentMode'),
})

function handleAbort() {
  agentHandleAbort(() => store.abortStream())
}

async function handleSend(content: string, skillIds: string[] = []) {
  await store.sendMessage(content, { skillIds })
}

function handleNewChat() {
  store.createConversation()
}

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
