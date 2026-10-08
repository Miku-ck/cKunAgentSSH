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
    :workflow-goal="store.workflowSnapshot.goal"
    :workflow-plan="store.workflowSnapshot.plan"
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
    @open-phase="handleOpenPhase"
    @clear-artifact="handleClearArtifact"
    @update:selected-model-id="store.setSelectedModel($event)"
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
import { useConfirm } from '@/composables/useConfirm'
import SideChatPanel from '@/components/chat/SideChatPanel.vue'
import AgentRunPanel from '@/components/agent/AgentRunPanel.vue'
import type { ChatAttachment } from '@/api/client'
import type { ApprovalKey, PostureKey } from '@/utils/run-modes'

defineEmits<{ close: [] }>()

const store = useSshAiStore()
const modelsStore = useModelsStore()
const agentStore = useAgentStore()
const toast = useToastStore()
const { confirm } = useConfirm()

// ── 会话工作流（Spec/Plan/Goal）──

/** chip 名：提示文案用，与 SideChatPanel 里 chipViews 的 name 同一口径 */
const CHIP_NAMES: Record<PostureKey, string> = { goal: '目标', plan: '计划' }

/** 各阶段的合成消息。计划的措辞刻意只说"先给我看方案"，不说"等你批准"——
 *  计划批准门（spec §2）还没实现，写出来就是一个点了没有下文的承诺。 */
const PHASE_REQUESTS: Record<PostureKey, string> = {
  goal:
    '进入「目标（Goal）」阶段：请先向我确认本次要达成什么、怎么算成功，' +
    '再用 goal_write 把可验收的成功标准沉淀为文档，作为本任务贯穿执行的锚点。',
  plan:
    '进入「计划（Plan）」阶段：请先只读调研目标机现状（不改变任何状态），' +
    '再用 plan_write 把有序步骤写成方案，我看过方案后再决定是否执行。',
}

/** chip 主体「让 AI 生成…」= 发一条该阶段的合成消息，让 agent 在 Run 内经
 *  spec/plan/goal_write 落盘工件（阶段由后端按落盘文件推导，前端不伪造）。
 *  mode 必须一并传：「计划」浮层承诺的是只读调研，不带 plan 姿态就等于在
 *  可写权限下"生成计划" —— 界面上那句话就成了假的。 */
async function handleOpenPhase(kind: PostureKey) {
  if (agentStore.isRunning) {
    toast.error('AI 正在运行中，请等待完成或先中止')
    return
  }
  // approval 必须一并传：chip 走的是"合成消息"这条路，漏掉它就会被 resolveRunModes
  // 压成 normal —— 用户选了自动审批，点「让 AI 生成方案」却按手动档跑，且界面上
  // 没有任何地方说这件事。这是 store 化之后剩下的第三条下达通路。
  await store.sendMessage(PHASE_REQUESTS[kind], { mode: kind, approval: store.approvalMode })
  // 发送失败时草稿由 store 层已有逻辑同步，这里不需要额外回滚
}

/** 阶段 → 中文标签（提示回落后果用；'' = 无工件，不提示） */
const PHASE_LABELS: Record<string, string> = { spec: '规格', plan: '计划', goal: '目标' }

/** chip 的 ×：删除该模式的工件。
 *  阶段取已存在的最高阶，删掉 goal 会回落到 plan/spec —— 不说出来，用户下次
 *  发消息就撞在一个凭空冒出来的状态上（批准门、注入内容都随阶段变）。 */
async function handleClearArtifact(kind: PostureKey) {
  const name = CHIP_NAMES[kind]
  const r = await store.clearWorkflowArtifact(kind)
  if (!r) {
    // null = 请求失败。不能退回「已移除」：空串是合法阶段（真的没有工件了），
    // 把失败说成成功，用户会以为 × 生效了，而工件仍在每轮注入。
    toast.error(`移除${name}失败，请稍后重试`)
    return
  }
  const label = PHASE_LABELS[r.phase]
  toast.info(label ? `已移除${name}，当前回到「${label}」阶段` : `已移除${name}`)
}

function handleAbort() {
  agentStore.abort()
}

// M18：发送被 store 早退拒绝（后端未就绪/无模型/建会话失败）时回滚组件草稿
const panelRef = ref<InstanceType<typeof SideChatPanel> | null>(null)
async function handleSend(content: string, skillIds: string[] = [], attachments: ChatAttachment[] = [], mode?: PostureKey | null, approval?: ApprovalKey) {
  // null = 用户没表态（chip 已关）：归一成 undefined，让 resolveRunModes 走
  // 「不注入任何姿态」那一支。传成 'goal' 就是把「没选」读成「选了目标」。
  const ok = await store.sendMessage(content, { skillIds, attachments, mode: mode ?? undefined, approval })
  if (!ok) panelRef.value?.restoreDraft()
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
  // S9-06：这是全应用**唯一**没有二次确认的不可逆用户数据删除入口 —— 后端一条
  // DELETE 就把消息历史 + 附件目录 rmtree 掉，而其余破坏性入口（定时任务/快捷
  // 命令/远程管理/SFTP/模型面板）都走 useConfirm。补齐同一口径，别让人误点一下
  // 垃圾桶就没了整段对话。
  const conv = store.conversations.find(c => c.id === id)
  const ok = await confirm({
    title: '删除对话',
    message: `确认删除对话「${conv?.title || '未命名'}」及其全部消息与附件？此操作不可恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
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
