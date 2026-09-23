<script setup lang="ts">
/**
 * AgentRunPanel — Agent 运行过程通知面板（异常/需要用户决策时显示）
 *
 * 实际包含的区块（均为条件渲染，无内容时整块隐藏）:
 * - 澄清向导（ClarifyingWizard：自带 visible 门控 + Teleport，不受下方整块开关影响）
 * - 任务规划（意图分类：级别/任务类型/思考预算）
 * - 警告（warnings）
 * - 人工介入求助卡片（AI 请求帮助，可忽略）
 * - 工具授权请求（允许本次/本会话允许/始终允许/忽略，命令工具绑定命令哈希；
 *   session 档只对非 dangerous 卡开放，口径见 utils/approval-scopes.ts）
 * - 可见终端执行：待确认命令（确认/拒绝）与在途交付状态徽章
 * - 错误（errorMessage）
 *
 * 整块的显隐由 agentStore.hasRunNotice 决定（数据侧单一判定，逐区块对齐）——
 * 宿主组件不要再抄一份条件：漏掉一项就等于那块内容永远不挂载。
 *
 * 注：常规运行状态（thinking/acting、迭代计数、工具调用流）不在本面板展示 —
 * 工具调用明细见消息气泡内联轨迹（AgentTraceInline / ToolOperationList）。
 */
import { computed, ref, watch } from 'vue'
import { useAgentStore, type PermissionRequest } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { useSettingsStore } from '@/stores/settings'
import { agentApi } from '@/api/client'
import { grantScopesForRisk, type GrantScope } from '@/utils/approval-scopes'
import { useToastStore } from '@/stores/toast'
import ClarifyingWizard from './ClarifyingWizard.vue'

const agentStore = useAgentStore()
const sshStore = useSSHStore()
const toast = useToastStore()

function levelLabel(level: string): string {
  const map: Record<string, string> = {
    trivial: '简单',
    simple: '单步',
    moderate: '多步',
    complex: '复杂',
    debug: '调试',
  }
  return map[level] || level
}

function taskTypeLabel(type: string): string {
  const map: Record<string, string> = {
    query: '查询',
    implement: '实现',
    modify: '修改',
    refactor: '重构',
    debug: '调试',
    research: '调研',
    conversation: '对话',
  }
  return map[type] || type
}

/** 可见终端交付状态的人读文案（失败态附退出码，与汇总栏失败口径对齐） */
function deliveryStatusLabel(status: string, exitCode: number | null = null): string {
  const map: Record<string, string> = {
    capturing: '执行中',
    done: '已完成',
    rejected: '已拒绝',
    aborted: '已中止',
    timeout: '超时',
    failed: '失败',
    writing: '写入中',
  }
  const label = map[status] || status
  return status === 'failed' && exitCode !== null ? `${label}(exit ${exitCode})` : label
}

/** 风险等级人读文案：英文 dangerous/moderate 对非专业用户没有信息量 */
function riskLabel(risk: string): string {
  const map: Record<string, string> = { dangerous: '高危', moderate: '中风险', safe: '低风险' }
  return map[risk] || risk
}

/** 审批目标主机展示名 — 后端只带 ssh_session_id，可读标签在前端按会话反查 */
function targetLabel(req: PermissionRequest): string {
  if (!req.sshSessionId) return ''
  const session = sshStore.sessions.find(s => s.id === req.sshSessionId)
  const conn = session?.connectionId
    ? sshStore.connections.find(c => c.id === session.connectionId)
    : undefined
  if (conn) return `${conn.name || conn.host}（${conn.username}@${conn.host}）`
  // 连接记录被删除或本地终端会话：退到会话名，再不行显示会话 id 片段（至少可辨识）
  return session?.name || `会话 ${req.sshSessionId.slice(0, 8)}`
}

/** 明确拒绝：后端登记后本 run 内 AI 重发同一条命令不再弹卡片 */
async function denyRequest(reqId: string) {
  if (!await agentStore.denyPermission(reqId)) {
    toast.error('拒绝登记失败，请重试')
  }
}

/** 授权（审查 M-3：once/session/always 三档，可见档位与后端契约对齐见
 *  grantScopesForRisk；命令工具始终允许时绑定该条具体命令，不会覆盖其他 dangerous 命令） */
async function grantPermission(reqId: string, tool: string, scope: GrantScope) {
  const req = agentStore.permissionRequests.find(r => r.id === reqId)
  if (!req || req.settling || req.resolved) return
  // 命令取 payload 原文（后端 permission_display_fields.command）：它才是授权键实际
  // 绑定的那条串。原先从 args.command 猜，job_run 用的是 args.cmd 因而永远传空 →
  // 键退化成裸工具名；file_write 更是根本没有 command 参数。
  const command = req.command
  req.settling = true
  try {
    // 回传发起审批的 run_id / 会话 id：once 按 run 键控、session 按会话键控
    await agentApi.grantPermission(tool, scope, command, req.runId, req.conversationId)
    agentStore.resolvePermission(reqId, true)
  } catch (e) {
    req.settling = false
    console.error('授权失败:', e)
    toast.error('授权失败，请重试')
  }
}

