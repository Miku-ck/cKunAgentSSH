<script setup lang="ts">
/**
 * ToolOperationList — Qoder 风格的极简工具调用日志
 *
 * 设计原则：
 * - 无卡片、无边框、无圆点时间线
 * - 状态用中文动作词表达（已查看 / 已修改 / 已检索…）
 * - 纯文本列表，无衬线字体
 * - 折叠头显示聚合统计（16次阅读 · 1次检索）
 */
import { ref, computed } from 'vue'

interface ToolCallEntry {
  id: string
  tool: string
  args: Record<string, unknown>
  result?: string
  success?: boolean
  /** DB 回放才有单步耗时（SSE 内联快照不提供），缺失即不显示 */
  duration_ms?: number | null
}

const props = withDefaults(defineProps<{
  toolCalls: ToolCallEntry[]
  defaultExpanded?: boolean
  /** 嵌入模式：隐藏自身折叠头，交由父组件控制展开 */
  hideHeader?: boolean
}>(), {
  defaultExpanded: false,
  hideHeader: false,
})

const expanded = ref(props.defaultExpanded || props.hideHeader)

function toggle() {
  expanded.value = !expanded.value
}

// ── 动作词映射：Qoder 风格的中文状态前缀 ──
// 工具名必须与后端注册名一致（file_read/file_write/file_edit/file_glob/file_grep…）。
// 旧实现按 read_file/write_file/list_dir/glob/grep/search_code 匹配，一个都对不上：
// 每一次文件操作的徽章都退化成「已调用」，折叠头的动作聚合也全归到同一类。
function verbFor(tool: string): string {
  switch (tool) {
    case 'file_read':
    case 'read_image':
    case 'log_read': return '已查看'
    case 'file_write':
    case 'file_edit': return '已修改'
    case 'file_glob': return '已查找'
    case 'file_grep': return '已检索'
    case 'shell_exec':
    case 'job_run': return '已执行'
    case 'job_list':
    case 'job_output':
    case 'job_kill': return '已管理任务'
    case 'service_manage':
    case 'schedule_op': return '已操作'
    case 'sys_overview':
    case 'process_inspect':
    case 'network_probe': return '已探测'
    case 'web_search': return '已搜索'
    case 'web_fetch': return '已访问'
    case 'todo_write': return '已记录'
    case 'load_skill': return '已加载'
    case 'spawn_subagent': return '已委派'
    default: return '已调用'
  }
}

// 失败条目整行改说"失败"（红字），聚合统计仍按动作计（见 actionStats）
function actionVerb(tool: string, success?: boolean): string {
  return success === false ? '失败' : verbFor(tool)
}

// ── 目标简短描述 ──
function targetLabel(tc: ToolCallEntry): string {
  const getStr = (v: unknown): string => typeof v === 'string' ? v : String(v ?? '')
  switch (tc.tool) {
    case 'file_read':
    case 'file_write':
    case 'file_edit':
    case 'read_image':
    case 'log_read':
      return shortPath(getStr(tc.args.path))
    case 'file_glob':
    case 'file_grep':
      // pattern 才是用户关心的检索目标，path 只是范围
      return getStr(tc.args.pattern) || shortPath(getStr(tc.args.path))
    case 'shell_exec':
      return truncate(getStr(tc.args.command || tc.args.shell), 60)
    case 'job_run':
      return truncate(getStr(tc.args.cmd), 60)
    case 'service_manage':
    case 'schedule_op':
      return `${getStr(tc.args.action)} ${getStr(tc.args.name)}`.trim()
    case 'network_probe':
      return getStr(tc.args.host) ? `${getStr(tc.args.host)}:${getStr(tc.args.port)}` : ''
    case 'process_inspect':
    case 'web_search':
      return truncate(getStr(tc.args.query), 60)
    case 'web_fetch':
      return truncate(getStr(tc.args.url), 60)
    case 'job_output':
    case 'job_kill':
      return getStr(tc.args.job_id)
    case 'load_skill':
      return getStr(tc.args.skill_slug)
    case 'spawn_subagent':
      return truncate(getStr(tc.args.task), 60)
    default: {
      const entries = Object.entries(tc.args).slice(0, 1)
      return entries.map(([k, v]) => `${k}: ${truncate(getStr(v), 30)}`).join(', ')
    }
  }
}

// 只保留最后两级路径，降低视觉噪音
function shortPath(p: string): string {
  if (!p) return ''
  const parts = p.replace(/\\/g, '/').split('/').filter(Boolean)
  return parts.slice(-2).join('/')
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '…' : s
}

