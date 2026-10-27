<template>
  <div
    class="gw-row"
    @contextmenu.prevent="$emit('menu', $event)"
  >
    <div class="cell-check"></div>

    <div class="cell-status">
      <span class="status-dot" :class="dotClass" :title="stateTitle"></span>
    </div>

    <div class="cell-name" :title="gateway.name">
      <span class="type-badge">TLS</span>
      <span class="name-text">{{ gateway.name }}</span>
    </div>

    <div class="cell-host" :title="`${gateway.user}@${gateway.host}:${gateway.port}`">
      <span class="host-text">{{ gateway.user }}@{{ gateway.host }}:{{ gateway.port }}</span>
    </div>

    <div class="cell-auth">
      <span class="auth-badge tls">{{ gateway.caPem ? 'TLS + CA' : 'TLS' }}</span>
    </div>

    <div class="cell-tags" :class="note.tone" :title="note.title">{{ note.text }}</div>

    <div class="cell-time">
      <span :title="fullTime">{{ relativeTime }}</span>
    </div>

    <div class="cell-created">
      <span :title="fullCreatedTime">{{ createdDateText }}</span>
    </div>

    <div class="cell-actions" @click.stop>
      <button
        class="row-btn"
        :class="actionTone"
        :disabled="actionDisabled"
        :title="actionTitle"
        @click="$emit('toggle')"
      >
        <svg v-if="showSpinner" class="spinner" viewBox="0 0 24 24" fill="none" width="14" height="14"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-opacity="0.25"/><path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
        <svg v-else-if="state === 'online'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M12 3v9"/><path d="M6.6 6.6a8 8 0 1 0 10.8 0"/></svg>
        <svg v-else-if="state === 'reconnecting'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        <span>{{ actionLabel }}</span>
      </button>
      <button class="row-btn more" title="更多操作" @click.stop="$emit('menu', $event)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { formatRelativeTime } from '@/utils/format'

const props = defineProps<{
  gateway: GatewayRecord
  /** 主进程推送的会话状态；无条目 = idle */
  status?: GatewayStatus
  /** 本次操作在途（接入/断开），按钮转圈并禁用 */
  busy: boolean
  /** 「最后连接」相对时间的基准时刻（父级定时 tick） */
  now: number
}>()

defineEmits<{
  toggle: []
  menu: [e: MouseEvent]
}>()

const state = computed<GatewayStatus['state']>(() => props.status?.state ?? 'idle')

const dotClass = computed(() => ({
  on: state.value === 'online',
  err: state.value === 'error',
  warn: state.value === 'connecting' || state.value === 'reconnecting',
  pulse: state.value === 'reconnecting',
  off: state.value === 'idle',
}))

const stateTitle = computed(() => {
  const s = props.status
  switch (state.value) {
    case 'connecting': return '连接中'
    case 'online': return '在线'
    case 'reconnecting': return `重连中 ${s?.attempt || 1}`
    case 'error': return s?.message || '连接失败'
    default: return '未连接'
  }
})

/** 备注列承载网关的真实状态信息：错误原因 / 重连次数 / 在线心跳与通道数 */
const note = computed<{ text: string; title: string; tone: string }>(() => {
  const s = props.status
  if (state.value === 'error') {
    const text = s?.message || '连接失败'
    return { text, title: text, tone: 'err' }
  }
  if (state.value === 'reconnecting') {
    const text = s?.message || `第 ${s?.attempt || 1} 次重连`
    return { text, title: text, tone: 'warn' }
  }
  if (state.value === 'online') {
    // 心跳是网关在 HELLO_OK 里协商下来的；通道数为 0 是合法读数，不能因为 falsy 省掉
    const bits = [`心跳 ${s?.heartbeatSec ?? '-'} s`, `${s?.channels ?? 0} 通道`]
    const title = [`会话 ${s?.sessionId || '-'}`, `角色 ${s?.role || '-'}`, ...bits].join(' · ')
    return { text: bits.join(' · '), title, tone: '' }
  }
  return { text: '—', title: 'TLS 隧道中继', tone: '' }
})

const showSpinner = computed(() => props.busy || state.value === 'connecting')
const actionDisabled = computed(() => props.busy || state.value === 'connecting')

const actionLabel = computed(() => {
  if (state.value === 'connecting') return '连接中'
  if (props.busy) return '处理中'
  if (state.value === 'online') return '断开'
  if (state.value === 'reconnecting') return '停止重连'
  return '接入'
})

