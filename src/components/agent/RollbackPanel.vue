<template>
  <!-- 加载失败（非 404）在无数据时也必须可见：埋进 v-if="hasData" 里就成了永不渲染的死文案 -->
  <p v-if="!hasData && error" class="rb-hint err">{{ error }}</p>
  <div v-if="hasData" class="rb-panel">
    <div class="rb-head">
      <span class="rb-title">回滚</span>
      <span class="rb-summary">{{ summaryText }}</span>
      <button
        type="button"
        class="rb-btn"
        :disabled="!canRollback || starting"
        :title="buttonHint"
        @click="startRollback"
      >{{ starting ? '正在发起…' : '一键回滚' }}</button>
    </div>
    <p v-if="prodAllowlistHint" class="rb-hint warn">{{ prodAllowlistHint }}</p>
    <p v-if="blockHint" class="rb-hint warn">{{ blockHint }}</p>
    <p v-if="error" class="rb-hint err">{{ error }}</p>

    <ul class="rb-items">
      <li v-for="it in items" :key="it.seq" class="rb-item">
        <span class="rb-seq mono">#{{ it.seq }}</span>
        <span class="rb-kind">{{ kindLabel(it.kind) }}</span>
        <span class="rb-conf" :class="confClass(it)">{{ confLabel(it.confidence) }}</span>
        <code class="rb-desc mono" :title="itemTitle(it)">{{ itemDesc(it) }}</code>
        <span v-if="it.status === 'auto_rolled_back'" class="rb-status">已自动回滚</span>
        <span v-else-if="it.status === 'rolled_back'" class="rb-status ok">已还原</span>
        <span v-if="it.warning" class="rb-warn">{{ it.warning }}</span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
/**
 * RollbackPanel — run 级一键回滚入口（功能 B）
 *
 * 挂在 AgentFullTrace 元信息条下方；挂载即拉 undo 明细，无数据（404）时整体
 * 不渲染 —— 调用方（展开行/消息轨迹）无需关心该 run 有没有可回滚内容。
 *
 * 可用性闸（fail-closed，三条全过才可点）：
 * 1. run 处于终态且存在 pending 的非 manual 条目（FAILED run 的条目已
 *    auto_rolled_back，天然不可再回滚）；
 * 2. 存在指向目标主机（undo 行记录的 host）的活动 SSH 会话 —— undo 里的
 *    ssh_session_id 是临时会话号、可能已失效，不做自动重连，杜绝回滚命令
 *    打到错误机器；
 * 3. 当前没有正在运行的 run（agentStore.isRunning）。
 *
 * 点击后按序：plan（服务端只组装任务文本）→ agentStore.runAgent 走既有
 * POST /agent/run 交互式全链（审批卡/审计零成本继承，conversationId 取
 * plan.conversation_id —— 回滚消息落原会话、对用户可见）→ lastRunId 就绪后
 * register 登记 run 关联（undo_restore 归属校验的数据前提）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { rollbackApi, type UndoActionItem } from '@/api/rollback'
import { toFriendlyMessage } from '@/api/base'
import { useAgentStore } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { useSshAiStore } from '@/stores/ssh-ai'

const props = defineProps<{ runId: string }>()

const agentStore = useAgentStore()
const sshStore = useSSHStore()
// 模型选择与任务台/AI 对话同一份单例（Pinia ref 解包后直接读字符串值）
const sshAi = useSshAiStore()

// 404 判定：与 routes/rollbacks.py 的 404 detail（"run 不存在: …" /
// "run 无 undo 记录: …"）及 request 的 "HTTP 404" 兜底同源
const NOT_FOUND_RE = /(无 undo 记录|run 不存在|HTTP 404)/

const hasData = ref(false)
const terminal = ref(false)
const items = ref<UndoActionItem[]>([])
const targetHost = ref('')
const environment = ref('test')
const error = ref('')
const starting = ref(false)

onMounted(() => { void fetchUndoActions() })

async function fetchUndoActions() {
  error.value = ''
  try {
    const resp = await rollbackApi.getUndoActions(props.runId)
    items.value = resp.items || []
    terminal.value = resp.terminal
    targetHost.value = items.value[0]?.host || ''
    environment.value = items.value[0]?.environment || 'test'
    hasData.value = items.value.length > 0
  } catch (e) {
    hasData.value = false
    const msg = toFriendlyMessage(e, '回滚数据加载失败')
    // 404（run 无 undo 记录/run 不存在）= 该 run 没有可回滚内容，属正常形态：
    // 面板静默不渲染；其余错误（网络/5xx）必须可见 —— 文案在 hasData 块外渲染。
    // 识别串与 routes/rollbacks.py 两个 404 detail 同源，改后端文案需同步这里。
    if (NOT_FOUND_RE.test(msg)) {
      error.value = ''
    } else {
      error.value = msg
    }
  }
}

/** 可回滚项：pending 且非 manual（manual = 无反向命令/无快照/无副作用） */
const rollbackable = computed(() =>
  items.value.filter(it => it.status === 'pending' && it.confidence !== 'manual'),
)

