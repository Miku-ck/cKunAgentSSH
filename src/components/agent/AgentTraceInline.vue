<script setup lang="ts">
/**
 * 内联执行过程 —— 助手回合正文上方的过程块（任务台 spec 故事 22–30）
 *
 * 数据只有两份：trace.toolCalls（按 iteration 归批）与运行期的 pendingPermissions
 * （按时间戳归并）。两者各自的分组口径都在 utils/run-trace，这里只负责把组头、
 * 工具行、审批卡按流顺序摆出来 —— 分组逻辑再抄一遍就会出现「第 3 步的组头下面
 * 是另一步的调用」这种错位。
 *
 * 折叠语义：运行中默认展开，收尾自动收起为一行摘要；用户收起/展开后不会被后续
 * 重渲染重置（只有 isLive 的 true→false 那一次会自动收起）。
 *
 * 审批卡只对当前 run 可见：approvals 由宿主只在 live 消息上传入。审批请求不写入
 * agentTrace（持久化需要后端契约），历史回看没有审批卡，追溯去运维审计/轨迹回放。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import type { AgentTraceSnapshot } from '@/api/client'
import type { PermissionRequest } from '@/stores/agent'
import { useAgentStore } from '@/stores/agent'
import { useToastStore } from '@/stores/toast'
import { useApprovalActions } from '@/composables/useApprovalActions'
import { onceGrantBlocked } from '@/utils/approval-scopes'
import { headline, processStatusLabel, toolState } from '@/utils/process-status'
import type { ProcessMeta } from '@/utils/process-status'
import { buildTraceModel, mergeStream } from '@/utils/run-trace'
import type { TraceEventView, TraceStepView } from '@/utils/run-trace'
import ApprovalCard from './ApprovalCard.vue'

const props = withDefaults(defineProps<{
  trace: AgentTraceSnapshot
  isLive?: boolean
  approvals?: PermissionRequest[]
  meta?: ProcessMeta
}>(), {
  isLive: false,
  approvals: () => [],
})

const agent = useAgentStore()
const toast = useToastStore()
const { settleAll } = useApprovalActions()

const model = computed(() => buildTraceModel(props.trace.toolCalls))
const stream = computed(() => mergeStream(model.value, props.approvals))

const pendingCount = computed(() => props.approvals.length)
const traceId = computed(() => props.meta?.runId || props.trace.lastRunId || '')

const expanded = ref(props.isLive)
watch(() => props.isLive, (live) => {
  // 只处理「运行结束」这一次自动收起；用户随后手动展开不再被任何重渲染打断
  if (!live) expanded.value = false
})

const status = computed(() => processStatusLabel(props.trace, props.isLive))
const headText = computed(() => {
  if (props.isLive) {
    return model.value.activeStepIndex >= 0
      ? `执行中 · 第 ${model.value.activeStepIndex + 1} 步`
      : '思考中'
  }
  const steps = model.value.steps.length ? `已执行 ${model.value.steps.length} 步` : ''
  if (status.value.text === '已完成') return steps || status.value.text
  return steps ? `${status.value.text} · ${steps}` : status.value.text
})

// ── 整程计时：运行中只挂一个 interval，收尾后读快照里的权威时长 ──
const nowTick = ref(0)
let ticker: ReturnType<typeof setInterval> | null = null

function stopTicker() {
  if (ticker === null) return
  clearInterval(ticker)
  ticker = null
}

watch(() => props.isLive, (live) => {
  stopTicker()
  if (!live) return
  nowTick.value = Date.now()
  ticker = setInterval(() => { nowTick.value = Date.now() }, 1000)
}, { immediate: true })

onUnmounted(stopTicker)

const elapsedMs = computed(() => {
  if (props.isLive) {
    return agent.runStartedAt > 0 ? Math.max(0, nowTick.value - agent.runStartedAt) : 0
  }
  if (typeof props.trace.duration === 'number' && props.trace.duration > 0) {
    return props.trace.duration * 1000
  }
  return props.meta?.durationMs ?? 0
})
const durationText = computed(() => elapsedMs.value > 0 ? `${(elapsedMs.value / 1000).toFixed(1)}s` : '')

// ── 按流顺序摊平：组头与批量头是插入项，不是 stream 的第二种数据 ──
interface StepRow { kind: 'step'; key: string; step: TraceStepView }
interface ApprovalHeadRow { kind: 'approval-head'; key: string; count: number }
interface ApprovalRow { kind: 'approval'; key: string; req: PermissionRequest }
interface ToolRow { kind: 'tool'; key: string; entry: TraceEventView }
type Row = StepRow | ApprovalHeadRow | ApprovalRow | ToolRow

const rows = computed<Row[]>(() => {
  const out: Row[] = []
  let lastStepIndex = -1
  let approvalHeadAdded = false
  for (const item of stream.value) {
    if (item.kind === 'tool') {
      if (item.stepIndex !== lastStepIndex) {
        lastStepIndex = item.stepIndex
        out.push({ kind: 'step', key: `s:${item.stepIndex}`, step: model.value.steps[item.stepIndex] })
      }
      out.push({ kind: 'tool', key: item.key, entry: item.entry })
      continue
    }
    if (!approvalHeadAdded && pendingCount.value > 1) {
      approvalHeadAdded = true
      out.push({ kind: 'approval-head', key: 'approval-head', count: pendingCount.value })
    }
    out.push({ kind: 'approval', key: item.key, req: item.req })
  }
  return out
})

function stepNo(step: TraceStepView): string {
  return step.iteration === null ? '未编号' : `第 ${step.iteration + 1} 步`
}
function stepDuration(step: TraceStepView): string {
  if (step.durationMs !== undefined) return `${(step.durationMs / 1000).toFixed(1)}s`
  return step.state === 'running' ? '执行中' : ''
}
/** 只有部分调用量到耗时：求和值偏小，必须标出来，不能当整批耗时读 */
function stepPartial(step: TraceStepView): boolean {
  return step.durationMs !== undefined && step.measuredCount < step.eventIndexes.length
}

