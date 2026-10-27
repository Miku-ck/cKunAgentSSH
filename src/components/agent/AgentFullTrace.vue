<template>
  <div class="full-trace">
    <p v-if="loading" class="ft-hint">加载轨迹…</p>
    <p v-else-if="error" class="ft-hint err">{{ error }}
      <button class="ft-retry" @click="fetchTrace">重试</button>
    </p>

    <template v-else-if="trace">
      <!-- 元信息条：整表都是「这一条 run 跑成了什么样」的汇总 -->
      <div class="ft-meta">
        <!-- S9-13：run.status 是后端原始终态（completed/failed/human_help/
             max_reached），二元 `=== 'completed' ? '完成' : '失败'` 会把
             「迭代耗尽」和「需要人工介入」都念成「失败」——这三种结局用户
             下一步该做的事完全不同。标签与配色走共享判定。 -->
        <span class="ft-chip" :class="runStatusChip(trace.run.status)">
          {{ runStatusLabel(trace.run.status) }}
        </span>
        <span class="ft-chip">迭代 {{ trace.run.iteration ?? '-' }}/{{ trace.run.max_iterations ?? '-' }}</span>
        <span class="ft-chip">{{ (trace.cost.duration_ms / 1000).toFixed(1) }}s</span>
        <span class="ft-chip">{{ trace.cost.total_tokens }} tok</span>
        <span class="ft-chip">${{ trace.cost.cost_usd.toFixed(4) }}</span>
        <span class="ft-chip">{{ trace.cost.tool_call_count }} 次调用</span>
        <span v-if="trace.run.model_used" class="ft-chip muted">{{ trace.run.model_used }}</span>
      </div>

      <!-- 功能 B：run 级一键回滚入口。面板自带可用性闸与无数据自隐藏（404 不渲染），
           挂在元信息条下、展开行与消息气泡轨迹两处复用同一份。 -->
      <RollbackPanel :run-id="runId" />

      <!-- 状态流转链：压缩连续重复，只保留迁移节点（init→thinking→acting→…→completed） -->
      <div v-if="stateChain.length" class="ft-states">
        <span class="ft-states-label">状态</span>
        <span class="ft-state-chain">
          <template v-for="(s, i) in stateChain" :key="i">
            <span v-if="i > 0" class="ft-arrow">→</span>
            <span class="ft-state">{{ s }}</span>
          </template>
        </span>
      </div>

      <!-- 执行过程：DB 里的 tool_calls，按 iteration 归步。这是记录面，
           审批不在其中 —— 待决请求只活在运行期，从不写入轨迹。 -->
      <div v-if="process.events.length" class="ft-tools">
        <template v-for="(entry, i) in process.events" :key="entry.id">
          <div
            v-if="i === 0 || process.events[i - 1].stepIndex !== entry.stepIndex"
            class="ft-step"
          >
            <span class="ft-step-no">{{ stepNo(stepOf(entry)) }}</span>
            <span v-if="stepDuration(stepOf(entry))" class="ft-step-dur">{{ stepDuration(stepOf(entry)) }}</span>
            <span v-if="stepPartial(stepOf(entry))" class="ft-step-note">部分计时</span>
            <span class="ft-step-line" aria-hidden="true"></span>
          </div>

          <div class="ft-tool">
            <div class="ft-tool-head">
              <span class="ft-tool-state" :class="toolTone(entry)">{{ toolStatus(entry) }}</span>
              <span class="ft-tool-name mono">{{ entry.tool }}</span>
              <code class="ft-cmd mono" :title="headline(entry)">{{ headline(entry) }}</code>
              <span v-if="itemDuration(entry)" class="ft-item-dur mono">{{ itemDuration(entry) }}</span>
            </div>
            <template v-if="outputLines(entry).length">
              <pre class="ft-out mono">{{
                openOutputs.has(entry.id) ? outputOf(entry) : outputPreview(entry)
              }}</pre>
              <button
                v-if="outputLines(entry).length > 3"
                type="button"
                class="ft-out-toggle"
                :aria-expanded="openOutputs.has(entry.id)"
                @click="toggleOutput(entry.id)"
              >{{ openOutputs.has(entry.id) ? '收起输出' : `展开输出（${outputLines(entry).length} 行）` }}</button>
            </template>
          </div>
        </template>
      </div>
      <p v-else class="ft-hint muted">这次运行没有产生工具调用</p>

      <!-- 行上已印着 trace 号、发起时间和各档汇总，这里只补行里没有的结束时刻。
           两端都必须过 formatBackendTs：后端存的是 UTC naive 串（database._now），
           原样印 = 比本地钟面早 8 小时，而同一张表的行上印的是本地时间（AgentRunsTable），
           同一条 run 上下两个时刻互相矛盾 —— 这是本仓记过的「时区接缝」家族。 -->
      <p v-if="trace.run.completed_at" class="ft-time muted">
        结束 {{ formatBackendTs(trace.run.completed_at) }}
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
/**
 * AgentFullTrace — 单条 run 的权威记录（运维轨迹 → 运行列表展开行）
 *
 * 数据只有一个来源：GET /agent/runs/{id}/trace（agent_runs + tool_calls）。
 * 会话气泡不再内联执行过程：AI 界面是对话，执行过程是记录 —— 两个投影分开，
 * 消息上的「轨迹」只是带 run_id 跳到这里。
 *
 * tool_calls 的 success 是三态：NULL = 调用发起了、结果从未回写（崩溃/中止），
 * 不能折成成功或失败，按「未回写」渲染。
 */
