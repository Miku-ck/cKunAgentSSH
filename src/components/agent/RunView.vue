<script setup lang="ts">
/**
 * 运行视图 —— 一条指令的过程与轨迹（spec 2026-09-29-home-command-panel 故事 15–22）
 *
 * 挂在 SshView 的首页里（指令卡与待放行队列下面），**不走 SideChatPanel 的
 * agent-panel slot**：那个 slot 处在 `displayMessages.length === 0` 的 v-else
 * 分支里，首条消息进 store 之前整块不挂载（AgentRunPanel 就吃过这个亏）。
 * 关掉右侧 AI 面板也要能看过程，才是这块视图存在的理由。
 * 2026-09-30：它先前是标签条上的一枚「执行」标签 + 整屏第三态，进这里要
 * openRunView() 把首页换掉；现在它跟着首页一起滚，下达后两条路都在同一屏上。
 *
 * 数据只读 agent store 一份，不复制进消息气泡；轨迹分组走 utils/run-trace，
 * 与后面要加的时间线列共用同一次分组结果。
 *
 * 头部的「模型 / 审批档」读的是 agent store 的归因两格（发起那一刻记下的值），
 * 不是设置面板的当前选择 —— 后者会在运行中被改，把已经跑完的那一轮标成另一个
 * 模型、另一档审批（故事 16 的修订记的就是这个坑）。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { useAgentStore } from '@/stores/agent'
import { useModelsStore } from '@/stores/models'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { approvalTargetLabel } from '@/utils/approval-display'
import { PERMISSION_LABELS } from '@/utils/run-modes'
import { buildTraceModel, mergeStream } from '@/utils/run-trace'
import ApprovalCard from './ApprovalCard.vue'
import ThinkingBlock from '@/components/chat/ThinkingBlock.vue'
import MessageContentRenderer from '@/components/chat/MessageContentRenderer.vue'

const agent = useAgentStore()
const ssh = useSSHStore()
const models = useModelsStore()

const toast = useToastStore()

/** 复制整串 trace。审计页的 run_id 是精确匹配，粘一半筛不出东西，所以给全号。 */
function copyTrace() {
  const id = agent.lastRunId
  if (!id) return
  navigator.clipboard.writeText(id).then(
    () => toast.success(`trace ${id} 已复制，粘到运维审计的 trace 框即可找回这一轮`),
    () => toast.error('复制失败，请手动选中 trace 号'),
  )
}

const trace = computed(() => buildTraceModel(agent.toolCalls))
/** 调用与待放行审批按时间归并成一条流（为什么不能把审批挂在调用上，见 mergeStream 注释） */
const stream = computed(() => mergeStream(trace.value, agent.pendingPermissions))

/** 步号读 iteration（后端循环计数器），不读 steps.length —— 只思考不调工具的迭代
 *  不产生条目，用数组下标当步号会跳号。 */
const stepNo = computed(() => (agent.totalIterations || agent.iteration + 1))

/** 深度思考：与对话气泡同源（agentStore.reasoning），只读不改。RunView 此前只渲染
 *  工具调用一份数据，思考过程丢了；这里补一块可折叠区，交互沿用 ThinkingBlock。 */
const reasoning = computed(() => agent.reasoning || '')
// 流式中默认展开、结束后回落到用户设置 thinkingExpanded —— 与 MessageItem 一致
const thinkingDefaultOpen = computed(() => (agent.isRunning ? true : undefined))

/** 模型输出结果（最终回答正文）：与对话气泡同源（agentStore.content）。RunView 此前
 *  只有工具调用，没把回答带回来；这里补上，复用气泡同一个渲染器（markdown / 纯文本回退一致）。 */
const answer = computed(() => agent.content || '')

const targetLabel = computed(() =>
  approvalTargetLabel({ sshSessionId: agent.lastSshSessionId }, ssh.sessions, ssh.connections),
)

/** 归因那一格只在"查得到名字"时出现：模型列表还没拉到、或那个模型已被删掉时，
 *  宁可空着，也不把内部 id 印成一格用户读不懂的字，更不拿当前选择顶上去。 */