/** 卡片上「授权命令」一行要显示的串：优先归一化形态（剥混淆、可比较），
 *  退回 payload 原文（写入/定时任务类工具显示的是实际落盘的模板）。 */
function cardCommand(req: PermissionRequest): string {
  return req.normalizedCommand || req.command || ''
}

/** 在途交付状态卡片的汇总统计（口径：已记录=全部登记的交付；执行=已离开待确认；
 *  失败=timeout/failed/aborted；探测=exit 为 0 的已执行项，用于体现成功执行的查询类命令）。 */
const deliveredDeliveries = computed(() =>
  agentStore.deliveries.filter(d => d.status !== 'pending_confirm'),
)
const deliveredTotal = computed(() => agentStore.deliveries.length)
const deliveredExec = computed(() => deliveredDeliveries.value.length)
const deliveredFailCount = computed(
  () => deliveredDeliveries.value.filter(d => ['timeout', 'failed', 'aborted'].includes(d.status)).length,
)
const deliveredProbe = computed(
  () => deliveredDeliveries.value.filter(d => d.exit_code === 0).length,
)

// 失败原因聚合：{ 原因名: 出现次数 }，用于折叠头一行说清"全部超时"而非列 N 个红徽章
const failReasonStats = computed(() => {
  const reasons = new Map<string, number>()
  for (const d of deliveredDeliveries.value) {
    if (!['timeout', 'failed', 'aborted'].includes(d.status)) continue
    const key = deliveryStatusLabel(d.status, d.exit_code)
    reasons.set(key, (reasons.get(key) || 0) + 1)
  }
  return Array.from(reasons.entries())
})

// 标题栏里的失败摘要："全部超时" / "3 个超时·1 个失败" / "2 个失败"
const failSummaryText = computed((): string => {
  const fails = deliveredFailCount.value
  if (fails === 0) return '全部成功'
  const total = deliveredExec.value
  const stats = failReasonStats.value
  // 全部失败且原因相同时，一句说清
  if (fails === total && stats.length === 1) return `全部${stats[0][0]}`
  // 多种失败原因，聚合说
  return stats.map(([label, n]) => `${n} 个${label}`).join('·')
})

// 命令列表是否可折叠：3 条以上默认折叠
const canCollapse = computed(() => deliveredExec.value >= 3)
const execListExpanded = ref(false)
function toggleExecList() { execListExpanded.value = !execListExpanded.value }

// ── 单一焦点审批队列（spec refactor-approval-center） ──
const pendingPerms = computed(() => agentStore.pendingPermissions)
/** E5：prod 专属警示的总开关（设置 → Agent 治理 → 运维策略） */
const prodConfirmOn = computed(() => useSettingsStore().settings.agent.prodConfirm)
/** 超时保留项（追溯区） */
const expiredPerms = computed(() => agentStore.expiredPermissions)
/** 已解决（已授权/已拒绝）项（追溯区） */
const resolvedPerms = computed(() => agentStore.permissionRequests.filter(r => r.resolved))

// 焦点在待审批队列内的索引；队列收缩/触发时钳制到合法范围
const focusIdx = ref(0)
watch(() => pendingPerms.value.length, (n) => {
  if (focusIdx.value >= n) focusIdx.value = Math.max(0, n - 1)
})
/** 焦点卡 = 待审批队列里最旧一条（队首），随结算自动向前 */
const focusPerm = computed(() => pendingPerms.value[focusIdx.value] ?? pendingPerms.value[0] ?? null)
/** 折叠的排队数（除焦点外其余待处理） */
const queueCount = computed(() => Math.max(0, pendingPerms.value.length - 1))
/** 待处理队列折叠列表是否展开 */
const queueOpen = ref(false)

function focusNext() {
  const n = pendingPerms.value.length
  if (n <= 1) return
  focusIdx.value = (focusIdx.value + 1) % n
}
function focusPrev() {
  const n = pendingPerms.value.length
  if (n <= 1) return
  focusIdx.value = (focusIdx.value - 1 + n) % n
}
function focusAt(i: number) {
  if (i >= 0 && i < pendingPerms.value.length) focusIdx.value = i
}

// 批量审批：快照待审批 id 再逐条按正常授权/拒绝流程结算（grantPermission /
// denyRequest 自身幂等：resolved/settling 会自动跳过）
const batchBusy = ref(false)
async function grantAll() {
  if (batchBusy.value || pendingPerms.value.length === 0) return
  batchBusy.value = true
  for (const req of [...pendingPerms.value]) {
    await grantPermission(req.id, req.tool, 'once')
  }
  batchBusy.value = false
}
async function denyAll() {
  if (batchBusy.value || pendingPerms.value.length === 0) return
  batchBusy.value = true
  for (const req of [...pendingPerms.value]) {
    await denyRequest(req.id)
  }
  batchBusy.value = false
}

