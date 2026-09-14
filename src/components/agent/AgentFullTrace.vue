<template>
  <div class="full-trace">
    <button class="ft-toggle" @click="onToggle">
      <span class="ft-caret" :class="{ open: expanded }">▸</span>
      查看完整轨迹（DB 权威记录：每步耗时 / 成本 / 状态流转）
    </button>

    <div v-if="expanded" class="ft-body">
      <p v-if="loading" class="ft-hint">加载轨迹…</p>
      <p v-else-if="error" class="ft-hint err">{{ error }}
        <button class="ft-retry" @click="fetchTrace">重试</button>
      </p>

      <template v-else-if="trace">
        <!-- 元信息条 -->
        <div class="ft-meta">
          <span class="ft-chip" :class="trace.run.status === 'completed' ? 'ok' : 'err'">
            {{ trace.run.status === 'completed' ? '完成' : '失败' }}
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

        <!-- 权威工具调用清单（含 DB 完整 result 与单步耗时） -->
        <ToolOperationList :tool-calls="dbCalls" :default-expanded="true" hide-header />

        <p v-if="!dbCalls.length" class="ft-hint muted">该 run 无工具调用记录</p>
        <p class="ft-time muted">run {{ runId.slice(0, 8) }}… · 始于 {{ trace.run.started_at || '-' }}</p>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * AgentFullTrace — 会话内回放：从 /agent/runs/{id}/trace 拉 DB 权威轨迹
 *
 * 与消息内联快照（AgentTraceSnapshot，SSE 期间累积）互补：
 * 快照是运行视图，这里是落库视图 —— 每步 duration_ms、完整 result、
 * 成本与状态历史以 DB 为准（回放审计口径）。
 */
import { computed, ref } from 'vue'
import { agentAdminApi, toFriendlyMessage, type AgentRunTrace } from '@/api/client'
import ToolOperationList from './ToolOperationList.vue'

const props = defineProps<{ runId: string }>()

const expanded = ref(false)
const loading = ref(false)
const error = ref('')
const trace = ref<AgentRunTrace | null>(null)

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

/** DB 行 → ToolOperationList 入口结构（arguments 兼容 JSON 字符串/对象） */
const dbCalls = computed(() =>
  (trace.value?.tool_calls || []).map((tc) => {
    let args: Record<string, unknown> = {}
    if (typeof tc.arguments === 'string') {
      try { args = JSON.parse(tc.arguments) } catch { args = { command: tc.arguments } }
    } else if (tc.arguments && typeof tc.arguments === 'object') {
      args = tc.arguments
    }
    return {
      id: tc.id || tc.created_at,
      tool: tc.tool_name,
      args,
      result: tc.result ?? undefined,
      success: tc.success,
      timestamp: Date.parse((tc.created_at || '').replace(' ', 'T') + 'Z') || Date.now(),
      iteration: tc.iteration,
    }
  }),
)
</script>

<style scoped>
.full-trace { margin-top: 4px; }
.ft-toggle {
  display: inline-flex; align-items: center; gap: 5px;
  background: none; border: none; padding: 2px 0; cursor: pointer;
  color: var(--text-secondary, #9a9a9a); font-size: 11.5px; font-family: inherit;
}
.ft-toggle:hover { color: var(--accent-primary, #7aa2d0); }
.ft-caret { transition: transform .15s; font-size: 10px; }
.ft-caret.open { transform: rotate(90deg); }
.ft-body { padding: 6px 0 2px; display: flex; flex-direction: column; gap: 8px; }
.ft-hint { margin: 0; font-size: 11.5px; color: var(--text-secondary); }
.ft-hint.err { color: #d98f86; }
.ft-hint.muted { color: var(--text-tertiary, #6a6a6a); }
.ft-retry {
  background: none; border: none; color: var(--accent-primary, #7aa2d0);
  cursor: pointer; font-size: 11.5px; font-family: inherit; padding: 0 0 0 4px; text-decoration: underline;
}
.ft-meta { display: flex; flex-wrap: wrap; gap: 5px; }
.ft-chip {
  font-size: 10.5px; padding: 1px 7px; border-radius: 8px;
  border: 1px solid var(--border-default, #333); color: var(--text-secondary);
  font-family: ui-monospace, Consolas, monospace;
}
.ft-chip.ok { color: #7fbc92; border-color: rgba(127,188,146,.35); }
.ft-chip.err { color: #cf8a80; border-color: rgba(207,138,128,.35); }
.ft-chip.muted { color: var(--text-tertiary, #6a6a6a); }
.ft-states { display: flex; align-items: center; gap: 6px; font-size: 11px; flex-wrap: wrap; }
.ft-states-label { color: var(--text-tertiary); }
.ft-state-chain { display: inline-flex; align-items: center; gap: 3px; flex-wrap: wrap; }
.ft-state {
  color: var(--text-secondary); font-family: ui-monospace, Consolas, monospace;
  background: var(--bg-input, rgba(255,255,255,.04)); border-radius: 4px; padding: 0 5px;
}
.ft-arrow { color: var(--text-tertiary); }
.ft-time { margin: 0; font-size: 10.5px; }
.muted { color: var(--text-tertiary, #6a6a6a); }
</style>