// 单步耗时（毫秒）→ 秒文本；null/undefined/非数字返回 ''，交由模板 v-if 隐藏
function fmtDuration(ms?: number | null): string {
  return typeof ms === 'number' && Number.isFinite(ms) ? `${(ms / 1000).toFixed(1)}s` : ''
}

// ── 按动作类型聚合统计（用于折叠头） ──
const actionStats = computed(() => {
  const stats = new Map<string, number>()
  for (const tc of props.toolCalls) {
    const verb = verbFor(tc.tool)
    stats.set(verb, (stats.get(verb) || 0) + 1)
  }
  return Array.from(stats.entries())
})

const totalRunning = computed(() => props.toolCalls.filter(c => c.result === undefined && c.success === undefined).length)
const totalFailed = computed(() => props.toolCalls.filter(c => c.success === false).length)
const totalCount = computed(() => props.toolCalls.length)

const headerTitle = computed((): string => {
  const n = totalCount.value
  if (totalRunning.value > 0) return `进行中 · ${n} 个操作`
  if (totalFailed.value > 0) return `已操作 ${n} 项 · ${totalFailed.value} 个失败`
  return `已操作 ${n} 项`
})

// 聚合统计文本，如 "16次阅读 · 1次检索"
// 名词直接由动作词去掉「已」派生（只「查看→阅读」一处特例），
// 不再维护一份需要与 verbFor 手工同步的映射表。
const statsText = computed((): string =>
  actionStats.value
    .map(([verb, count]) => `${count}次${verb === '已查看' ? '阅读' : verb.replace('已', '')}`)
    .join(' · '),
)
</script>

<template>
  <div class="tool-log" :class="{ embedded: hideHeader }">
    <div v-if="!hideHeader" class="log-header" @click="toggle">
      <span class="log-title">{{ headerTitle }}</span>
      <span class="log-right">
        <span v-if="statsText && !totalRunning" class="log-stats">{{ statsText }}</span>
        <svg
          class="log-chevron"
          :class="{ expanded }"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          width="14"
          height="14"
        >
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </span>
    </div>

    <div v-show="expanded" class="log-body">
      <div
        v-for="tc in toolCalls"
        :key="tc.id"
        class="log-item"
        :class="{ failed: tc.success === false, running: tc.result === undefined && tc.success === undefined }"
      >
        <span class="log-verb">{{ actionVerb(tc.tool, tc.success) }}</span>
        <span class="log-target">{{ targetLabel(tc) }}</span>
        <span v-if="fmtDuration(tc.duration_ms)" class="log-duration">{{ fmtDuration(tc.duration_ms) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-log {
  font-family: var(--font-sans);
}

/* 折叠头：极简无背景 */
.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
  line-height: 1.6;
}

.log-header:hover {
  color: var(--text-primary);
}

.log-right {
  display: flex;
  align-items: center;
  gap: 6px;
}

.log-chevron {
  color: var(--text-secondary);
  transition: transform 0.2s ease;
  flex-shrink: 0;
  opacity: 0.5;
}

.log-chevron.expanded {
  transform: rotate(90deg);
}

.log-title {
  font-weight: 400;
}

/* 聚合统计：更暗的灰，不抢主标题 */
.log-stats {
  color: var(--text-tertiary);
  font-size: 12px;
}

/* 日志条目：纯文本，无缩进 */
.log-body {
  padding: 2px 0 4px 0;
  display: flex;
  flex-direction: column;
}

.log-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  line-height: 1.9;
  color: var(--log-text);
}

/* 动作词：暗灰，作为前缀标签 */
.log-verb {
  color: var(--log-muted);
  flex-shrink: 0;
}

/* 目标：主文本色 */
.log-target {
  color: var(--log-text);
  word-break: break-word;
  min-width: 0;
}

/* 单步耗时：右对齐弱色，仅 DB 回放有值 */
.log-duration {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 11px;
  color: var(--log-muted);
  font-family: ui-monospace, Consolas, monospace;
}

/* 失败态：仅用文字色区分，不用红圆点 */
.log-item.failed .log-verb {
  color: var(--log-fail);
}

.log-item.failed .log-target {
  color: var(--log-fail-text);
}

/* 运行中态：斜体淡蓝 */
.log-item.running .log-verb {
  color: var(--log-running);
}

.log-item.running .log-target {
  color: var(--log-muted);
}
</style>