// ── 人工介入提示（Task 3：降级为顶部细条通知，只显示最近一条） ──
const latestHelp = computed(() =>
  agentStore.humanHelpRequests[agentStore.humanHelpRequests.length - 1] ?? null,
)

</script>

<template>
  <ClarifyingWizard />
  <div v-if="agentStore.hasRunNotice" class="agent-run-panel">
    <!-- 规划/意图信息 -->
    <div class="intention-section" v-if="agentStore.intentionInfo">
      <div class="ap-card ap-intention">
        <div class="ap-head">
          <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
          <span class="ap-ttl">任务规划</span>
          <span class="ap-level" :class="`level-${agentStore.intentionInfo.level}`">
            {{ levelLabel(agentStore.intentionInfo.level) }}
          </span>
        </div>
        <div class="ap-intention-details">
          <span class="ap-tag">{{ taskTypeLabel(agentStore.intentionInfo.task_type) }}</span>
          <span class="ap-tag" v-if="agentStore.intentionInfo.allow_planning_phase">含规划阶段</span>
          <span class="ap-tag" v-if="agentStore.intentionInfo.reasoning_budget">思考预算 {{ agentStore.intentionInfo.reasoning_budget }} tokens</span>
        </div>
      </div>
    </div>

    <!-- 警告 -->
    <div class="warnings-section" v-if="agentStore.warnings.length > 0">
      <div v-for="(w, index) in agentStore.warnings" :key="`warn-${index}`" class="ap-warning">
        <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        {{ w }}
      </div>
    </div>

    <!-- 人工介入提示（Task 3：降级为顶部细条通知，非阻塞、不占审批焦点） -->
    <div class="ap-help-msgbar" v-if="latestHelp">
      <svg class="ap-ic ap-help-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8.5"/><path d="M14.8 9.2a3 3 0 1 0-5.2 2c.9.9 1.4 1.3 1.4 2.3"/><line x1="12" y1="17.2" x2="12.01" y2="17.2"/></svg>
      <span class="ap-help-msg" :title="latestHelp.reason">
        AI 请求人工介入<template v-if="agentStore.humanHelpRequests.length > 1">（{{ agentStore.humanHelpRequests.length }} 次）</template>：{{ latestHelp.reason }}
      </span>
      <span class="ap-sp"></span>
      <button
        v-if="!agentStore.isRunning"
        class="ap-icon-btn"
        title="忽略"
        aria-label="忽略该请求"
        @click="agentStore.dismissHelp(latestHelp.id)"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>

    <!-- 权限请求：单一焦点审批托盘 -->
    <div class="permission-section" v-if="agentStore.permissionRequests.length > 0">
      <!-- 托盘头：汇总计数 + 人工介入角标 + 批量操作 -->
      <div class="ap-perm-tray">
        <svg class="ap-ic ap-perm-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        <span class="ap-ttl ap-perm-ttl">审批队列</span>
        <span class="ap-tray-count">{{ pendingPerms.length }} 待处理</span>
        <span
          v-if="agentStore.humanHelpRequests.length > 0"
          class="ap-badge ap-badge-warn"
          role="status"
        >{{ agentStore.humanHelpRequests.length }} 次人工介入</span>
        <span class="ap-sp"></span>
        <button class="ap-btn ap-btn-grant" :disabled="batchBusy || pendingPerms.length === 0" @click="grantAll">
          {{ batchBusy ? '处理中...' : `全部允许本次（${pendingPerms.length}）` }}
        </button>
        <button class="ap-btn ap-btn-deny" :disabled="batchBusy || pendingPerms.length === 0" @click="denyAll">
          {{ batchBusy ? '处理中...' : `全部拒绝（${pendingPerms.length}）` }}
        </button>
      </div>

      <!-- 单一焦点卡：最旧一条未处理 -->
      <div v-if="focusPerm" class="ap-card ap-perm">
        <div class="ap-head">
          <svg class="ap-ic ap-perm-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          <span class="ap-ttl ap-perm-ttl">AI 请求执行确认</span>
          <span class="ap-risk" :class="`risk-${focusPerm.risk}`">{{ riskLabel(focusPerm.risk) }}</span>
          <span v-if="focusPerm.environment" class="ap-env" :class="`env-${focusPerm.environment}`">
            {{ focusPerm.environment === 'prod' ? '生产环境' : '测试环境' }}
          </span>
          <span class="ap-sp"></span>
          <button
            v-if="pendingPerms.length > 1"
            class="ap-icon-btn"
            title="上一项"
            aria-label="切换到上一条待审批项"
            @click="focusPrev"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
          </button>
          <button
            v-if="pendingPerms.length > 1"
            class="ap-icon-btn"
            title="下一项"
            aria-label="切换到下一条待审批项"
            @click="focusNext"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
          </button>
          <button
            v-if="!agentStore.isRunning"
            class="ap-icon-btn"
            title="忽略"
            aria-label="忽略该请求"
            @click="agentStore.dismissPermission(focusPerm.id)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
        <div class="ap-body">
          <!-- 阅读顺序：我要做的事 → 后果 → 目标机器 -->
          <!-- E5：prod 提示受「生产环境额外确认」开关控制。关掉只是不再多问一句，
               风险判定本身（dangerous 审批 / prod 预授权 allowlist / hardline 阻断）不受影响 -->
          <div v-if="focusPerm.environment === 'prod' && prodConfirmOn" class="ap-prod-warn">
            目标为生产环境主机，误操作影响线上业务，请格外确认后再授权。
          </div>
          <div v-if="focusPerm.task" class="ap-keyval" :title="focusPerm.task">
            <span class="ap-k">你的请求</span>
            <span class="ap-v ap-2line">{{ focusPerm.task }}</span>
          </div>
          <div v-if="focusPerm.explain" class="ap-keyval">
            <span class="ap-k">这条命令会做什么</span>
            <span class="ap-v">{{ focusPerm.explain }}</span>
            <span v-if="focusPerm.explainSource === 'llm'" class="ap-tag-inline">AI 生成，仅供参考</span>
          </div>
          <div v-if="targetLabel(focusPerm)" class="ap-keyval">
            <span class="ap-k">目标主机</span>
            <code class="ap-code">{{ targetLabel(focusPerm) }}</code>
          </div>
          <div class="ap-msg">{{ focusPerm.message }}</div>
          <div class="ap-keyval">
            <span class="ap-k">工具:</span>
            <code class="ap-code">{{ focusPerm.tool }}</code>
            <span class="ap-k ap-k-gap">可逆:</span>
            <span class="ap-v">{{ focusPerm.reversible ? '是' : '否' }}</span>
            <template v-if="cardCommand(focusPerm)">
              <span class="ap-k ap-k-gap">授权命令:</span>
              <code class="ap-code ap-code-normal">{{ cardCommand(focusPerm) }}</code>
            </template>
          </div>
          <details v-if="focusPerm.args && Object.keys(focusPerm.args).length > 0" class="ap-args">
            <summary>参数</summary>
            <pre>{{ JSON.stringify(focusPerm.args, null, 2) }}</pre>
          </details>
        </div>
        <div class="ap-actions">
          <button class="ap-btn ap-btn-grant" :disabled="focusPerm.settling" @click="grantPermission(focusPerm.id, focusPerm.tool, 'once')">
            {{ focusPerm.settling ? '处理中...' : '允许本次' }}
          </button>
          <button
            v-if="grantScopesForRisk(focusPerm.risk).includes('session')"
            class="ap-btn ap-btn-session"
            :disabled="focusPerm.settling"
            @click="grantPermission(focusPerm.id, focusPerm.tool, 'session')"
          >
            本会话允许
          </button>
          <button class="ap-btn ap-btn-always" :disabled="focusPerm.settling" @click="grantPermission(focusPerm.id, focusPerm.tool, 'always')">
            {{ cardCommand(focusPerm) ? '始终允许此命令' : '始终允许' }}
          </button>
          <button class="ap-btn ap-btn-deny" :disabled="focusPerm.settling" @click="denyRequest(focusPerm.id)">拒绝</button>
        </div>
        <div class="ap-hint">
          <template v-if="cardCommand(focusPerm)">
            「允许本次」只对这行命令生效，其他高风险命令仍会逐条询问；
            「本会话允许」对本会话内同一命令免问（后端重启即失效）；
            「始终允许」只把这行命令（含其中的目标路径）写入本机持久授权清单，
            今后包括生产环境在内的同一命令不再询问、换一条仍会询问 ——
            拿不准就先选「允许本次」。
          </template>
          <template v-else-if="grantScopesForRisk(focusPerm.risk).includes('session')">
            「本会话允许」对本会话内该工具免问（后端重启即失效），其他会话不受影响。
          </template>
          拒绝后本次运行内 AI 不会再请求同一条命令。
        </div>
      </div>

      <!-- 折叠的排队项（+N 条待处理） -->
      <div v-if="queueCount > 0" class="ap-queue">
        <button class="ap-queue-toggle" :aria-expanded="queueOpen" @click="queueOpen = !queueOpen">
          <svg v-if="queueOpen" class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 15 12 9 18 15"/></svg>
          <svg v-else class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
          <span v-if="queueOpen">收起待处理</span>
          <span v-else>+{{ queueCount }} 条待处理</span>
        </button>
        <div v-if="queueOpen" class="ap-queue-list">
          <button
            v-for="(req, i) in pendingPerms"
            :key="req.id"
            class="ap-queue-item"
            :class="{ 'is-focus': i === focusIdx }"
            @click="focusAt(i)"
          >
            <span class="ap-queue-ic" :class="`risk-${req.risk}`">!</span>
            <code class="ap-queue-cmd" :title="cardCommand(req) || req.tool">{{ cardCommand(req) || req.tool }}</code>
            <span class="ap-risk ap-risk-mini" :class="`risk-${req.risk}`">{{ riskLabel(req.risk) }}</span>
          </button>
        </div>
      </div>

      <!-- 追溯区：超时保留 + 已解决（不删数据） -->
      <details v-if="expiredPerms.length > 0 || resolvedPerms.length > 0" class="ap-trace">
        <summary>
          <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          历史记录（{{ expiredPerms.length }} 超时 · {{ resolvedPerms.length }} 已处理）
        </summary>
        <div v-for="req in expiredPerms" :key="req.id" class="ap-trace-row">
          <code class="ap-trace-cmd" :title="cardCommand(req) || req.tool">{{ cardCommand(req) || req.tool }}</code>
          <span class="ap-trace-status ap-trace-timeout">
            <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            已超时
          </span>
        </div>
        <div v-for="req in resolvedPerms" :key="req.id" class="ap-trace-row">
          <code class="ap-trace-cmd" :title="cardCommand(req) || req.tool">{{ cardCommand(req) || req.tool }}</code>
          <span v-if="req.granted" class="ap-trace-status" :class="`env-${req.environment || 'test'}`">
            <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            已授权
          </span>
          <span v-else class="ap-trace-status ap-trace-denied">
            <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            已拒绝
          </span>
        </div>
      </details>
    </div>

    <!-- 可见终端执行：待确认命令 -->
    <div class="delivery-section" v-if="agentStore.pendingDeliveries.length > 0">
      <div v-for="d in agentStore.pendingDeliveries" :key="d.tool_id" class="ap-card ap-deliver">
        <div class="ap-head">
          <svg class="ap-ic ap-deliver-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
          <span class="ap-ttl">命令待确认</span>
          <span class="ap-risk-pill">将在当前终端执行</span>
        </div>
        <div class="ap-body">
          <pre class="ap-deliver-code">{{ d.command }}</pre>
          <div class="ap-actions">
            <button class="ap-btn ap-btn-grant" @click="agentStore.confirmDeliver(d.tool_id)">确认执行</button>
            <button class="ap-btn ap-btn-deny" @click="agentStore.rejectDeliver(d.tool_id)">拒绝执行</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 可见终端执行：在途交付状态（同因失败聚合 + 长列表折叠） -->
    <div class="ap-card ap-exec" v-if="agentStore.deliveries.some(d => d.status !== 'pending_confirm')">
      <div class="ap-head" :class="{ 'ap-head-clickable': canCollapse }" @click="canCollapse && toggleExecList()">
        <svg class="ap-ic ap-muted-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
        <span class="ap-ttl ap-exec-ttl">已操作 {{ deliveredExec }} 项</span>
        <span class="ap-sp"></span>
        <span
          class="ap-badge"
          :class="deliveredFailCount > 0 ? 'ap-badge-danger' : 'ap-badge-success'"
        >{{ failSummaryText }}</span>
        <!-- 可折叠时才显示展开箭头 -->
        <svg
          v-if="canCollapse"
          class="ap-exec-chevron"
          :class="{ expanded: execListExpanded }"
          viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          width="14" height="14"
        ><polyline points="9 18 15 12 9 6"/></svg>
      </div>
      <div v-show="!canCollapse || execListExpanded" class="ap-summary">
        <span>已记录 <b>{{ deliveredTotal }}</b></span>
        <span>执行 <b>{{ deliveredExec }}</b></span>
        <span :class="deliveredFailCount > 0 ? 'ap-fail' : ''">失败 <b>{{ deliveredFailCount }}</b></span>
        <span>探测 <b>{{ deliveredProbe }}</b></span>
      </div>
      <div v-show="!canCollapse || execListExpanded" class="ap-exec-list">
        <div
          v-for="d in agentStore.deliveries.filter(d => d.status !== 'pending_confirm')"
          :key="d.tool_id"
          class="ap-exec-row"
        >
          <code class="ap-exec-cmd" :title="d.command">{{ d.command }}</code>
          <span class="ap-badge" :class="`ap-badge-${d.status}`">{{ deliveryStatusLabel(d.status, d.exit_code) }}</span>
        </div>
      </div>
      <!-- 折叠态的占位提示（点击展开） -->
      <div v-if="canCollapse && !execListExpanded" class="ap-exec-folded-hint" @click="toggleExecList()">
        点击展开 {{ deliveredExec }} 条命令
      </div>
    </div>

    <!-- 错误 -->
    <div class="error-section" v-if="agentStore.errorMessage">
      <div class="ap-error">
        <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        {{ agentStore.errorMessage }}
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ═══════════ AgentRunPanel 卡片化重构（ui-ux-pro-max：Minimal + JetBrains Mono / OLED 深色） ═══════════
 * 视觉沿用项目的 CSS 变量令牌（status-* / card-* / accent-* / text-*），
 * 语义色与统计口径和既有模型保持一致，仅调整结构、层级与间距。 */