// ── 工具行 ──
const UNWRITTEN_RESULT = '(运行结束前未回写结果)'

function toolStatus(entry: TraceEventView): string {
  if (entry.result === UNWRITTEN_RESULT) return '未回写'
  const state = toolState(entry)
  if (state === 'running') return '执行中'
  return state === 'failed' ? '失败' : '成功'
}
function toolTone(entry: TraceEventView): 'running' | 'ok' | 'bad' | 'muted' {
  if (entry.result === UNWRITTEN_RESULT) return 'muted'
  const state = toolState(entry)
  return state === 'running' ? 'running' : state === 'failed' ? 'bad' : 'ok'
}
function itemDuration(entry: TraceEventView): string {
  return entry.durationMs !== undefined ? `${(entry.durationMs / 1000).toFixed(1)}s` : ''
}
function outputOf(entry: TraceEventView): string {
  if (!entry.result || entry.result === UNWRITTEN_RESULT) return ''
  return entry.result
}
function outputLines(entry: TraceEventView): string[] {
  const text = outputOf(entry)
  return text ? text.split('\n') : []
}
function outputPreview(entry: TraceEventView): string {
  return outputLines(entry).slice(0, 3).join('\n')
}

/** 输出展开状态按工具调用 id 记；行 key 就是 `t:${id}`，不随 rerender 变 */
const openOutputs = ref<Set<string>>(new Set())
function toggleOutput(key: string) {
  const next = new Set(openOutputs.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  openOutputs.value = next
}

// ── 批量放行：只授 once，run 结束后 once 不可达 ──
const batching = ref(false)
const onceBlocked = computed(() => onceGrantBlocked(agent.isRunning))

async function batch(kind: 'approve' | 'deny') {
  if (batching.value) return
  batching.value = true
  try {
    await settleAll(kind)
  } finally {
    batching.value = false
  }
}

function copyTrace() {
  if (!traceId.value) return
  navigator.clipboard.writeText(traceId.value).then(
    () => toast.success(`trace ${traceId.value} 已复制，粘到运维审计的 trace 框即可找回这一轮`),
    () => toast.error('复制失败，请手动选中 trace 号'),
  )
}
</script>

<template>
  <section class="trace-inline" :class="{ collapsed: !expanded }" aria-label="执行过程">
    <header class="tr-head">
      <button
        type="button"
        class="tr-toggle"
        :aria-expanded="expanded"
        :title="expanded ? '收起执行过程' : '展开执行过程'"
        @click="expanded = !expanded"
      >
        <svg class="tr-chev" :class="{ open: expanded }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" width="10" height="10" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>
        <span class="tr-dot" :class="status.tone" aria-hidden="true"></span>
        <span class="tr-status" :class="status.tone">{{ headText }}</span>
        <span v-if="durationText" class="tr-dur">{{ durationText }}</span>
      </button>
      <span class="tr-sp"></span>
      <span v-if="pendingCount" class="tr-pending">{{ pendingCount }} 条待放行</span>
      <button
        v-if="traceId"
        type="button"
        class="tr-trace"
        title="点击复制完整 trace 号，运维审计页按它筛这一轮的全部记录"
        @click="copyTrace"
      >№{{ traceId }}</button>
    </header>

    <div v-if="expanded" class="tr-body">
      <template v-for="row in rows" :key="row.key">
        <div v-if="row.kind === 'step'" class="tr-step">
          <span class="tr-step-no">{{ stepNo(row.step) }}</span>
          <span v-if="stepDuration(row.step)" class="tr-step-dur">{{ stepDuration(row.step) }}</span>
          <span v-if="stepPartial(row.step)" class="tr-step-note">部分计时</span>
          <span class="tr-step-line" aria-hidden="true"></span>
        </div>

        <div v-else-if="row.kind === 'approval-head'" class="tr-ap-head">
          <span class="tr-ap-count">{{ row.count }} 条待你放行</span>
          <span class="tr-sp"></span>
          <button type="button" class="tr-b" :disabled="batching" @click="batch('deny')">
            全部拒绝
          </button>
          <button
            type="button"
            class="tr-b"
            :disabled="batching || onceBlocked"
            :title="onceBlocked ? '本轮运行已结束，once 授权无处生效' : ''"
            @click="batch('approve')"
          >{{ batching ? '处理中…' : '全部批准本次' }}</button>
        </div>

        <ApprovalCard v-else-if="row.kind === 'approval'" :req="row.req" />

        <div v-else-if="row.kind === 'tool'" class="tr-tool">
          <div class="tr-tool-head">
            <span class="tr-tool-state" :class="toolTone(row.entry)">{{ toolStatus(row.entry) }}</span>
            <span class="tr-tool-name mono">{{ row.entry.tool }}</span>
            <code class="tr-cmd mono" :title="headline(row.entry)">{{ headline(row.entry) }}</code>
            <span v-if="itemDuration(row.entry)" class="tr-item-dur mono">{{ itemDuration(row.entry) }}</span>
          </div>
          <template v-if="outputLines(row.entry).length">
            <pre class="tr-out mono">{{
              openOutputs.has(row.key) ? outputOf(row.entry) : outputPreview(row.entry)
            }}</pre>
            <button
              v-if="outputLines(row.entry).length > 3"
              type="button"
              class="tr-out-toggle"
              :aria-expanded="openOutputs.has(row.key)"
              @click="toggleOutput(row.key)"
            >{{ openOutputs.has(row.key) ? '收起输出' : `展开输出（${outputLines(row.entry).length} 行）` }}</button>
          </template>
        </div>
      </template>

      <div v-if="isLive && !rows.length" class="tr-preparing">
        <span>思考中</span>
        <span class="tr-dots" aria-hidden="true"><span></span><span></span><span></span></span>
      </div>
      <p v-else-if="!rows.length" class="tr-empty">这次运行没有产生工具调用</p>

      <div v-if="meta" class="tr-meta">
        <span v-if="meta.modelName">模型 <span class="tr-meta-v">{{ meta.modelName }}</span></span>
        <span v-if="meta.permissionLabel">审批 <span class="tr-meta-v">{{ meta.permissionLabel }}</span></span>
        <span v-if="meta.targetLabel" class="mono">目标 <span class="tr-meta-v">{{ meta.targetLabel }}</span></span>
        <span v-if="meta.durationMs">耗时 <span class="tr-meta-v">{{ (meta.durationMs / 1000).toFixed(1) }}s</span></span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.trace-inline {
  font-family: var(--font-sans);
  margin-bottom: 12px;
  min-width: 0;
}

.tr-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 12px;
}