import { computed, onMounted, ref } from 'vue'
import { agentAdminApi, toFriendlyMessage, type AgentRunTrace } from '@/api/client'
import { formatBackendTs } from '@/utils/format'
import { runStatusChip, runStatusLabel } from '@/utils/agent-run-status'
import { headline } from '@/utils/process-status'
import { buildTraceModel } from '@/utils/run-trace'
import RollbackPanel from '@/components/agent/RollbackPanel.vue'
import type { TraceEventView, TraceInputEntry, TraceStepView } from '@/utils/run-trace'

const props = defineProps<{ runId: string }>()

const loading = ref(false)
const error = ref('')
const trace = ref<AgentRunTrace | null>(null)

onMounted(() => { void fetchTrace() })

async function fetchTrace() {
  loading.value = true
  error.value = ''
  try {
    trace.value = await agentAdminApi.getRunTrace(props.runId)
  } catch (e) {
    error.value = toFriendlyMessage(e, '轨迹加载失败')
  } finally {
    loading.value = false
  }
}

/** 状态链：压缩连续重复，只保留迁移节点（init→thinking→acting→…→completed） */
const stateChain = computed(() => {
  const out: string[] = []
  for (const s of trace.value?.state_history || []) {
    const to = s.to || s.from
    if (to && to !== out[out.length - 1]) out.push(to)
  }
  return out.slice(0, 24)
})

function parseArgs(raw: Record<string, unknown> | string): Record<string, unknown> {
  if (raw && typeof raw === 'object') return raw
  if (typeof raw !== 'string') return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : {}
  } catch {
    return {}
  }
}

/** DB 行 → 过程视图的输入：NULL result 映射成 undefined（未回填），
 *  与 run-trace 里「undefined = 在途」的口径对齐。 */
const processEntries = computed<TraceInputEntry[]>(() =>
  (trace.value?.tool_calls || []).map(tc => ({
    id: tc.id,
    tool: tc.tool_name,
    args: parseArgs(tc.arguments),
    result: tc.result ?? undefined,
    success: tc.success ?? undefined,
    timestamp: Date.parse(tc.created_at) || 0,
    iteration: tc.iteration,
    durationMs: tc.duration_ms ?? undefined,
  })),
)
const process = computed(() => buildTraceModel(processEntries.value))

function stepOf(entry: TraceEventView): TraceStepView {
  return process.value.steps[entry.stepIndex]
}
function stepNo(step: TraceStepView): string {
  return step.iteration === null ? '未编号' : `第 ${step.iteration + 1} 步`
}
function stepDuration(step: TraceStepView): string {
  if (step.durationMs !== undefined) return `${(step.durationMs / 1000).toFixed(1)}s`
  return ''
}
/** 只有部分调用量到耗时：求和值偏小，必须标出来，不能当整批耗时读 */
function stepPartial(step: TraceStepView): boolean {
  return step.durationMs !== undefined && step.measuredCount < step.eventIndexes.length
}