const autoRolledCount = computed(() =>
  items.value.filter(it => it.status === 'auto_rolled_back').length,
)

const summaryText = computed(() => {
  const parts: string[] = []
  if (rollbackable.value.length) parts.push(`${rollbackable.value.length} 项可回滚`)
  if (autoRolledCount.value) parts.push(`${autoRolledCount.value} 项已自动回滚`)
  const manualCount = items.value.length - rollbackable.value.length - autoRolledCount.value
  if (manualCount > 0) parts.push(`${manualCount} 项仅记录`)
  return parts.join(' · ') || '无副作用记录'
})

/** 匹配「指向目标主机」的活动 SSH 会话（按连接配置的 host 匹配） */
const matchedSessionId = computed(() => {
  const host = targetHost.value
  if (!host) return ''
  const s = sshStore.sessions.find((x) => {
    if (x.type !== 'remote' || !x.connectionId) return false
    const conn = sshStore.connections.find(c => c.id === x.connectionId)
    return conn?.host === host
  })
  return s?.id || ''
})

const canRollback = computed(() =>
  terminal.value
  && rollbackable.value.length > 0
  && !!matchedSessionId.value
  && !!targetHost.value
  && !agentStore.isRunning,
)

const blockHint = computed(() => {
  if (!terminal.value) return 'run 尚未结束，结束后才能回滚'
  if (!rollbackable.value.length) return '没有待回滚的副作用（均已回滚或无可信反向）'
  if (!targetHost.value) return '无法确定目标主机，请勿回滚'
  if (!matchedSessionId.value) return `请先连接目标主机 ${targetHost.value}（不做自动重连，防止回滚命令打错机器）`
  if (agentStore.isRunning) return '有正在运行的 run，结束后再发起回滚'
  return ''
})

/** prod 说明（2026-10-27 修订）：交互会话里 dangerous 工具会弹审批卡，
 *  「始终允许」写入本主机 allowlist；无人值守（定时任务 / 工作流）没人可点卡，
 *  只能预先登记。本面板无法替用户登记（fail-closed），只能把两条通道说破，
 *  避免「点了回滚→file 项全被拒→不知原因」。 */
const prodAllowlistHint = computed(() =>
  environment.value === 'prod'
    ? '生产环境：危险操作会在审批卡上由你现场确认（允许本次 / 始终允许）。选「始终允许」即把该命令写入本主机 allowlist；定时任务、工作流等无人值守运行无法弹卡，必须预先登记，否则会被拒绝。'
    : '',
)

const buttonHint = computed(() => blockHint.value || '按 LIFO 序发起一次回滚 run（危险项会弹审批卡）')

function kindLabel(kind: string): string {
  if (kind === 'shell') return '命令'
  if (kind === 'file') return '文件'
  return '记录'
}

function confLabel(confidence: string): string {
  if (confidence === 'snapshot') return '快照还原'
  if (confidence === 'derived') return '反向命令'
  if (confidence === 'delete_created') return '将删除文件'
  return '仅记录'
}

