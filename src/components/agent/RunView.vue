<script setup lang="ts">
/**
 * 运行视图 —— 一条指令的过程与轨迹（spec 2026-09-29-home-command-panel 故事 15–22）
 *
 * 挂在 SshView 层，与首页/终端平级，**不走 SideChatPanel 的 agent-panel slot**：
 * 那个 slot 处在 `displayMessages.length === 0` 的 v-else 分支里，首条消息进 store
 * 之前整块不挂载（AgentRunPanel 就吃过这个亏）。关掉右侧 AI 面板也要能看过程，
 * 才是这条视图存在的理由。
 *
 * 数据只读 agent store 一份，不复制进消息气泡；轨迹分组走 utils/run-trace，
 * 与后面要加的时间线列共用同一次分组结果。
 *
 * 头部刻意没放「模型 / 审批档」两项：那两个值此刻只存在于设置面板的当前选择里，
 * run 本身没记住自己是用哪档跑的。把"现在的选择"标成"这一轮的档位"是另一种串台
 * （运行中改设置就会看到历史被改写），等 run 归因字段定了再补，见任务清单。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { useAgentStore } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { approvalTargetLabel } from '@/utils/approval-display'
import { buildTraceModel, mergeStream } from '@/utils/run-trace'
import ApprovalCard from './ApprovalCard.vue'

const agent = useAgentStore()
const ssh = useSSHStore()

const emit = defineEmits<{ backHome: [] }>()

const trace = computed(() => buildTraceModel(agent.toolCalls))
/** 调用与待放行审批按时间归并成一条流（为什么不能把审批挂在调用上，见 mergeStream 注释） */
const stream = computed(() => mergeStream(trace.value, agent.pendingPermissions))

/** 步号读 iteration（后端循环计数器），不读 steps.length —— 只思考不调工具的迭代
 *  不产生条目，用数组下标当步号会跳号。 */
const stepNo = computed(() => (agent.totalIterations || agent.iteration + 1))

const targetLabel = computed(() =>
  approvalTargetLabel({ sshSessionId: agent.lastSshSessionId }, ssh.sessions, ssh.connections),
)

const statusText = computed(() => {
  if (agent.isRunning) return '运行中'
  if (agent.aborted) return '已中止'
  if (agent.status === 'failed') return '失败'
  if (agent.status === 'completed') return '已完成'
  return '未开始'
})
/** 三点状态色不当成唯一信道：文案同时说明状态（色觉障碍下仍可读） */
const statusTone = computed(() =>
  agent.isRunning ? 'run' : agent.status === 'failed' || agent.aborted ? 'bad' : 'ok',
)

// ── 整程计时：只有运行中才挂 interval，收尾后读后端的权威时长 ──
const nowTick = ref(0)
let ticker: ReturnType<typeof setInterval> | null = null

function stopTicker() {
  if (ticker === null) return
  clearInterval(ticker)
  ticker = null
}

watch(() => agent.isRunning, (running) => {
  stopTicker()
  if (!running) return
  nowTick.value = Date.now()
  ticker = setInterval(() => { nowTick.value = Date.now() }, 1000)
}, { immediate: true })

onUnmounted(stopTicker)

const elapsedMs = computed(() => {
  if (agent.isRunning) {
    return agent.runStartedAt > 0 && nowTick.value > 0 ? nowTick.value - agent.runStartedAt : 0
  }
  if (agent.costInfo?.duration_ms) return agent.costInfo.duration_ms
  // 中止或异常收尾时后端可能没送 cost 快照。退回最后一次 tick：宁可少报一秒，
  // 也不能显示 00:00 假装这一轮没跑过。
  return agent.runStartedAt > 0 && nowTick.value > agent.runStartedAt
    ? nowTick.value - agent.runStartedAt
    : 0
})