function toolStatus(entry: TraceEventView): string {
  if (entry.success === undefined) return '未回写'
  return entry.success ? '成功' : '失败'
}
function toolTone(entry: TraceEventView): 'ok' | 'bad' | 'muted' {
  if (entry.success === undefined) return 'muted'
  return entry.success ? 'ok' : 'bad'
}
function itemDuration(entry: TraceEventView): string {
  return entry.durationMs !== undefined ? `${(entry.durationMs / 1000).toFixed(1)}s` : ''
}
function outputOf(entry: TraceEventView): string {
  return entry.result || ''
}
function outputLines(entry: TraceEventView): string[] {
  const text = outputOf(entry)
  return text ? text.split('\n') : []
}
function outputPreview(entry: TraceEventView): string {
  return outputLines(entry).slice(0, 3).join('\n')
}

/** 输出展开状态按工具调用 id 记；收起/展开不随 rerender 丢 */
const openOutputs = ref<Set<string>>(new Set())
function toggleOutput(id: string) {
  const next = new Set(openOutputs.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  openOutputs.value = next
}
</script>

<style scoped>
.full-trace {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-8);
  width: 100%;
  min-width: 0;
}
.ft-hint { margin: 0; font-size: 0.8846rem; color: var(--text-secondary); }
.ft-hint.err { color: var(--status-error-text); }
.ft-hint.muted { color: var(--text-tertiary); }
.ft-retry {
  background: none; border: none; color: var(--accent-text);
  cursor: pointer; font-size: 0.8846rem; font-family: inherit; padding: 0 0 0 var(--spacing-4); text-decoration: underline;
}
.ft-meta { display: flex; flex-wrap: wrap; gap: 5px; }
.ft-chip {
  font-size: 0.8077rem; padding: 1px 7px; border-radius: var(--radius-lg);
  border: 1px solid var(--border-default); color: var(--text-secondary);
  font-family: var(--font-mono);
}
.ft-chip.ok { color: var(--status-success-text); border-color: color-mix(in srgb, var(--status-success-text) 35%, transparent); }
.ft-chip.err { color: var(--status-error-text); border-color: color-mix(in srgb, var(--status-error-text) 35%, transparent); }
.ft-chip.muted { color: var(--text-tertiary); }
.ft-states { display: flex; align-items: center; gap: var(--spacing-6); font-size: 0.8462rem; flex-wrap: wrap; }
.ft-states-label { color: var(--text-tertiary); }
.ft-state-chain { display: inline-flex; align-items: center; gap: 3px; flex-wrap: wrap; }
.ft-state {
  color: var(--text-secondary); font-family: var(--font-mono);
  background: var(--bg-input); border-radius: var(--radius-sm); padding: 0 5px;
}
.ft-arrow { color: var(--text-tertiary); }

/* ── 执行过程 ── */
.ft-tools { min-width: 0; }
.ft-step {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  margin: var(--spacing-10) 0 3px;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
}
.ft-step:first-child { margin-top: 0 }
.ft-step-no { color: var(--text-secondary); font-weight: 600; white-space: nowrap }
.ft-step-dur { font-family: var(--font-mono); white-space: nowrap }
.ft-step-note { font-size: 0.8077rem; white-space: nowrap }
.ft-step-line {
  flex: 1;
  height: 1px;
  background: var(--border-subtle);
}
.ft-tool {
  padding: 3px 0 3px var(--spacing-10);
  min-width: 0;
}
.ft-tool-head {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-8);
  min-width: 0;
}
.ft-tool-state {
  flex: none;
  width: 36px;
  font-size: 0.8462rem;
  font-weight: 600;
}
.ft-tool-state.ok { color: var(--status-success-text) }
.ft-tool-state.bad { color: var(--danger-text) }
.ft-tool-state.muted { color: var(--text-tertiary) }
.ft-tool-name {
  flex: none;
  font-size: 0.8846rem;
  color: var(--text-secondary);
}
.ft-cmd {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.8846rem;
  color: var(--text-secondary);
}
.ft-item-dur {
  flex: none;
  font-size: 0.8077rem;
  color: var(--text-tertiary);
}
.ft-out {
  margin: var(--spacing-4) 0 var(--spacing-2);
  padding: var(--spacing-6) 9px;
  max-height: 180px;
  overflow: auto;
  background: var(--bg-input);
  border-left: 2px solid var(--border-default);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 0.8846rem;
  line-height: 1.55;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
.ft-out-toggle {
  padding: var(--spacing-2) 0;
  border: 0;
  background: none;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  cursor: pointer;
}
.ft-out-toggle:hover { color: var(--text-secondary) }

.ft-time { margin: 0; font-size: 0.8077rem; }
.mono { font-family: var(--font-mono) }
</style>
