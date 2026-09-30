<script setup lang="ts">
/**
 * 「运行记录」—— 一条指令跑完之后从哪里找回它（spec 2026-09-30-retire-ai-side-panel R2）
 *
 * 存在的理由：侧栏「历史任务」原先是**会话列表**，会话被删，那轮 run 的过程就无处可查；
 * 而 run 的权威记录一直在 `agent_runs` 表里，只是后端只有按号单查、没有列表端点。
 * 这一页接的就是那张表：按时间倒序，可按状态与 trace 号筛，点开一行读 DB 权威轨迹
 * （复用 AgentFullTrace，不重画一份事件流）。
 *
 * 刻意没有的两列：目标主机、审批方式。`agent_runs` 没存这两样（列清单见
 * client.ts 的 AgentRunRow），凭空画就等于拿"当前设置里的选择"去标历史 ——
 * 运行中改设置会改写过去的外观，那是 run 归因那一轮已经踩过并修掉的串台。
 */
import { computed, onMounted, ref } from 'vue'
import {
  agentAdminApi,
  toFriendlyMessage,
  type AgentRunRow,
} from '@/api/client'
import AgentFullTrace from '@/components/agent/AgentFullTrace.vue'
import { formatBackendTs } from '@/utils/format'

const PAGE = 50

