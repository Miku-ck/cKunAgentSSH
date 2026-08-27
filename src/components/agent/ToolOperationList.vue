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
  timestamp: number
  iteration?: number
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
function actionVerb(tool: string, success?: boolean): string {
  if (success === false) return '失败'
  switch (tool) {
    case 'read_file': return '已查看'
    case 'write_file': return '已修改'
    case 'list_dir': return '已浏览'
    case 'glob': return '已查找'
    case 'grep':
    case 'search_code':
    case 'search_content': return '已检索'
    case 'shell_exec': return '已执行'
    case 'web_search': return '已搜索'
    case 'web_fetch': return '已访问'
    case 'delete_file':
    case 'delete': return '已删除'
    default: return '已调用'
  }
}

// ── 目标简短描述 ──
function targetLabel(tc: ToolCallEntry): string {
  const getStr = (v: unknown): string => typeof v === 'string' ? v : String(v ?? '')
  switch (tc.tool) {
    case 'read_file':
    case 'write_file':
      return shortPath(getStr(tc.args.file_path || tc.args.path))
    case 'list_dir':
      return shortPath(getStr(tc.args.path))
    case 'glob':
      return getStr(tc.args.pattern)
    case 'grep':
    case 'search_code':
    case 'search_content':
      return getStr(tc.args.pattern || tc.args.query)
    case 'shell_exec':
      return truncate(getStr(tc.args.command || tc.args.shell), 60)
    case 'web_search':
      return truncate(getStr(tc.args.query), 60)
    case 'web_fetch':
      return truncate(getStr(tc.args.url), 60)
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

// ── 按动作类型聚合统计（用于折叠头） ──
const actionStats = computed(() => {
  const stats = new Map<string, number>()
  for (const tc of props.toolCalls) {
    const verb = actionVerb(tc.tool, tc.success)
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
const statsText = computed((): string => {
  const verbShort: Record<string, string> = {
    '已查看': '阅读',
    '已修改': '修改',
    '已浏览': '浏览',
    '已查找': '查找',
    '已检索': '检索',
    '已执行': '执行',
    '已搜索': '搜索',
    '已访问': '访问',
    '已删除': '删除',
    '已调用': '调用',
    '失败': '失败',
  }
  return actionStats.value
    .filter(([verb]) => verb !== '失败')
    .map(([verb, count]) => `${count}次${verbShort[verb] || verb.replace('已', '')}`)
    .join(' · ')
})
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
  color: #888;
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
  color: #c9d1d9;
}

/* 动作词：暗灰，作为前缀标签 */
.log-verb {
  color: #8b949e;
  flex-shrink: 0;
}

/* 目标：主文本色 */
.log-target {
  color: #c9d1d9;
  word-break: break-word;
  min-width: 0;
}

/* 失败态：仅用文字色区分，不用红圆点 */
.log-item.failed .log-verb {
  color: #f85149;
}

.log-item.failed .log-target {
  color: #ffa198;
}

/* 运行中态：斜体淡蓝 */
.log-item.running .log-verb {
  color: #58a6ff;
}

.log-item.running .log-target {
  color: #8b949e;
}
</style>
