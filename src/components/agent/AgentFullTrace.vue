<template>
  <div class="full-trace" :class="{ inline }">
    <button v-if="!inline" class="ft-toggle" title="DB 权威记录：每步耗时 / 成本 / 状态流转" @click="onToggle">
      <span class="ft-caret" :class="{ open: expanded }">▸</span>
      轨迹
    </button>

    <div v-if="expanded" class="ft-body">
      <p v-if="loading" class="ft-hint">加载轨迹…</p>
      <p v-else-if="error" class="ft-hint err">{{ error }}
        <button class="ft-retry" @click="fetchTrace">重试</button>
      </p>

      <template v-else-if="trace">
        <!-- 元信息条：整表都是「这一条 run 跑成了什么样」的汇总 -->
        <div v-if="!inline" class="ft-meta">
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

        <!-- 状态流转链 -->
        <div v-if="trace.state_history.length" class="ft-states">
          <span class="ft-states-label">状态</span>
          <span class="ft-state-chain">
            <template v-for="(s, i) in stateChain" :key="i">
              <span v-if="i > 0" class="ft-arrow">→</span>
              <span class="ft-state">{{ s }}</span>
            </template>
          </span>
        </div>
        <!-- inline 时状态链是这一行的全部内容，缺了它展开就是片空白，得说一句 -->
        <p v-else-if="inline" class="ft-hint muted">这条 run 没有状态流转记录</p>

        <!-- 工具调用清单不在前端显示 -->

        <p v-if="!inline" class="ft-hint muted">工具调用明细已隐藏</p>
        <!-- inline 时行上已经印着 trace 号、发起时间和各档汇总，这里只补行里没有的结束时刻。
             两端都必须过 formatBackendTs：后端存的是 UTC naive 串（database._now），
             原样印 = 比本地钟面早 8 小时，而同一张表的行上印的是本地时间（AgentRunsTable），
             同一条 run 上下两个时刻互相矛盾 —— 这是本仓记过的「时区接缝」家族。 -->
        <p v-if="!inline" class="ft-time muted">run {{ runId.slice(0, 8) }}… · 始于 {{ formatBackendTs(trace.run.started_at) || '-' }}</p>
        <p v-else-if="trace.run.completed_at" class="ft-time muted">结束 {{ formatBackendTs(trace.run.completed_at) }}</p>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * AgentFullTrace — 会话内回放：从 /agent/runs/{id}/trace 拉 DB 权威轨迹
 *
 * 与消息内联快照（AgentTraceSnapshot，SSE 期间累积）互补：
 * 快照是运行视图，这里是落库视图 —— 每步 duration_ms、成本与状态历史
 * 以 DB 为准（回放审计口径）。清单行只展示动作与目标，不展开结果正文。
 *
 * inline=true 供「运行记录」表用：展开由宿主行控制，本体在文档流里（宿主的浮层
 * 定位是绝对定位，塞进 <tr> 会飘出单元格），并且行上已印过的数字不再重印一遍。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { agentAdminApi, toFriendlyMessage, type AgentRunTrace } from '@/api/client'
import { formatBackendTs } from '@/utils/format'
import { runStatusChip, runStatusLabel } from '@/utils/agent-run-status'
const props = defineProps<{ runId: string; inline?: boolean }>()
const emit = defineEmits<{ 'expand-change': [boolean] }>()

const expanded = ref(!!props.inline)
const loading = ref(false)
const error = ref('')
const trace = ref<AgentRunTrace | null>(null)

watch(expanded, (v) => emit('expand-change', v))

onMounted(() => {
  if (props.inline) void fetchTrace()
})

async function onToggle() {
  expanded.value = !expanded.value
  if (expanded.value && !trace.value && !loading.value) void fetchTrace()
}

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

</script>

<style scoped>
.full-trace { display: inline-flex; }
.ft-toggle {
  display: inline-flex; align-items: center; gap: 5px;
  background: none; border: none; padding: 2px 0; cursor: pointer;
  color: var(--text-secondary); font-size: 11.5px; font-family: inherit;
}
.ft-toggle:hover { color: var(--accent-primary); }
.ft-caret { transition: transform .15s; font-size: 10px; }
.ft-caret.open { transform: rotate(90deg); }
/* 下拉浮层：锚定宿主消息块（.message-block 提供 relative），
   左右钉满对话区宽度 —— 与 AI 面板整体同幅，拖拽调宽时实时自适应 */
.ft-body {
  position: absolute;
  top: calc(100% + 6px);
  left: 2px;
  right: 2px;
  z-index: 40;
  max-height: 60vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 8px;
}
.ft-hint { margin: 0; font-size: 11.5px; color: var(--text-secondary); }
.ft-hint.err { color: var(--status-error-text); }
.ft-hint.muted { color: var(--text-tertiary); }
.ft-retry {
  background: none; border: none; color: var(--accent-primary);
  cursor: pointer; font-size: 11.5px; font-family: inherit; padding: 0 0 0 4px; text-decoration: underline;
}
.ft-meta { display: flex; flex-wrap: wrap; gap: 5px; }
.ft-chip {
  font-size: 10.5px; padding: 1px 7px; border-radius: 8px;
  border: 1px solid var(--border-default); color: var(--text-secondary);
  font-family: ui-monospace, Consolas, monospace;
}
.ft-chip.ok { color: var(--status-success-text); border-color: color-mix(in srgb, var(--status-success-text) 35%, transparent); }
.ft-chip.err { color: var(--status-error-text); border-color: color-mix(in srgb, var(--status-error-text) 35%, transparent); }
.ft-chip.muted { color: var(--text-tertiary); }
.ft-states { display: flex; align-items: center; gap: 6px; font-size: 11px; flex-wrap: wrap; }
.ft-states-label { color: var(--text-tertiary); }
.ft-state-chain { display: inline-flex; align-items: center; gap: 3px; flex-wrap: wrap; }
.ft-state {
  color: var(--text-secondary); font-family: ui-monospace, Consolas, monospace;
  background: var(--bg-input); border-radius: 4px; padding: 0 5px;
}
.ft-arrow { color: var(--text-tertiary); }
.ft-time { margin: 0; font-size: 10.5px; }
.muted { color: var(--text-tertiary); }
/* inline（运行记录表）：展开由宿主那一行控制，本体落回文档流。
   外壳一并拆掉 —— 宿主单元格已经给了底色和留白，再描一圈就是双层卡片。 */
.full-trace.inline { display: block; width: 100%; }
.inline .ft-body {
  position: static;
  top: auto;
  left: auto;
  right: auto;
  z-index: auto;
  max-height: none;
  overflow: visible;
  padding: 0;
  background: none;
  border: none;
  border-radius: 0;
  gap: 6px;
}
</style>
