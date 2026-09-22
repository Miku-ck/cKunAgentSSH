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
    :workflow-phase="store.workflowPhase"
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
    @open-goal="handleOpenGoal"
    @update:selected-model-id="store.setSelectedModel($event)"
    :upload-handler="handleUploadFiles"
  >

    <template #agent-panel>
      <!-- 异常/决策面板：自身按 agentStore.hasRunNotice 决定挂不挂载内容。
         这里不要再写条件：曾经写过一份，而那份漏了澄清向导/任务规划/待确认交付，
         结果是需要用户决策时面板根本没挂载，用户只看到 toast，运行等到后端超时。 -->
    <AgentRunPanel class="agent-run-panel-ssh" />
    </template>
  </SideChatPanel>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useModelsStore } from '@/stores/models'
import { useAgentStore } from '@/stores/agent'
import { useToastStore } from '@/stores/toast'
import SideChatPanel from '@/components/chat/SideChatPanel.vue'
import AgentRunPanel from '@/components/agent/AgentRunPanel.vue'
import type { ChatAttachment } from '@/api/client'

defineEmits<{ close: [] }>()

const store = useSshAiStore()
const modelsStore = useModelsStore()
const agentStore = useAgentStore()
const toast = useToastStore()

// ── 会话工作流（Spec/Plan/Goal）──

/** 就地菜单点「目标」：向 agent 发起一条"进入目标阶段"的意图，触发其在
 *  Run 内经 goal_write 沉淀成功标准文档（阶段由后端按落盘工件推导，前端不伪造）。 */
async function handleOpenGoal() {
  if (agentStore.isRunning) {
    toast.error('AI 正在运行中，请等待完成或先中止')
    return
  }
  const ok = await store.sendMessage(
    '进入「目标（Goal）」阶段：请先向我确认本次要达成什么、怎么算成功，' +
    '再用 goal_write 把可验收的成功标准沉淀为文档，作为本任务贯穿执行的锚点。',
  )
  // 发送失败时草稿由 store 层已有逻辑同步，这里不需要额外回滚
}

function handleAbort() {
  agentStore.abort()
}

// M18：发送被 store 早退拒绝（后端未就绪/无模型/建会话失败）时回滚组件草稿
const panelRef = ref<InstanceType<typeof SideChatPanel> | null>(null)
async function handleSend(content: string, skillIds: string[] = [], attachments: ChatAttachment[] = [], mode?: string, approval?: 'normal' | 'auto') {
  const ok = await store.sendMessage(content, { skillIds, attachments, mode, approval })
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

// 组件卸载时重置创建中标志，避免状态残留。
// 不中止运行中的 Agent — Agent 运行设计为可跨面板存活（P3-4），结果固化在
// store 层 watch，不依赖组件生命周期，面板重新打开即可继续查看。
onUnmounted(() => {
  creating.value = false
})

// ── 终端「问 AI」预填：store 请求预填 → 打开面板 + 注入问题草稿 ──
// 每次请求都是新对象（watch 按引用触发）；消费后置 null 防重复。面板是 v-if 条件挂载，
// 首次「问 AI」时 pendingPrefill 与 showAiPanel 同批写入，setup 阶段 immediate watch 先触发，
// 此刻 SideChatPanel 的 template ref 尚未赋值——用 nextTick 等渲染/挂载完成后写入。
// 仅在实际执行 prefill 后才清空请求；ref 仍不就绪时保留请求，由下方 watch(panelRef)
// 在 ref 落地后补一次消费，兑现「保留请求待下次挂载消费」的承诺（不静默丢弃）。
function consumePrefill(): void {
  const req = store.pendingPrefill
  const el = panelRef.value
  if (!req || !el) return
  el.prefill(req.text)
  store.pendingPrefill = null
}
watch(() => store.pendingPrefill, (req) => {
  if (!req) return
  nextTick(consumePrefill)
}, { immediate: true })
// ref 就绪兜底：pendingPrefill 在 ref 挂载前写入时，上面的 nextTick 里 ref 可能尚未赋值
// → ref 落地（null → 实例）后再消费一次，覆盖面板首次打开的竞态路径。
watch(panelRef, () => {
  nextTick(consumePrefill)
})

function handleSwitchConv(id: string) {
  store.switchConversation(id)
}

async function handleDeleteConv(id: string) {
  await store.deleteConversation(id)
}

onMounted(() => {
  store.fetchDefaultModel()
  // 工作流工件落盘在后端、每轮 run 都会被注入（即使前端从不知道它存在）。
  // 面板挂载时补一次读取：否则重启后阶段指示消失，但模型仍在按工件执行 ——
  // 界面与注入不一致，比没这个功能更危险。
  void store.loadWorkflow()
})
</script>

<style scoped>
</style>