const actionTone = computed(() => {
  if (state.value === 'online') return 'danger'
  if (state.value === 'reconnecting') return 'warn'
  return 'connect'
})

const actionTitle = computed(() => `${actionLabel.value} ${props.gateway.name}`)

// 网关没有持久化的「最后连接」：主进程在每次接入成功时打 sinceAt（断开/报错不清），
// 运行期内即最近一次接入时刻，重启进程后未知。
const relativeTime = computed(() => formatRelativeTime(props.status?.sinceAt, props.now, '—'))
const fullTime = computed(() => {
  const t = props.status?.sinceAt
  return t ? new Date(t).toLocaleString() : '无接入记录'
})

const createdDateText = computed(() => formatDate(props.gateway.createdAt))
const fullCreatedTime = computed(() => new Date(props.gateway.createdAt).toLocaleString())

function formatDate(ts: number): string {
  if (!ts) return '-'
  const d = new Date(ts)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
</script>

<style scoped>
.gw-row {
  display: grid;
  grid-template-columns: var(--rm-grid-cols, 22px 12px minmax(110px, 1.1fr) minmax(160px, 1.4fr) 60px minmax(80px, 0.9fr) 84px 84px 112px);
  align-items: center;
  gap: 10px;
  height: 44px;
  padding: 0 12px;
  border-bottom: 1px solid var(--border-subtle);
  cursor: default;
  transition: background var(--transition-fast);
  font-family: var(--font-sans);
  text-align: left;
}

.gw-row:hover,
.gw-row:focus-within {
  background: var(--bg-hover);
}

.gw-row > div {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.cell-check {
  display: flex;
  align-items: center;
}

.cell-status {
  display: flex;
  align-items: center;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}

.status-dot.on {
  background: var(--accent-green);
  box-shadow: 0 0 4px color-mix(in srgb, var(--accent-green) 50%, transparent);
}

.status-dot.off {
  background: color-mix(in srgb, var(--text-tertiary) 55%, transparent);
}

.status-dot.warn {
  background: var(--status-warn-text);
}

.status-dot.err {
  background: var(--danger-text);
}

.status-dot.pulse {
  animation: gw-dot-pulse 1.2s ease-in-out infinite;
}

@keyframes gw-dot-pulse {
  50% { opacity: 0.25; }
}

.cell-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-primary);
  font-weight: 500;
}

.name-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.type-badge {
  flex-shrink: 0;
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.3px;
  color: var(--accent-text);
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
}

.cell-host {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}

.host-text {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell-auth {
  display: flex;
  align-items: center;
  justify-content: flex-start;
}

.auth-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 10px;
  font-weight: 500;
  white-space: nowrap;
  color: var(--accent-text);
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
}

.cell-tags {
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 400;
}

.cell-tags.err { color: var(--danger-text); }
.cell-tags.warn { color: var(--status-warn-text); }

.cell-time,
.cell-created {
  font-size: 11px;
  color: var(--text-tertiary);
}

.cell-actions {
  display: flex;
  gap: 4px;
  justify-content: flex-end;
  align-items: center;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.gw-row:hover .cell-actions,
.gw-row:focus-within .cell-actions {
  opacity: 1;
}

.row-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 26px;
  padding: 0 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 11px;
  font-family: var(--font-sans);
  transition: all var(--transition-fast);
}

.row-btn.connect {
  color: var(--accent-text);
  border-color: color-mix(in srgb, var(--accent-primary) 30%, transparent);
}

.row-btn.connect:hover {
  background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
}

.row-btn.danger {
  color: var(--danger-text);
  border-color: color-mix(in srgb, var(--danger-text) 40%, transparent);
}

.row-btn.danger:hover {
  background: var(--danger-bg);
}

.row-btn.warn {
  color: var(--status-warn-text);
  border-color: color-mix(in srgb, var(--status-warn-text) 40%, transparent);
}

.row-btn.warn:hover {
  background: color-mix(in srgb, var(--status-warn-text) 12%, transparent);
}

.row-btn:disabled {
  pointer-events: none;
  opacity: 0.85;
  border-color: var(--accent-primary);
  background: color-mix(in srgb, var(--accent-primary) 8%, transparent);
  color: var(--accent-text);
}

.row-btn.more {
  width: 26px;
  padding: 0;
  justify-content: center;
}

.row-btn.more:hover {
  background: var(--bg-active);
  color: var(--text-primary);
}

.row-btn .spinner {
  animation: gw-spin 0.8s linear infinite;
  color: var(--accent-text);
}

@keyframes gw-spin {
  to { transform: rotate(360deg); }
}
</style>
