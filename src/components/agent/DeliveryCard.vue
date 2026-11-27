<script setup lang="ts">
/**
 * DeliveryCard — 可见终端交付的逐条确认卡，消费方是输入区上方的 ApprovalDock
 *
 * 与 ApprovalCard 的分工：权限卡回答「能不能执行」，本卡回答「现在是否把它写进
 * 可见终端」。两者都是运行期的瞬时决策，所以都只活在输入区上方，不跟消息流滚动。
 *
 * 命令块只做换行、不做 shell 语法拆行：拆错引号/heredoc 会把「一条链」画成
 * 「多条独立命令」，用户据此拍板就失真了。原样展示，靠空格与分隔符自然折行。
 */
import { computed, ref } from 'vue'
import type { DeliverySegment, ToolDelivery } from '@/composables/useAgentDeliver'
import { useAgentDeliver } from '@/composables/useAgentDeliver'
import { useAgentStore } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { approvalTargetLabel } from '@/utils/approval-display'

const props = defineProps<{
  delivery: ToolDelivery
}>()

const agentStore = useAgentStore()
const sshStore = useSSHStore()
const deliverApi = useAgentDeliver()

const targetLabel = computed(() =>
  approvalTargetLabel(
    { sshSessionId: props.delivery.session_id },
    sshStore.sessions,
    sshStore.connections,
  ),
)

/** prod 徽章与权限卡同源：从会话反查连接；查不到就不冒充测试环境 */
const isProd = computed(() => {
  const session = sshStore.sessions.find(s => s.id === props.delivery.session_id)
  const conn = session?.connectionId
    ? sshStore.connections.find(c => c.id === session.connectionId)
    : undefined
  return conn?.environment === 'prod'
})

/** 卡头风险徽章：聚合级别由后端拆解层给出（展示用，权威判定在授权链）。
 *  旧后端不带 risk 字段时不渲染 —— 宁缺毋滥，别让徽章说没根据的话。 */
const riskBadge = computed(() => {
  const level = props.delivery.risk
  if (level === 'safe') {
    // P1b：safe 只代表免审批；证明只读时才敢说「不改动任何文件」
    return props.delivery.readonly
      ? { text: '只读 · 不改动任何文件', cls: 'dc-risk-safe' }
      : { text: '免审批 · 可能写入磁盘', cls: 'dc-risk-warn' }
  }
  if (level === 'dangerous') return { text: '含写入操作', cls: 'dc-risk-bad' }
  if (level === 'hardline') return { text: '高危操作', cls: 'dc-risk-bad' }
  return null
})

/** 复合命令逐段拆解（后端拆解层，单段命令不带 segments） */
const segments = computed(() =>
  (props.delivery.segments ?? []).length > 1 ? props.delivery.segments! : [])

/** 只有真正拿到非零退出码的段才算失败：红色不再表示「非只读风险」 */
function isStepFailed(seg: DeliverySegment): boolean {
  return typeof seg.exit_code === 'number' && seg.exit_code !== 0
}
const failedStepCount = computed(() => segments.value.filter(isStepFailed).length)
const firstFailedStep = computed(() => {
  const i = segments.value.findIndex(isStepFailed)
  if (i < 0) return null
  return { index: i + 1, code: segments.value[i].exit_code as number }
})

/** 退出码列只在真的拿到逐段状态后出现：待确认的卡不必挂一列全是「—」 */
const showExitColumn = computed(() =>
  segments.value.some(s => typeof s.exit_code === 'number'))

/** 执行结果是否已经产生（回传失败 / 执行失败 / 超时的卡要展示退出码与原因） */
const hasResult = computed(() =>
  props.delivery.exit_code !== null
  || !!props.delivery.output
  || props.delivery.status === 'failed'
  || props.delivery.status === 'timeout')
const resultFailed = computed(() =>
  (props.delivery.exit_code !== null && props.delivery.exit_code !== 0)
  || failedStepCount.value > 0)
const exitLabel = computed(() =>
  props.delivery.exit_code === null ? '未知' : String(props.delivery.exit_code))
/** 结果摘要：优先点名失败段（整条退出码在 ; 链里只等于最后一段，容易误读为成功） */
const resultSummary = computed(() => {
  const head = firstFailedStep.value
    ? (failedStepCount.value > 1
      ? `第 ${firstFailedStep.value.index} 步起失败，共 ${failedStepCount.value} 步（首段退出码 ${firstFailedStep.value.code}）`
      : `第 ${firstFailedStep.value.index} 步退出码 ${firstFailedStep.value.code}`)
    : `退出码 ${exitLabel.value}`
  return props.delivery.failure_reason ? `${head}：${props.delivery.failure_reason}` : head
})

/** 查看详情：卡内展开（完整原文 + 完整输出），不引入额外弹窗层级 */
const detailsOpen = ref(false)
const hasDetails = computed(() => !!props.delivery.command || !!props.delivery.output)