.agent-run-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", "Cascadia Mono", Menlo, Consolas, monospace;
}

/* ── 通用卡片骨架 ── */
.ap-card {
  background: var(--bg-elevated, var(--bg-base));
  border: 1px solid var(--card-warn-border, var(--border-default));
  border-radius: 10px;
  overflow: hidden;
  box-shadow: 0 3px 12px -4px rgba(0, 0, 0, 0.35);
}
.ap-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 13px;
  border-bottom: 1px solid var(--border-default, rgba(255, 255, 255, 0.08));
}
.ap-ic {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  color: var(--text-secondary);
}
.ap-ttl {
  font-weight: 600;
  font-size: 13px;
  letter-spacing: 0.2px;
  color: var(--text-primary);
}
.ap-sp { flex: 1; }
.ap-body { padding: 12px 13px; }

/* ── 规划/意图卡片 ── */
.ap-intention { background: var(--bg-elevated, var(--bg-base)); border-color: var(--status-info-border); }
.ap-level {
  font-size: 11px;
  padding: 2px 9px;
  border-radius: 999px;
  font-weight: 600;
}
.ap-level.level-trivial,
.ap-level.level-simple { background: var(--status-success-bg); color: var(--status-success-text); }
.ap-level.level-moderate { background: var(--status-warn-bg); color: var(--status-warn-text); }
.ap-level.level-complex { background: var(--status-error-bg); color: var(--accent-orange, #f59e0b); }
.ap-level.level-debug { background: var(--status-debug-bg); color: var(--status-debug-text); }
.ap-intention-details {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 13px 10px;
}
.ap-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 5px;
  background: var(--status-tag-bg);
  color: var(--status-tag-text);
  border: 1px solid var(--status-tag-border);
}

/* ── 警告 ── */
.ap-warning {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  font-size: 12.5px;
  color: var(--status-warn-text);
  background: var(--status-warn-bg);
  padding: 8px 12px;
  border-radius: 8px;
}
.ap-warning .ap-ic { margin-top: 2px; color: var(--status-warn-text); }

/* ── 人工介入提示（Task 3：降级为顶部细条通知，非阻塞） ── */
.ap-help-msgbar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--status-warn-text, #f59e0b);
  background: var(--status-warn-bg, var(--bg-elevated, var(--bg-base)));
  border: 1px solid var(--card-warn-border, rgba(245, 158, 11, 0.35));
  border-radius: 8px;
  padding: 6px 10px;
}
.ap-help-msgbar .ap-ic { flex: none; color: var(--status-warn-text, #f59e0b); }
.ap-help-msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ap-help-accent { color: var(--status-warn-text, #f59e0b); }

/* ── 审批托盘头（Task 2：批量操作 + 汇总计数 + 人工介入角标） ── */
.ap-perm-tray {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 13px;
  background: var(--bg-elevated, var(--bg-input, var(--bg-base)));
  border: 1px solid var(--border-default);
  border-radius: 10px;
}
.ap-perm-tray .ap-ic { flex: none; }
.ap-tray-count {
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--card-perm-border, var(--border-default));
  padding: 2px 9px;
  border-radius: 999px;
}
.ap-perm-tray .ap-btn { padding: 4px 11px; }

/* ── 折叠的排队项（+N 条待处理） ── */
.ap-queue { display: flex; flex-direction: column; gap: 4px; }
.ap-queue-toggle {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-secondary);
  background: var(--bg-input, var(--bg-base));
  border: 1px solid var(--border-default);
  border-radius: 7px;
  padding: 5px 11px;
  cursor: pointer;
  transition: background 0.18s, color 0.18s;
}
.ap-queue-toggle:hover { background: var(--bg-hover, var(--bg-input)); color: var(--text-primary); }
.ap-queue-toggle .ap-ic { width: 12px; height: 12px; }
.ap-queue-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 5px;
  background: var(--bg-elevated, var(--bg-base));
}
.ap-queue-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: inherit;
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 6px 9px;
  cursor: pointer;
  transition: background 0.18s, border-color 0.18s;
}
.ap-queue-item:hover { background: var(--bg-hover, var(--bg-input)); }
.ap-queue-item.is-focus {
  background: var(--bg-input, var(--card-perm-bg));
  border-color: var(--card-perm-border, var(--border-default));
}
.ap-queue-ic {
  flex: none;
  width: 16px;
  height: 16px;
  display: grid;
  place-items: center;
  font-weight: 800;
  font-size: 10px;
  border-radius: 50%;
}
.ap-queue-ic.risk-dangerous { background: var(--danger-bg); color: var(--danger-text); }
.ap-queue-ic.risk-moderate { background: var(--status-warn-bg); color: var(--status-warn-text); }
.ap-queue-ic.risk-safe { background: var(--status-success-bg); color: var(--status-success-text); }
.ap-queue-cmd {
  flex: 1;
  min-width: 0;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ap-risk-mini { font-size: 9.5px; padding: 1px 7px; }

/* ── 追溯区：超时保留 + 已解决 ── */
.ap-trace {
  border: 1px solid var(--border-default);
  border-radius: 8px;
  background: var(--bg-input, var(--bg-base));
}
.ap-trace summary {
  cursor: pointer;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 11px;
  color: var(--text-tertiary);
  font-size: 11px;
}
.ap-trace summary::-webkit-details-marker { display: none; }
.ap-trace summary .ap-ic { width: 13px; height: 13px; }
.ap-trace-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 11px;
  border-top: 1px solid var(--border-default);
}
.ap-trace-cmd {
  flex: 1;
  min-width: 0;
  font-family: inherit;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ap-trace-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
}
.ap-trace-status .ap-ic { width: 12px; height: 12px; }
.ap-trace-timeout { color: var(--status-warn-text); }
.ap-trace-denied { color: var(--danger-text); }