function mmss(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(total / 60)
  return `${String(m).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

/** 工具卡上那行命令：命令执行类直接给 command，其余给参数摘要
 *  （整包 JSON 会让一张卡变成一屏噪声） */
function headline(e: { tool: string; args?: Record<string, unknown> }): string {
  const cmd = typeof e.args?.command === 'string' ? e.args.command : ''
  if (cmd) return cmd
  const keys = Object.keys(e.args || {})
  if (!keys.length) return e.tool
  const preview = keys.slice(0, 3).map(k => {
    const v = String(e.args?.[k])
    // 截断必须留痕：不给省略号，用户会把半截参数当全文
    return `${k}=${v.length > 40 ? `${v.slice(0, 40)}…` : v}`
  }).join(' ')
  return `${e.tool}(${preview})`
}

function stateOf(e: { result?: string; success?: boolean }): string {
  if (e.result === undefined) return 'running'
  return e.success === false ? 'failed' : 'done'
}

// ── 时间线 ↔ 事件流联动：点某批，右栏滚到它第一条并亮一下 ──
const streamRef = ref<HTMLElement | null>(null)
const litStep = ref(-1)
let litTimer: ReturnType<typeof setTimeout> | null = null

function focusStep(stepIndex: number) {
  // data-step 存的就是批次号，querySelector 拿到的第一个匹配即该批最早那条
  // （同批的卡在 DOM 里按到达序相邻，审批插在其间不影响"第一条"是谁）
  const el = streamRef.value?.querySelector<HTMLElement>(`[data-step="${stepIndex}"]`)
  if (!el) return
  el.scrollIntoView({ block: 'nearest' })
  litStep.value = stepIndex
  if (litTimer) clearTimeout(litTimer)
  litTimer = setTimeout(() => { litStep.value = -1 }, 1600)
}

onUnmounted(() => { if (litTimer) clearTimeout(litTimer) })
</script>

<template>
  <div class="rv">
    <header class="rv-head">
      <div class="rv-task" :title="agent.lastTask">{{ agent.lastTask || '（本次运行没有指令文本）' }}</div>
      <div class="rv-meta">
        <span class="rv-status" :class="statusTone">
          <span class="dot" aria-hidden="true"></span>{{ statusText }} {{ mmss(elapsedMs) }}
        </span>
        <span class="m">第 {{ stepNo }} 步</span>
        <span v-if="targetLabel" class="m mono">{{ targetLabel }}</span>
        <span v-if="agent.lastRunId" class="m mono" :title="`trace ${agent.lastRunId}`">
          №{{ agent.lastRunId.slice(0, 8) }}
        </span>
        <span class="sp"></span>
        <button type="button" class="b" @click="emit('backHome')">回首页</button>
        <button
          v-if="agent.isRunning"
          type="button"
          class="b stop"
          @click="agent.abort()"
        >
          中止
        </button>
      </div>
    </header>

    <div class="rv-body">
      <!-- 左栏：按批的时间线。步号读 iteration（后端循环计数器），不是数组下标 -->
      <nav v-if="trace.steps.length" class="rv-tl" aria-label="执行批次时间线">
        <button
          v-for="(s, i) in trace.steps"
          :key="`tl-${i}`"
          type="button"
          class="tl-row"
          :class="[s.state, { active: i === trace.activeStepIndex }]"
          @click="focusStep(i)"
        >
          <span class="tl-dot" aria-hidden="true"></span>
          <span class="tl-no">{{ s.iteration === null ? '未编号' : `第 ${s.iteration + 1} 批` }}</span>
          <span class="tl-meta">
            {{ s.eventIndexes.length }} 项<template v-if="s.durationMs !== undefined"> · {{ (s.durationMs / 1000).toFixed(1) }}s</template>
            <template v-else-if="s.state === 'running'"> · 执行中</template>
          </span>
        </button>
      </nav>

      <div ref="streamRef" class="rv-stream">
      <template v-for="item in stream" :key="item.key">
        <article
          v-if="item.kind === 'tool'"
          class="rv-card"
          :class="[stateOf(item.entry), { lit: litStep === item.stepIndex }]"
          :data-step="item.stepIndex"
        >
          <div class="rv-card-head">
            <span class="tool mono">{{ item.entry.tool }}</span>
            <span v-if="item.entry.iteration !== undefined" class="iter">第 {{ item.entry.iteration + 1 }} 批</span>
            <span v-if="item.entry.durationMs !== undefined" class="dur mono">{{ (item.entry.durationMs / 1000).toFixed(1) }}s</span>
            <span v-if="stateOf(item.entry) === 'failed'" class="tag bad">失败</span>
            <span v-if="stateOf(item.entry) === 'running'" class="tag run">执行中</span>
          </div>
          <code class="rv-cmd mono" :title="headline(item.entry)">{{ headline(item.entry) }}</code>
          <details v-if="item.entry.result" class="rv-out">
            <summary>输出 {{ item.entry.result.length }} 字</summary>
            <pre class="mono">{{ item.entry.result }}</pre>
          </details>
        </article>

        <!-- 待放行：与侧栏托盘、首页队列同一张卡、同一个对象，在这儿批等于在那儿批 -->
        <div v-else class="rv-approval">
          <ApprovalCard :req="item.req" />
        </div>
      </template>

      <p v-if="!stream.length" class="rv-empty">
        {{ agent.isRunning ? 'AI 正在思考，还没开始调用工具' : '这次运行没有产生工具调用' }}
      </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 只用现有主题令牌 —— 手记主题是第四片的事 */
.rv {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
  background: var(--bg-base);
}
.rv-head {
  flex: none;
  padding: 12px 18px 10px;
  border-bottom: 1px solid var(--border-subtle);
}
.rv-task {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.rv-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 7px;
  font-size: 11px;
  color: var(--text-tertiary);
}
.rv-meta .sp { flex: 1 }
.mono { font-family: var(--font-mono) }
.rv-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 600;
}
.rv-status .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.rv-status.run { color: var(--accent-green) }
.rv-status.run .dot { animation: rv-blink 1.1s steps(2, start) infinite }
.rv-status.ok { color: var(--text-secondary) }
.rv-status.bad { color: var(--danger-text) }
@keyframes rv-blink { 50% { opacity: .35 } }
@media (prefers-reduced-motion: reduce) {
  .rv-status.run .dot,
  .tl-row.running .tl-dot { animation: none }
}
.m { color: var(--text-tertiary) }
.b {
  height: 24px;
  padding: 0 11px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}
.b:hover { background: var(--bg-hover); color: var(--text-primary) }
.b.stop { border-color: var(--danger-border); color: var(--danger-text) }
.b.stop:hover { background: var(--danger-bg) }

.rv-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-wrap: wrap;
}

/* ── 左栏时间线 ── */
.rv-tl {
  position: relative;
  flex: 0 0 212px;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 10px;
  border-right: 1px solid var(--border-subtle);
}
/* 纵向连接线：穿过每行状态点的中心 */
.rv-tl::before {
  content: "";
  position: absolute;
  left: 17px;
  top: 20px;
  bottom: 20px;
  width: 1px;
  background: var(--border-subtle);
}
.tl-row {
  position: relative;
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr);
  gap: 1px 8px;
  width: 100%;
  padding: 6px 7px;
  border-radius: var(--radius-sm);
  text-align: left;
  transition: background var(--transition-fast);
}
.tl-row:hover { background: var(--bg-hover) }
.tl-row.active { background: var(--bg-input) }
.tl-dot {
  grid-row: 1 / 3;
  align-self: center;
  justify-self: center;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-tertiary);
}
.tl-row.done .tl-dot { background: var(--status-success-border) }
.tl-row.failed .tl-dot { background: var(--danger-text) }
.tl-row.running .tl-dot { background: var(--accent-primary); animation: rv-blink 1.1s steps(2, start) infinite }
.tl-no { font-size: 11.5px; color: var(--text-primary) }
.tl-meta { font-size: 10.5px; color: var(--text-tertiary) }

.rv-stream {
  flex: 1 1 420px;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.rv-card {
  max-width: 940px;  padding: 9px 12px 10px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-left: 2px solid var(--border-default);
  border-radius: var(--radius-md);
}
.rv-card.done { border-left-color: var(--status-success-border) }
.rv-card.failed { border-left-color: var(--danger-border) }
.rv-card.running { border-left-color: var(--accent-primary) }
/* 从时间线点过来的那一条亮一下（1.6s 后自动退），不滚走就找不到对应关系了 */
.rv-card.lit {
  border-color: var(--accent-primary);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent-primary) 22%, transparent);
}
/* 内联审批卡与工具卡同宽，两种卡片在流里对齐成一条竖线 */
.rv-approval { max-width: 940px }
.rv-card-head {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 6px;
  font-size: 11px;
}
.rv-card-head .tool { color: var(--text-secondary); font-weight: 600 }
.rv-card-head .iter,
.rv-card-head .dur { color: var(--text-tertiary) }
.rv-card-head .tag { margin-left: auto; font-weight: 600 }
.rv-card-head .tag.bad { color: var(--danger-text) }
.rv-card-head .tag.run { color: var(--accent-primary) }
.rv-cmd {
  display: block;
  font-size: 12px;
  color: var(--accent-cyan);
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 5px 8px;
  white-space: pre;
  overflow-x: auto;
}
.rv-out { margin-top: 6px }
.rv-out summary {
  cursor: pointer;
  list-style: none;
  font-size: 11px;
  color: var(--text-tertiary);
}
.rv-out summary::-webkit-details-marker { display: none }
.rv-out summary:hover { color: var(--text-secondary) }
.rv-out pre {
  margin-top: 5px;
  max-height: 240px;
  overflow: auto;
  padding: 8px 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
.rv-empty {
  font-size: 12px;
  color: var(--text-tertiary);
  padding: 18px 2px;
}
</style>