/**
 * Q-F6 恢复入口：结果回传自动重试耗尽（postFailed）的交付在这里给出
 * 「重发结果」按钮（调 useAgentDeliver.retryPostResult）。没有它，回传彻底
 * 失败就是仅 console.error 的死路终态 —— 后端挂起的循环只能干等 TTL。
 * 这类交付已是终态：确认/拒绝按钮换下，卡头文案也不再是「待确认」。
 */
const isPostFailed = computed(() => props.delivery.postFailed === true)
const cardTitle = computed(() => {
  if (isPostFailed.value) return '结果回传失败'
  if (props.delivery.status === 'failed') return '执行失败'
  if (props.delivery.status === 'timeout') return '执行超时'
  return '需要你确认'
})
const resending = ref(false)

async function resendResult(): Promise<void> {
  if (resending.value) return
  resending.value = true
  try {
    await deliverApi.retryPostResult(props.delivery.tool_id)
  } finally {
    resending.value = false
  }
}
</script>

<template>
  <div class="ap-card delivery-card">
    <div class="ap-head">
      <span class="ap-ttl">{{ cardTitle }}</span>
      <span class="delivery-target" :title="targetLabel || '当前终端'">{{ targetLabel || '当前终端' }}</span>
      <span v-if="isProd" class="ap-env env-prod">生产环境</span>
      <span v-if="riskBadge" class="dc-risk" :class="riskBadge.cls">{{ riskBadge.text }}</span>
      <span class="ap-sp"></span>
    </div>
    <div class="ap-body">
      <!-- 阅读顺序与审批卡一致：你的请求 → 这条命令会做什么 → 逐段拆解 → 原文。
           解释由后端解释层随 tool_deliver 下发（纯展示，不参与任何放行判定），
           失败/未携带时不渲染 -->
      <div v-if="delivery.task" class="ap-keyval" :title="delivery.task">
        <span class="ap-k">你的请求</span>
        <span class="ap-v ap-2line">{{ delivery.task }}</span>
      </div>
      <!-- 回传诊断（回传失败/写入失败等原因，useAgentDeliver 落在 delivery.error）：
           此前卡片不渲染它，回传彻底失败时用户只能看到一张沉默的卡 -->
      <div v-if="delivery.error" class="ap-keyval">
        <span class="ap-k">回传诊断</span>
        <span class="ap-v dc-err">{{ delivery.error }}</span>
      </div>
      <div v-if="delivery.explain" class="ap-keyval">
        <span class="ap-k">这条命令会做什么</span>
        <span class="ap-v">{{ delivery.explain }}</span>
      </div>
      <!-- 执行结果：退出码 + 失败原因。红色只属于真的失败，不再拿风险等级冒充失败 -->
      <div v-if="hasResult" class="ap-keyval">
        <span class="ap-k">执行结果</span>
        <span class="ap-v" :class="{ 'dc-err': resultFailed }">{{ resultSummary }}</span>
      </div>
      <!-- 复合命令：逐段「意图 + 命令片段 + 退出码」表格。
           非只读段用琥珀色底色提示核对（不是失败）；只有退出码非零的段才标红。 -->
      <div v-if="segments.length" class="seg-table-wrap">
        <table class="seg-table">
          <thead>
            <tr>
              <th class="seg-col-idx">#</th>
              <th class="seg-col-intent">意图</th>
              <th class="seg-col-cmd">命令</th>
              <th v-if="showExitColumn" class="seg-col-result">退出码</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(seg, i) in segments"
              :key="i"
              :class="{
                'seg-row-risky': seg.level !== 'safe',
                'seg-row-failed': isStepFailed(seg),
              }"
            >
              <td class="seg-idx" :class="{ 'seg-idx-bad': isStepFailed(seg) }">{{ i + 1 }}</td>
              <td class="seg-intent">{{ seg.intent }}</td>
              <td class="seg-cmd">{{ seg.command }}</td>
              <td class="seg-result" v-if="showExitColumn" :class="{ 'seg-result-bad': isStepFailed(seg) }">
                {{ seg.exit_code === undefined || seg.exit_code === null ? '—' : seg.exit_code }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <!-- 查看详情：完整命令原文 + 完整输出（含失败段前后的全部回显） -->
      <div v-if="detailsOpen" class="dc-details">
        <div class="dc-details-k">完整命令</div>
        <pre class="delivery-code">{{ delivery.command || '（无）' }}</pre>
        <div class="dc-details-k">执行输出</div>
        <pre class="delivery-code dc-output">{{ delivery.output || '（无输出）' }}</pre>
      </div>
      <pre v-if="!segments.length && !detailsOpen" class="delivery-code">{{ delivery.command }}</pre>
    </div>
    <div class="ap-actions">
      <!-- 回传失败是终态：只保留「查看详情」+「重发结果」，不混入确认/拒绝等结算按钮。
           详情必须无条件可点 —— 回传失败恰恰是最需要看原始输出的时候。 -->
      <template v-if="isPostFailed">
        <button class="ap-btn ap-btn-always" @click="detailsOpen = !detailsOpen">
          {{ detailsOpen ? '收起详情' : '查看详情' }}
        </button>
        <button class="ap-btn ap-btn-grant" :disabled="resending" @click="resendResult">
          {{ resending ? '重发中…' : '重发结果' }}
        </button>
      </template>
      <template v-else-if="delivery.status === 'pending_confirm'">
        <button class="ap-btn ap-btn-grant" @click="agentStore.confirmDeliver(delivery.tool_id)">确认执行</button>
        <button class="ap-btn ap-btn-deny" @click="agentStore.rejectDeliver(delivery.tool_id)">拒绝执行</button>
      </template>
      <!-- 执行失败/超时的结果卡：只看结论，不给结算 -->
      <button v-else-if="hasDetails" class="ap-btn ap-btn-always" @click="detailsOpen = !detailsOpen">
        {{ detailsOpen ? '收起详情' : '查看详情' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 与审批停靠区的 .ad-body 同高：整卡有界后正文自己滚，确认/拒绝不会落到可视区外 */
.delivery-card {
  border-color: var(--card-warn-border);
  max-height: min(38vh, 340px);
}

/* 回传诊断行：失败原因用告警红，与风险徽章同色系 */
.dc-err { color: var(--accent-red); }

/* 多会话时这是卡头唯一的位置信息：写进哪台机器。过长时截断，完整值留在 title。 */
.delivery-target {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px var(--spacing-8);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-pill);
  font-family: var(--font-mono);
  font-size: 0.8077rem;
  color: var(--text-secondary);
}

/* break-all 会把路径从中间截断；改成只在空格/分隔符处折行，超长无空格
   token 才硬断。限高保证极端长命令不会把输入区顶出视口。 */
.delivery-code {
  margin: 0;
  max-height: 180px;
  overflow-y: auto;
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  padding: 9px 11px;
  font-family: var(--font-mono);
  font-size: 0.8846rem;
  line-height: 1.55;
  color: var(--text-primary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 卡头风险徽章：聚合级别展示（safe 只读绿 / 非 safe 警示红）。
   底色用文字令牌自身 12% 混入，与 col-badge 同一套派生法，不吃死色值。 */
.dc-risk {
  font-size: 0.8077rem;
  padding: 1px var(--spacing-8);
  border-radius: var(--radius-sm);
  white-space: nowrap;
}
.dc-risk-safe { color: var(--accent-green); background: color-mix(in srgb, var(--accent-green) 12%, transparent); }
.dc-risk-bad { color: var(--accent-red); background: color-mix(in srgb, var(--accent-red) 12%, transparent); }
.dc-risk-warn { color: var(--status-warn-text); background: color-mix(in srgb, var(--status-warn-text) 12%, transparent); }

/* 逐段拆解表：# / 意图 / 命令 / 退出码 四列。命令列折行不截断；
   非只读段用琥珀底色提示核对（风险不是失败），失败段（退出码非零）才是红色。 */
.seg-table-wrap {
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  overflow: hidden;
}
.seg-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9231rem;
}
.seg-table th {
  text-align: left;
  font-weight: 500;
  font-size: 0.7692rem;
  color: var(--text-tertiary);
  white-space: nowrap;
  padding: var(--spacing-6) var(--spacing-10);
  background: var(--bg-input);
  border-bottom: 1px solid var(--border-subtle);
}
.seg-table td {
  padding: var(--spacing-6) var(--spacing-10);
  vertical-align: top;
  border-top: 1px solid var(--border-subtle);
}
.seg-table tbody tr:first-child td { border-top: none; }
.seg-col-idx { width: 28px; }
.seg-col-result { width: 52px; white-space: nowrap; }
.seg-row-risky td { background: color-mix(in srgb, var(--status-warn-text) 6%, transparent); }
.seg-row-failed td { background: color-mix(in srgb, var(--accent-red) 8%, transparent); }
.seg-idx { color: var(--text-tertiary); }
.seg-idx-bad { color: var(--accent-red); font-weight: 600; }
.seg-intent { color: var(--text-primary); line-height: 1.45; }
.seg-cmd {
  font-family: var(--font-mono);
  font-size: 0.8077rem;
  color: var(--text-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.seg-row-risky .seg-idx { color: var(--status-warn-text); }
.seg-row-failed .seg-idx,
.seg-row-failed .seg-cmd { color: var(--accent-red); }
.seg-result {
  font-family: var(--font-mono);
  font-size: 0.8077rem;
  color: var(--text-tertiary);
  white-space: nowrap;
}
.seg-result-bad { color: var(--accent-red); font-weight: 600; }

/* 查看详情：完整命令 + 完整输出（各自限高滚动，长回显不顶爆卡片） */
.dc-details { display: flex; flex-direction: column; gap: var(--spacing-4); }
.dc-details-k { font-size: 0.7692rem; color: var(--text-tertiary); }
.dc-output { max-height: 200px; }
</style>