/* ── 通用 key/value 行 ── */
.ap-keyval {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  margin-bottom: 7px;
}
.ap-k { color: var(--text-tertiary); flex: none; }
.ap-k-gap { margin-left: 10px; }
.ap-v { color: var(--text-primary); }
.ap-2line {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.ap-code {
  background: var(--bg-input, var(--bg-base));
  border: 1px solid var(--border-default);
  padding: 1px 6px;
  border-radius: 4px;
  color: var(--accent-cyan, #38bdf8);
  font-size: 11.5px;
}
.ap-code-normal { color: var(--accent-cyan, #38bdf8); font-style: italic; }
.ap-tag-inline { font-size: 10.5px; color: var(--text-tertiary); margin-left: 2px; }
.ap-msg { color: var(--text-primary); font-size: 12.5px; line-height: 1.5; margin-bottom: 7px; }

/* 参数折叠块 */
.ap-args {
  border: 1px solid var(--border-default);
  border-radius: 6px;
  background: var(--bg-input, var(--bg-base));
  margin: 4px 0 8px;
}
.ap-args summary {
  cursor: pointer;
  list-style: none;
  padding: 5px 10px;
  color: var(--text-tertiary);
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ap-args summary::-webkit-details-marker { display: none; }
.ap-args summary::before { content: "+"; }
.ap-args[open] summary::before { content: "−"; }
.ap-args pre {
  padding: 0 12px 9px;
  color: var(--text-secondary);
  font-size: 10.5px;
  white-space: pre-wrap;
  overflow-x: auto;
}

/* ── 权限卡片 ── */
.ap-perm { background: var(--bg-elevated, var(--bg-base)); border-color: var(--card-perm-border); }
.ap-perm-resolved { opacity: 0.62; border-color: var(--card-perm-resolved); }
.ap-perm-accent { color: var(--danger-text, #ef4444); }
.ap-perm-ttl { color: var(--card-perm-text); }
.ap-risk { font-size: 10.5px; font-weight: 700; padding: 2px 9px; border-radius: 999px; }
.ap-risk.risk-moderate { background: var(--card-perm-border); color: var(--card-perm-text); }
.ap-risk.risk-dangerous { background: var(--danger-bg); color: var(--danger-text); }
.ap-env { font-size: 10.5px; font-weight: 600; padding: 2px 9px; border-radius: 999px; }
.ap-env.env-prod { background: var(--danger-bg); color: var(--danger-text); }
.ap-env.env-test { background: var(--card-perm-border); color: var(--text-secondary); }
.ap-prod-warn {
  font-size: 11.5px;
  color: var(--danger-text);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  border-radius: 7px;
  padding: 7px 11px;
  margin-bottom: 9px;
  line-height: 1.5;
}

/* ── 通用动作区/按钮 ── */
.ap-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 0 13px 11px; }
.ap-actions-static { padding-top: 4px; }
.ap-btn {
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 7px;
  border: 1px solid transparent;
  cursor: pointer;
  transition: transform 0.15s, background 0.18s, box-shadow 0.18s;
}
.ap-btn:active { transform: translateY(1px); }
.ap-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.ap-btn-grant { background: var(--status-success-bg); border-color: var(--status-success-border); color: var(--status-success-text); }
.ap-btn-grant:hover:not(:disabled) { filter: brightness(1.06); }
.ap-btn-always { background: transparent; border-color: var(--status-success-border); color: var(--status-success-text); }
.ap-btn-always:hover:not(:disabled) { background: var(--status-success-bg); }
.ap-btn-session { background: transparent; border-color: var(--accent-cyan, #38bdf8); color: var(--accent-cyan, #38bdf8); }
.ap-btn-session:hover:not(:disabled) { background: color-mix(in srgb, var(--accent-cyan, #38bdf8) 12%, transparent); }
.ap-btn-deny { background: transparent; border-color: var(--border-default); color: var(--text-secondary); }
.ap-btn-deny:hover:not(:disabled) { border-color: var(--danger-text); color: var(--danger-text); background: var(--danger-bg); }
.ap-resolved {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--status-success-text);
}
.ap-expired { font-size: 12px; color: var(--text-tertiary); }
.ap-hint {
  font-size: 10.5px;
  color: var(--text-tertiary);
  border-top: 1px dashed var(--border-default);
  padding: 8px 13px 10px;
  line-height: 1.6;
}

/* ── 命令待确认 ── */
.ap-deliver { background: var(--bg-elevated, var(--bg-base)); border-color: var(--card-warn-border); }
.ap-deliver-accent { color: var(--status-warn-text, #f59e0b); }
.ap-risk-pill {
  font-size: 10px;
  background: var(--danger-bg);
  color: var(--danger-text);
  padding: 2px 9px;
  border-radius: 999px;
}
.ap-deliver-code {
  margin: 0 0 11px;
  background: var(--bg-input, var(--bg-base));
  border: 1px solid var(--border-default);
  border-radius: 6px;
  padding: 9px 11px;
  font-size: 11.5px;
  color: var(--accent-orange, #f59e0b);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: inherit;
}

/* ── 在途交付状态卡片（命令执行明细） ── */
.ap-exec { margin-top: 2px; }
.ap-exec .ap-head { background: transparent; }
.ap-muted-ic { color: var(--text-tertiary); }
.ap-exec-ttl { color: var(--text-secondary); font-weight: 500; }
.ap-head-clickable { cursor: pointer; user-select: none; }
.ap-head-clickable:hover { color: var(--text-primary); }
.ap-exec-chevron {
  color: var(--text-tertiary);
  transition: transform 0.2s ease;
  flex-shrink: 0;
}
.ap-exec-chevron.expanded { transform: rotate(90deg); }
.ap-exec-folded-hint {
  padding: 6px 13px;
  font-size: 11px;
  color: var(--text-tertiary);
  cursor: pointer;
  text-align: center;
}
.ap-exec-folded-hint:hover { color: var(--text-secondary); }
.ap-summary {
  display: flex;
  gap: 16px;
  font-size: 10.5px;
  color: var(--text-tertiary);
  padding: 8px 13px;
  border-bottom: 1px solid var(--border-default);
}
.ap-summary b { color: var(--text-primary); font-weight: 600; }
.ap-fail b { color: var(--danger-text); }
.ap-exec-list { display: flex; flex-direction: column; }
.ap-exec-row {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 13px;
  border-bottom: 1px solid var(--border-default);
}
.ap-exec-row:last-child { border-bottom: none; }
.ap-exec-cmd {
  flex: 1;
  min-width: 0;
  font-family: inherit;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ap-exec-cmd:hover { white-space: normal; overflow: visible; cursor: help; }

/* ── 徽章：圆点前缀 + 文字 + 配色三重传达（不依赖单一颜色） ── */
.ap-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.4px;
  padding: 2px 9px;
  border-radius: 999px;
  line-height: 1;
}
.ap-badge::before {
  content: "";
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.ap-badge-info { color: var(--status-info-text); background: var(--status-info-bg); }
.ap-badge-danger { color: var(--danger-text); background: var(--danger-bg); }
.ap-badge-success { color: var(--status-success-text); background: var(--status-success-bg); }
.ap-badge-warn,
.ap-badge-capturing,
.ap-badge-writing,
.ap-badge-timeout { color: var(--status-warn-text); background: var(--status-warn-bg); }
.ap-badge-done { color: var(--status-success-text); background: var(--status-success-bg); }
.ap-badge-rejected,
.ap-badge-aborted,
.ap-badge-failed { color: var(--danger-text); background: var(--danger-bg); }

/* ── 错误 ── */
.ap-error {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: var(--status-error-text);
  background: var(--status-error-bg);
  padding: 9px 12px;
  border-radius: 8px;
}
.ap-error .ap-ic { color: currentColor; }

/* ── 通用图标按钮（忽略） ── */
.ap-icon-btn {
  background: none;
  border: none;
  color: var(--text-tertiary);
  width: 26px;
  height: 26px;
  border-radius: 6px;
  cursor: pointer;
  display: grid;
  place-items: center;
  transition: background 0.18s, color 0.18s;
}
.ap-icon-btn:hover { background: var(--bg-hover, var(--bg-input, var(--bg-base))); color: var(--text-primary); }
.ap-icon-btn svg { width: 14px; height: 14px; }

@media (prefers-reduced-motion: reduce) {
  .ap-btn, .ap-icon-btn { transition: none; }
}
</style>