const rows = ref<AgentRunRow[]>([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const error = ref('')
const statusFilter = ref('')
const traceFilter = ref('')
/** 展开详情的行 id；同一时刻只开一条，读起来像表而不是手风琴 */
const expanded = ref('')

/** 状态取值来自后端 `database._TERMINAL_RUN_STATUSES` + 两个在途值，
 *  少列一个就有一类 run 筛不出来；对不上的值这里显示原文，不猜翻译。 */
const STATUS_LABELS: Record<string, string> = {
  init: '初始化',
  running: '运行中',
  completed: '完成',
  failed: '失败',
  human_help: '请求人工介入',
  max_reached: '达到迭代上限',
}
const statusLabel = (s: string) => STATUS_LABELS[s] || s

const hasMore = computed(() => offset.value + rows.value.length < total.value)

async function load() {
  const seq = ++loadSeq
  loading.value = true
  error.value = ''
  try {
    const res = await agentAdminApi.listRuns({
      status: statusFilter.value || undefined,
      runId: traceFilter.value.trim() || undefined,
      limit: PAGE,
      offset: offset.value,
    })
    if (seq !== loadSeq) return
    rows.value = res.items
    total.value = res.total
  } catch (e) {
    if (seq !== loadSeq) return
    error.value = toFriendlyMessage(e, '读取运行记录失败')
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/** 请求序号：连着点筛选/翻页会并发多个请求，慢的那个后到会覆盖新结果（审计页同法） */
let loadSeq = 0

function reloadFirst() {
  offset.value = 0
  expanded.value = ''
  void load()
}

function page(delta: number) {
  const next = offset.value + delta * PAGE
  if (loading.value || next < 0 || (delta > 0 && !hasMore.value)) return
  offset.value = next
  expanded.value = ''
  void load()
}

function toggle(row: AgentRunRow) {
  expanded.value = expanded.value === row.id ? '' : row.id
}

function copyTrace(id: string) {
  navigator.clipboard.writeText(id).then(
    () => { /* 行里就印着整串号，看得见就不用再弹一句 */ },
    () => { error.value = '复制失败，请手动选中 trace 号' },
  )
}

function fmtDuration(ms: number): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
}

onMounted(load)
</script>

<template>
  <div class="rhv">
    <header class="rhv-head">
      <span class="t">运行记录</span>
      <span class="sub">{{ total }} 条 · 按发起时间倒序</span>
      <span class="sp"></span>
      <select v-model="statusFilter" @change="reloadFirst">
        <option value="">全部状态</option>
        <option v-for="(label, key) in STATUS_LABELS" :key="key" :value="key">{{ label }}</option>
      </select>
      <input
        v-model="traceFilter"
        class="trace"
        placeholder="trace 号（整串）"
        title="按 trace 号精确找回一条 run。号是 12 位 hex，粘半个筛不出东西"
        @keyup.enter="reloadFirst"
      />
      <button type="button" :disabled="loading" @click="reloadFirst">刷新</button>
    </header>

    <p v-if="error" class="rhv-error">{{ error }}</p>
    <p v-else-if="loading && !rows.length" class="rhv-hint">读取中…</p>
    <p v-else-if="!rows.length" class="rhv-hint">
      {{ traceFilter || statusFilter ? '没有符合条件的运行' : '还没有跑过 Agent 指令' }}
    </p>

    <table v-else class="rhv-table">
      <thead>
        <tr>
          <th>发起时间</th>
          <th>trace</th>
          <th>指令</th>
          <th>状态</th>
          <th title="实际迭代 / 本次发出的上限（上限是自己发的值，不是后端算出来的）">步数</th>
          <th>耗时</th>
          <th>tokens</th>
          <th>成本</th>
          <th>调用</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="r in rows" :key="r.id">
          <tr class="row" :class="{ open: expanded === r.id }" @click="toggle(r)">
            <td class="mono">{{ formatBackendTs(r.started_at) }}</td>
            <td>
              <button
                type="button"
                class="mono trace-cell"
                title="点击复制整串 trace 号"
                @click.stop="copyTrace(r.id)"
              >{{ r.id }}</button>
            </td>
            <td class="task" :title="r.task || ''">{{ r.task || '（没有指令文本）' }}</td>
            <td><span class="st" :class="`st-${r.status}`">{{ statusLabel(r.status) }}</span></td>
            <td class="num">{{ r.iteration }}/{{ r.max_iterations }}</td>
            <td class="num">{{ fmtDuration(r.duration_ms) }}</td>
            <td class="num">{{ r.total_tokens || '—' }}</td>
            <td class="num">{{ r.cost_usd ? `$${r.cost_usd.toFixed(4)}` : '—' }}</td>
            <td class="num">{{ r.tool_call_count }}</td>
          </tr>
          <tr v-if="expanded === r.id" class="detail">
            <td colspan="9">
              <!-- DB 权威回放：状态流转链。与消息气泡里那枚 ▸轨迹是同一个组件、
                   同一个端点；inline 让它待在单元格流内（浮层定位会飘出<tr>），
                   并只补这一行没印过的字段。 -->
              <AgentFullTrace inline :run-id="r.id" />
            </td>
          </tr>
        </template>
      </tbody>
    </table>

    <footer v-if="rows.length" class="rhv-foot">
      <button type="button" :disabled="loading || offset === 0" @click="page(-1)">上一页</button>
      <span class="mono">第 {{ offset + 1 }}–{{ offset + rows.length }} 条 / 共 {{ total }} 条</span>
      <button type="button" :disabled="loading || !hasMore" @click="page(1)">下一页</button>
    </footer>
  </div>
</template>

<style scoped>
/* 与运维审计同一套外壳与密度：只用各主题都已有的令牌，手记主题下不需要额外适配 */
.rhv { display: flex; flex-direction: column; height: 100%; min-height: 0; padding: 12px 16px }
.rhv-head {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--divider-primary);
}
.rhv-head .t { font-size: 13px; font-weight: 600; color: var(--text-primary) }
.rhv-head .sub { font-size: 11px; color: var(--text-tertiary) }
.rhv-head .sp { flex: 1 }
.rhv-head select,
.rhv-head input,
.rhv-head button,
.rhv-foot button {
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-primary);
  font-size: 12px;
}
.rhv-head input.trace { width: 150px; font-family: var(--font-mono) }
.rhv-head button:disabled,
.rhv-foot button:disabled { opacity: .5; cursor: default }
.rhv-head button:hover:not(:disabled),
.rhv-foot button:hover:not(:disabled) { background: var(--bg-hover) }
.rhv-error { font-size: 12px; color: var(--danger-text); padding: 10px 0 }
.rhv-hint { font-size: 12px; color: var(--text-tertiary); padding: 14px 0 }
.rhv-table {
  flex: 1;
  min-height: 0;
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.rhv-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  text-align: left;
  padding: 7px 9px;
  font-size: 11px;
  font-weight: 500;
  color: var(--text-tertiary);
  background: var(--bg-base);
  border-bottom: 1px solid var(--border-default);
}
.rhv-table td {
  padding: 7px 9px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  vertical-align: top;
}
.rhv-table .row { cursor: pointer }
.rhv-table .row:hover td { background: var(--bg-hover) }
.rhv-table .row.open td { background: var(--bg-elevated) }
.mono { font-family: var(--font-mono) }
.trace-cell {
  padding: 0;
  border: 0;
  background: none;
  font: inherit;
  color: var(--text-tertiary);
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 2px;
}
.trace-cell:hover { color: var(--text-primary) }
.task {
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}
.num { text-align: right; font-family: var(--font-mono); white-space: nowrap }
.st {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
}
.st-completed { color: var(--status-success-text); border-color: var(--status-success-border) }
.st-failed,
.st-max_reached { color: var(--danger-text); border-color: var(--danger-border) }
.st-running,
.st-human_help { color: var(--status-warn-text); background: var(--status-warn-bg) }
.detail td { background: var(--bg-elevated); padding: 4px 9px 10px }
.rhv-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding-top: 9px;
  font-size: 11px;
  color: var(--text-tertiary);
}
</style>