.tr-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  padding: 3px 4px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.tr-toggle:hover { background: var(--bg-hover); color: var(--text-primary) }

.tr-chev {
  flex: none;
  color: var(--text-tertiary);
  transition: transform var(--transition-fast);
}
.tr-chev.open { transform: rotate(90deg) }

.tr-dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.tr-dot.run { color: var(--accent-primary); animation: tr-blink 1.1s steps(2, start) infinite }
.tr-dot.ok { color: var(--text-tertiary) }
.tr-dot.bad { color: var(--danger-text) }

.tr-status { font-weight: 600; white-space: nowrap }
.tr-status.run { color: var(--accent-primary) }
.tr-status.ok { color: var(--text-secondary) }
.tr-status.bad { color: var(--danger-text) }

.tr-dur {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

.tr-sp { flex: 1 }

.tr-pending {
  flex: none;
  font-size: 11px;
  font-weight: 600;
  color: var(--status-warn-text);
  white-space: nowrap;
}

.tr-trace {
  flex: none;
  padding: 0;
  border: 0;
  background: none;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
  text-decoration: underline dotted;
  text-underline-offset: 2px;
  cursor: pointer;
  white-space: nowrap;
}
.tr-trace:hover { color: var(--text-primary) }

.tr-body {
  margin-top: 8px;
  padding-left: 4px;
  min-width: 0;
}

/* ── 步骤组头 ── */
.tr-step {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 3px;
  font-size: 11px;
  color: var(--text-tertiary);
}
.tr-step:first-child { margin-top: 2px }
.tr-step-no { color: var(--text-secondary); font-weight: 600; white-space: nowrap }
.tr-step-dur { font-family: var(--font-mono); white-space: nowrap }
.tr-step-note { font-size: 10.5px; white-space: nowrap }
.tr-step-line {
  flex: 1;
  height: 1px;
  background: var(--border-subtle);
}

/* ── 工具行 ── */
.tr-tool {
  padding: 3px 0 3px 10px;
  min-width: 0;
}
.tr-tool-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.tr-tool-state {
  flex: none;
  width: 36px;
  font-size: 11px;
  font-weight: 600;
}
.tr-tool-state.running { color: var(--accent-primary) }
.tr-tool-state.ok { color: var(--status-success-text) }
.tr-tool-state.bad { color: var(--danger-text) }
.tr-tool-state.muted { color: var(--text-tertiary) }
.tr-tool-name {
  flex: none;
  font-size: 11.5px;
  color: var(--text-secondary);
}
.tr-cmd {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
  color: var(--text-secondary);
}
.tr-item-dur {
  flex: none;
  font-size: 10.5px;
  color: var(--text-tertiary);
}

.tr-out {
  margin: 4px 0 2px;
  padding: 6px 9px;
  max-height: 180px;
  overflow: auto;
  background: var(--bg-input);
  border-left: 2px solid var(--border-default);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
.tr-out-toggle {
  padding: 2px 0;
  border: 0;
  background: none;
  font-size: 11px;
  color: var(--text-tertiary);
  cursor: pointer;
}
.tr-out-toggle:hover { color: var(--text-secondary) }

/* ── 审批就地 ── */
.tr-ap-head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 10px 0 6px;
  font-size: 11px;
}
.tr-ap-count { color: var(--status-warn-text); font-weight: 600 }
.tr-b {
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-primary);
  font-size: 11.5px;
  white-space: nowrap;
  transition: background var(--transition-fast);
}
.tr-b:hover:not(:disabled) { background: var(--bg-hover) }
.tr-b:disabled { opacity: 0.5; cursor: default }

:deep(.ap-card) { margin: 8px 0 }

/* ── 占位与空态 ── */
.tr-preparing {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 0;
  font-size: 13px;
  color: var(--accent-primary);
}
.tr-dots { display: inline-flex; gap: 3px }
.tr-dots span {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--accent-primary);
  animation: tr-bounce 1.4s infinite ease-in-out both;
}
.tr-dots span:nth-child(1) { animation-delay: -0.32s }
.tr-dots span:nth-child(2) { animation-delay: -0.16s }
.tr-empty {
  padding: 8px 0;
  font-size: 12px;
  color: var(--text-tertiary);
}

/* ── 归因 ── */
.tr-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
}
.tr-meta-v { color: var(--text-secondary) }

.mono { font-family: var(--font-mono) }

@keyframes tr-blink { 50% { opacity: 0.35 } }
@keyframes tr-bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4 }
  40% { transform: scale(1); opacity: 1 }
}

@media (prefers-reduced-motion: reduce) {
  .tr-dot.run,
  .tr-dots span { animation: none }
}
</style>
