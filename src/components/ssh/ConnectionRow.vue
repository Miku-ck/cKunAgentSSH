<template>
  <div
    class="conn-row"
    :class="{ selected, online, 'check-visible': selected }"
    @contextmenu.prevent="$emit('menu', $event, connection)"
  >
    <div class="cell-check" @click.stop="$emit('toggle-select', connection.id)">
      <input type="checkbox" :checked="selected" @click.stop="$emit('toggle-select', connection.id)" />
    </div>

    <div class="cell-status">
      <span class="status-dot" :class="online ? 'on' : 'off'" :title="online ? '在线' : '离线'"></span>
    </div>

    <div class="cell-name" :title="connection.name">
      <span class="name-text">{{ connection.name }}</span>
    </div>

    <div class="cell-host" :title="`${connection.username}@${connection.host}:${connection.port}`">
      <span class="host-text">{{ connection.username }}@{{ connection.host }}<template v-if="connection.port !== 22">:{{ connection.port }}</template></span>
    </div>

    <div class="cell-auth">
      <span class="auth-badge" :class="connection.authType">{{ authLabel }}</span>
    </div>

    <div class="cell-tags" :title="noteText || ''">
      {{ noteText }}
    </div>

    <div class="cell-time">
      <span :title="fullTime">{{ relativeTime }}</span>
    </div>

    <div class="cell-created">
      <span :title="fullCreatedTime">{{ createdDateText }}</span>
    </div>

    <div class="cell-actions" @click.stop>
      <button class="row-btn connect" :class="{ loading: isConnecting }" :title="isConnecting ? '连接中...' : '连接'" @click="$emit('connect', connection.id)">
        <svg v-if="!isConnecting" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        <svg v-else class="spinner" viewBox="0 0 24 24" fill="none" width="14" height="14"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-opacity="0.25"/><path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
        <span v-if="!isConnecting">连接</span>
        <span v-else class="loading-text">连接中...</span>
      </button>
      <button class="row-btn more" title="更多操作" @click.stop="$emit('menu', $event, connection)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { connNote, type SSHConnection } from '@/stores/ssh'

const props = defineProps<{
  connection: SSHConnection
  selected: boolean
  online: boolean
  /** 是否正在连接中 */
  connecting: boolean
  /** 当前正在连接中的连接 ID */
  pendingConnId: string
  /** 「最近连接」相对时间的基准时刻（父级定时 tick，见 relativeTime 的说明） */
  now: number
}>()

/** 当前行是否处于连接中状态 */
const isConnecting = computed(() => props.connecting && props.pendingConnId === props.connection.id)

defineEmits<{
  connect: [id: string]
  edit: [conn: SSHConnection]
  duplicate: [id: string]
  delete: [id: string]
  'toggle-select': [id: string]
  menu: [e: MouseEvent, conn: SSHConnection]
}>()

const authLabel = computed(() => {
  switch (props.connection.authType) {
    case 'password': return '密码'
    case 'privateKey': return '私钥'
    case 'agent': return 'Agent'
    default: return props.connection.authType
  }
})

// 备注显示文本：note 优先、tags[0] 兜底，读法收归 connNote 单一来源
const noteText = computed(() => connNote(props.connection))

// 相对时间的基准由父级 tick 传入：Date.now() 不是响应式来源，写在 computed 里
// 等于「挂载那一刻定稿」——刚连上时显示"刚刚"，之后几小时（甚至重开面板前）都不会
// 变成"3 分钟前"，除非该行的 lastConnectedAt 恰好又变一次。
const relativeTime = computed(() => formatRelative(props.connection.lastConnectedAt, props.now))
const fullTime = computed(() => {
  const t = props.connection.lastConnectedAt
  if (!t) return '从未连接'
  return new Date(t).toLocaleString()
})

// 创建时间显示为 YYYY-MM-DD(本地时区)
const createdDateText = computed(() => formatDate(props.connection.createdAt))
const fullCreatedTime = computed(() => new Date(props.connection.createdAt).toLocaleString())

function formatRelative(ts: number | undefined, now: number): string {
  if (!ts) return '从未连接'
  const diff = now - ts
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return new Date(ts).toLocaleDateString()
}

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
.conn-row {
  display: grid;
  grid-template-columns: var(--rm-grid-cols, 22px 12px minmax(110px, 1.1fr) minmax(160px, 1.4fr) 60px minmax(80px, 0.9fr) 84px 84px 96px);
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

.conn-row:hover {
  background: var(--bg-hover);
}

.conn-row.selected {
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
}

/* 单元格基础样式 */
.conn-row > div {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

/* ── 复选框 (默认隐藏,hover/selected 时显示) ── */
.cell-check {
  display: flex;
  align-items: center;
  opacity: 0;
  transition: opacity var(--transition-fast);
  pointer-events: none;
}

.conn-row:hover .cell-check,
.conn-row.selected .cell-check,
.conn-row.check-visible .cell-check {
  opacity: 1;
  pointer-events: auto;
}

.cell-check input {
  width: 13px;
  height: 13px;
  accent-color: var(--accent-primary);
  cursor: pointer;
}

/* ── 状态点 ── */
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
  /* 55% 的 --text-tertiary 在深色底上落回原来那档 #4a4a4a，浅/手记主题则跟着主题的灰走 */
  background: color-mix(in srgb, var(--text-tertiary) 55%, transparent);
}

/* ── 名称 ── */
.cell-name {
  font-size: 13px;
  color: var(--text-primary);
  font-weight: 500;
  text-align: left;
}

.name-text {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── 主机 ── */
.cell-host {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  text-align: left;
}

.host-text {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── 认证方式 ── */
.cell-auth {
  display: flex;
  align-items: center;
  justify-content: flex-start;
}

.auth-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 3px;
  font-size: 10px;
  font-weight: 500;
  text-align: left;
  white-space: nowrap;
}

.auth-badge.password {
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
  color: var(--accent-text);
}

.auth-badge.privateKey {
  background: color-mix(in srgb, var(--accent-purple) 12%, transparent);
  color: var(--accent-purple);
}

.auth-badge.agent {
  background: color-mix(in srgb, var(--accent-green) 12%, transparent);
  color: var(--accent-green);
}

/* ── 备注 ── */
.cell-tags {
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 400;
}

/* ── 最后连接时间 ── */
.cell-time {
  text-align: left;
  font-size: 11px;
  color: var(--text-tertiary);
}

/* ── 创建时间 ── */
.cell-created {
  text-align: left;
  font-size: 11px;
  color: var(--text-tertiary);
}

/* ── 操作 (默认隐藏,hover 时显示) ── */
.cell-actions {
  display: flex;
  gap: 4px;
  justify-content: flex-end;
  align-items: center;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.conn-row:hover .cell-actions,
.conn-row.selected .cell-actions {
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

.row-btn.more {
  width: 26px;
  padding: 0;
  justify-content: center;
}

.row-btn.more:hover {
  background: var(--bg-active);
  color: var(--text-primary);
}

/* 连接中 spinner 动画 */
.row-btn.connect.loading {
  pointer-events: none;
  opacity: 0.85;
  border-color: var(--accent-primary);
  background: color-mix(in srgb, var(--accent-primary) 8%, transparent);
}
.row-btn.connect .spinner {
  animation: spin 0.8s linear infinite;
  color: var(--accent-text);
}
.row-btn.connect .loading-text {
  color: var(--accent-text);
  font-weight: 500;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