const runModelName = computed(() =>
  models.models.find(m => m.id === agent.lastModelId)?.name || '',
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
        <!-- 整串 12 位 hex 才是审计能认的号（后端 _build_filters 是 run_id = ? 精确匹配，
             8 位前缀筛不出东西），而且这 12 位是刻意从 8 位加长的 —— 8 位在数万 run 量级
             下生日碰撞显著（agent.py:731）。截断显示等于把防碰撞的那几位扔掉，还看着像全号。 -->
        <button
          v-if="agent.lastRunId"
          type="button"
          class="m mono trace"
          title="点击复制完整 trace 号，运维审计页按它筛这一轮的全部记录"
          @click="copyTrace"
        >№{{ agent.lastRunId }}</button>
        <span v-if="runModelName" class="m" title="本轮使用的模型（发起时记下，不随设置变化）">{{ runModelName }}</span>
        <span class="m" title="本轮的审批方式（发起时记下，不随设置变化）">
          {{ PERMISSION_LABELS[agent.lastPermissionMode] }}
        </span>
        <span class="sp"></span>
        <!-- 「回首页」撤掉：过程就长在首页上，没有"回去"这回事了 -->
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

        <!-- 待放行：与首页队列同一个对象、同一份结算（store 的 approve/deny）。
             决策位就在这张卡上 —— 队列已经降为计数条。C1 之前过程是另一屏，
             两屏各自可见所以不重复；并成一屏后必须择一，选卡不选条的理由见
             spec 2026-09-29 C2 的裁决。 -->
        <div v-else class="rv-approval">
          <ApprovalCard :req="item.req" />
        </div>
      </template>

      <p v-if="!stream.length" class="rv-empty">
        {{ agent.isRunning ? 'AI 正在思考，还没开始调用工具' : '这次运行没有产生工具调用' }}
      </p>
      </div>
    </div>

    <!-- 深度思考：工具调用轨迹下方；无 reasoning 时整块不渲染 -->
    <div v-if="reasoning" class="rv-thinking">
      <ThinkingBlock
        :reasoning="reasoning"
        :streaming="agent.isRunning"
        :default-open="thinkingDefaultOpen"
      />
    </div>

    <!-- 模型输出结果（最终回答）：运行中即使正文未至也占位（渲染器会显示思考小点） -->
    <div v-if="answer || agent.isRunning" class="rv-answer">
      <div class="rv-answer-label">输出结果</div>
      <MessageContentRenderer
        role="assistant"
        :content="answer"
        :streaming="agent.isRunning"
        :is-last="true"
      />
    </div>
  </div>
</template>

<style scoped>
/* 只用现有主题令牌 —— 手记主题只改令牌值，不动组件 */
/**
 * 过程面板现在是首页里的一块（先前是整屏的一第三态）：所以它跟着页面流，
 * 自己不再撑满高度、也不再画一层画布底 —— 纸从首页那里透上来就行。
 * 宽度与指令卡/待放行队列同栏（720），三者左边缘对得上。
 */
.rv {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 720px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}
.rv-head {
  flex: none;
  padding: 12px 18px 10px;
  border-bottom: 1px solid var(--border-subtle);
}
/* 深度思考区：在工具调用轨迹下方，与头部/工具卡同左右幅（18px），顶部分隔线接轨迹 */
.rv-thinking {
  flex: none;
  padding: 8px 18px;
  border-top: 1px solid var(--border-subtle);
}
/* 模型输出结果：与轨迹/思考同左右幅，顶部分隔线接思考区 */
.rv-answer {
  flex: none;
  padding: 10px 18px 14px;
  border-top: 1px solid var(--border-subtle);
}
.rv-answer-label {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-bottom: 6px;
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
/* trace 号是一枚可点的读数，不是控件 —— 长得要和旁边那几格一模一样 */
button.trace {
  background: none;
  border: 0;
  padding: 0;
  font: inherit;
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 2px;
}
button.trace:hover { color: var(--text-primary) }
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