function confClass(it: UndoActionItem): string {
  if (it.confidence === 'delete_created') return 'bad'
  if (it.confidence === 'snapshot') return 'ok'
  return 'muted'
}

function itemDesc(it: UndoActionItem): string {
  if (it.kind === 'file') return it.path || ''
  if (it.kind === 'shell') return it.reverse_command || it.command || ''
  return it.tool || ''
}

function itemTitle(it: UndoActionItem): string {
  if (it.kind === 'shell') return `原命令：${it.command || ''}\n反向：${it.reverse_command || '（无）'}`
  return it.path || ''
}

async function startRollback() {
  if (starting.value || !canRollback.value) return
  starting.value = true
  error.value = ''
  const preRunId = agentStore.lastRunId
  try {
    const plan = await rollbackApi.plan(props.runId)
    // conversationId 取 plan（后端从 agent_runs 直读）——回滚消息落原会话
    agentStore.runAgent({
      task: plan.task_text,
      conversationId: plan.conversation_id,
      modelId: sshAi.selectedModelId,
      sshSessionId: matchedSessionId.value || undefined,
      environment: plan.environment === 'prod' ? 'prod' : 'test',
      permissionMode: 'normal',
    })
    waitAndRegister(preRunId)
  } catch (e) {
    // 原会话有活动 run 时 plan 前被 isRunning 闸挡住；后端 409 等错误走这里
    error.value = toFriendlyMessage(e, '回滚发起失败，请稍后重试')
  } finally {
    starting.value = false
  }
}

/** 回滚 run 的首个 run_id SSE 事件到达（agentStore.lastRunId 变化）后登记关联。
 *  登记失败不影响回滚 run 本身；20s 兜底停止等待（run_id 迟迟不来多半是流挂了）。 */
function waitAndRegister(preRunId: string) {
  const stop = watch(() => agentStore.lastRunId, (id) => {
    if (!id || id === preRunId) return
    stop()
    rollbackApi.register(props.runId, id).catch((e) => {
      // 登记是关联簿记：失败只提示，不打断回滚 run；归属校验因此未生效前
      // undo_restore 会 fail-closed 拒绝 —— 属安全方向
      error.value = toFriendlyMessage(e, '回滚关联登记失败（回滚 run 仍在执行）')
    })
  })
  setTimeout(() => { stop() }, 20000)
}
</script>

<style scoped>
.rb-panel {
  padding: 6px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  min-width: 0;
}
.rb-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.rb-title { font-size: 11.5px; font-weight: 600; color: var(--text-secondary); }
.rb-summary { font-size: 11px; color: var(--text-tertiary); }
.rb-btn {
  margin-left: auto;
  padding: 2px 10px;
  font-size: 11.5px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  background: var(--bg-hover, transparent);
  color: var(--text-secondary);
  cursor: pointer;
}
.rb-btn:hover:not(:disabled) { color: var(--accent-text); border-color: var(--accent-text); }
.rb-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.rb-hint { margin: 4px 0 0; font-size: 11px; }
.rb-hint.warn { color: var(--status-warning-text, #b58900); }
.rb-hint.err { color: var(--status-error-text); }
.rb-items {
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.rb-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 11px;
  min-width: 0;
  flex-wrap: wrap;
}
.rb-seq { color: var(--text-tertiary); flex: none; }
.rb-kind { color: var(--text-secondary); flex: none; }
.rb-conf {
  flex: none;
  padding: 0 6px;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
  font-size: 10.5px;
}
.rb-conf.ok { color: var(--status-success-text); border-color: color-mix(in srgb, var(--status-success-text) 35%, transparent); }
.rb-conf.bad { color: var(--status-error-text); border-color: color-mix(in srgb, var(--status-error-text) 35%, transparent); }
.rb-conf.muted { color: var(--text-tertiary); }
.rb-desc {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}
.rb-status { flex: none; color: var(--text-tertiary); }
.rb-status.ok { color: var(--status-success-text); }
.rb-warn { flex: none; color: var(--status-error-text); }
.mono { font-family: var(--font-mono); }
</style>
