<template>
  <div class="tunnel-view">
    <div class="page-head">
      <div class="page-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
        <span>隧道</span>
      </div>
      <span class="head-spacer"></span>
      <button class="btn btn-primary" :disabled="!hasConnections" :title="hasConnections ? '' : '请先在远程终端页创建连接'" @click="openCreate">＋ 新建隧道</button>
    </div>

    <!-- 正文：头部钉住，仅此处滚动 -->
    <div class="tunnel-body">

      <!-- 状态筛选（计数随启停/删除实时变化） -->
      <div class="chips">
        <button v-for="f in FILTERS" :key="f.key" class="chip" :class="{ on: filter === f.key }" @click="filter = f.key">
          {{ f.label }} <span class="n">{{ counts[f.key] }}</span>
        </button>
      </div>

      <div class="tunnel-list">
        <div v-for="row in visibleRows" :key="row.rule.id" class="tunnel-row" :class="{ error: row.state === 'error', orphan: row.orphan }">
          <span class="dot" :class="row.dotClass"></span>

          <div class="row-main">
            <span class="badge" :class="row.rule.type">{{ TYPE_LABELS[row.rule.type] }}</span>
            <span class="rule-path">{{ row.path }}</span>
            <span v-if="row.warn" class="badge warn">{{ row.warn }}</span>
          </div>

          <div class="row-actions">
            <!-- D 类活动时提供一键复制代理地址 -->
            <button v-if="row.rule.type === 'dynamic' && row.state === 'active'" class="copy-chip" :class="{ ok: copyingId === row.rule.id }" title="复制代理地址" @click="copyAddr(row.rule)">
              {{ copyLabel(row.rule) }}
            </button>
            <button class="btn btn-sm" :disabled="row.orphan || busyId === row.rule.id" :title="row.orphan ? '连接配置已删除，无法启动' : ''" @click="toggle(row.rule)">
              {{ row.toggleLabel }}
            </button>
            <button class="icon-btn" title="编辑" :disabled="row.orphan" @click="openEdit(row.rule)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4Z"/></svg>
            </button>
            <!-- 删除两段确认：单击 → 变「确认删除」，3 秒不点自动还原 -->
            <button class="icon-btn" :class="{ confirming: confirmingId === row.rule.id }" title="删除" @click="askDelete(row.rule)">
              <svg v-if="confirmingId !== row.rule.id" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              <template v-else>确认删除</template>
            </button>
          </div>

          <div class="row-meta">
            <template v-if="row.orphan">
              <span class="orphan-tag">⚠ 连接配置已删除</span>
              <span>— 删除本规则或重建连接后可用</span>
            </template>
            <template v-else>
              <span class="conn-name">{{ row.connName }}</span>
              <span>{{ row.stateLabel }}</span>
              <span v-if="row.state === 'active' && row.connCount > 0" class="cnt">{{ row.connCount }} conn</span>
            </template>
          </div>

          <div v-if="row.state === 'error' && row.message" class="row-err">
            <span>{{ row.message }}</span>
            <button class="btn btn-sm" @click="toggle(row.rule)">重试</button>
          </div>
        </div>
      </div>

      <!-- 空状态：无规则，或当前筛选下无匹配 -->
      <div v-if="visibleRows.length === 0" class="empty">
        <div>{{ tunnelStore.rules.length === 0 ? '还没有隧道规则' : '没有符合当前筛选的隧道' }}</div>
        <div class="d">通过已保存的连接建立端口转发，隧道独立于终端会话运行。</div>
        <button v-if="tunnelStore.rules.length === 0 && hasConnections" class="btn btn-primary" @click="openCreate">＋ 新建隧道</button>
      </div>
    </div>

    <TunnelModal v-if="showModal" :rule="editingRule" @close="closeModal" @saved="onSaved" />
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onUnmounted, ref } from 'vue'
import { useTunnelStore } from '@/stores/tunnel'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'

const TunnelModal = defineAsyncComponent(() => import('./TunnelModal.vue'))

const tunnelStore = useTunnelStore()
const sshStore = useSSHStore()
const toast = useToastStore()

const TYPE_LABELS: Record<TunnelRule['type'], string> = { local: 'L', remote: 'R', dynamic: 'D' }
const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'active', label: '活动' },
  { key: 'error', label: '错误' },
  { key: 'stopped', label: '已停止' },
] as const
type FilterKey = (typeof FILTERS)[number]['key']

const filter = ref<FilterKey>('all')
const showModal = ref(false)
const editingRule = ref<TunnelRule | undefined>(undefined)
const busyId = ref('')
const confirmingId = ref('')
const copyingId = ref('')
let delTimer: ReturnType<typeof setTimeout> | null = null
let copyTimer: ReturnType<typeof setTimeout> | null = null

const hasConnections = computed(() => sshStore.connections.length > 0)
/** 第一个 D 类活动隧道的端口（footer 示例文案用） */
const sockPort = computed(() => {
  const d = tunnelStore.rules.find(r => r.type === 'dynamic' && tunnelStore.stateOf(r.id) === 'active')
  return d?.listenPort ?? 1080
})

interface Row {
  rule: TunnelRule
  state: TunnelStatus['state']
  orphan: boolean
  connName: string
  connCount: number
  path: string
  warn?: string
  dotClass: string
  stateLabel: string
  toggleLabel: string
  message?: string
}

function autoName(rule: TunnelRule): string {
  if (rule.type === 'local') return `L ${rule.listenPort} → ${rule.dstHost}:${rule.dstPort}`
  if (rule.type === 'remote') return `R ${rule.remoteBindHost || '127.0.0.1'}:${rule.bindPort} → ${rule.dstHost}:${rule.dstPort}`
  return `D SOCKS5 ${rule.listenHost || '127.0.0.1'}:${rule.listenPort}`
}

function pathOf(rule: TunnelRule): string {
  if (rule.type === 'local') return `${rule.listenHost || '127.0.0.1'}:${rule.listenPort} → ${rule.dstHost}:${rule.dstPort}`
  if (rule.type === 'remote') return `${rule.remoteBindHost || '127.0.0.1'}:${rule.bindPort} → ${rule.dstHost}:${rule.dstPort}`
  return `${rule.listenHost || '127.0.0.1'}:${rule.listenPort} · SOCKS5`
}

const rows = computed<Row[]>(() =>
  tunnelStore.rules.map(rule => {
    const status = tunnelStore.statuses[rule.id]
    const state = tunnelStore.stateOf(rule.id)
    const conn = sshStore.connections.find(c => c.id === rule.connectionId)
    const orphan = !conn
    const isRemoteOpenBind = rule.type === 'remote' && (rule.remoteBindHost === '0.0.0.0' || rule.remoteBindHost === '::')
    let stateLabel = '已停止'
    let dotClass = ''
    if (state === 'active') { stateLabel = '活动'; dotClass = 'active' }
    else if (state === 'starting') { stateLabel = '启动中'; dotClass = 'busy' }
    else if (state === 'error') { stateLabel = '错误'; dotClass = 'error' }
    const busy = busyId.value === rule.id
    return {
      rule,
      state,
      orphan,
      connName: conn?.name ?? '已删除',
      connCount: status?.connections ?? 0,
      path: pathOf(rule),
      warn: isRemoteOpenBind ? '对远端网段开放' : rule.type === 'dynamic' ? '仅本机' : undefined,
      dotClass,
      stateLabel: busy ? (state === 'active' ? '停止中' : '启动中') : stateLabel,
      toggleLabel: orphan ? '启动' : busy ? (state === 'active' ? '停止中…' : '启动中…') : state === 'active' ? '停止' : state === 'error' ? '重试' : '启动',
      message: status?.message,
    }
  }),
)

const visibleRows = computed(() => rows.value.filter(r => filter.value === 'all' || r.state === filter.value))

const counts = computed<Record<FilterKey, number>>(() => {
  const c: Record<FilterKey, number> = { all: rows.value.length, active: 0, error: 0, stopped: 0 }
  for (const r of rows.value) {
    if (r.state === 'active' || r.state === 'error' || r.state === 'stopped') c[r.state]++
    else c.stopped++ // starting 并入已停止桶外的兜底：仍算非活动，归入「已停止」可被筛出
  }
  return c
})

function openCreate() {
  editingRule.value = undefined
  showModal.value = true
}

function openEdit(rule: TunnelRule) {
  editingRule.value = { ...rule }
  showModal.value = true
}

function closeModal() {
  showModal.value = false
  editingRule.value = undefined
}

function onSaved() {
  closeModal()
}

/** 启停：状态迁移由主进程推送 + 返回值合并；busy 只管按钮过渡态。
 *  state=active → stop，其余（stopped/error/starting）→ start，重试按钮同路。 */
async function toggle(rule: TunnelRule) {
  const state = tunnelStore.stateOf(rule.id)
  busyId.value = rule.id
  try {
    if (state === 'active') await tunnelStore.stop(rule.id)
    else await tunnelStore.start(rule.id)
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '隧道操作失败'))
  } finally {
    busyId.value = ''
  }
}

/** 两段确认删除：再次点击才真删，3 秒超时还原（防误触） */
function askDelete(rule: TunnelRule) {
  if (confirmingId.value === rule.id) {
    confirmingId.value = ''
    if (delTimer) clearTimeout(delTimer)
    tunnelStore.remove(rule.id).catch(err => toast.error(toFriendlyMessage(err, '删除失败')))
    return
  }
  confirmingId.value = rule.id
  delTimer = setTimeout(() => { confirmingId.value = '' }, 3000)
}

function copyAddr(rule: TunnelRule) {
  const addr = `socks5h://${rule.listenHost || '127.0.0.1'}:${rule.listenPort}`
  void navigator.clipboard?.writeText(addr).catch(() => {})
  copyingId.value = rule.id
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = setTimeout(() => { copyingId.value = '' }, 1600)
}

function copyLabel(rule: TunnelRule): string {
  const addr = `socks5h://${rule.listenHost || '127.0.0.1'}:${rule.listenPort}`
  return copyingId.value === rule.id ? `${addr} ✓ 已复制` : `${addr} ⧉`
}

onUnmounted(() => {
  if (delTimer) clearTimeout(delTimer)
  if (copyTimer) clearTimeout(copyTimer)
})
</script>

<style scoped>
.tunnel-view {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-base);
}
/* 头部统一以 RemoteManager .rm-header 为准：44px 高 + 16px 侧距 + 底部分隔线 */
.page-head {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  height: var(--page-head-h);
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
}
.page-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--page-title-size);
  font-weight: 600;
  color: var(--text-primary);
  font-family: var(--font-sans);
}
.head-spacer {
  flex: 1;
}
.tunnel-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px 24px;
}
.icon-btn {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.icon-btn svg { width: 14px; height: 14px; }
.icon-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.icon-btn.confirming {
  width: auto;
  padding: 0 9px;
  gap: 5px;
  background: var(--danger-bg);
  color: var(--danger-text);
}
.icon-btn.confirming:hover { background: var(--danger-border); }
.icon-btn:disabled { opacity: 0.45; cursor: not-allowed; pointer-events: none; }

.chips {
  display: flex;
  gap: 6px;
  margin-bottom: 14px;
}
.chip {
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11.5px;
  cursor: pointer;
  border: 1px solid var(--border-subtle);
  background: transparent;
  color: var(--text-secondary);
  transition: all var(--transition-fast);
}
.chip:hover { border-color: var(--border-default); color: var(--text-primary); }
.chip.on { background: var(--bg-active); border-color: var(--border-default); color: var(--text-primary); }
.chip .n { font-family: var(--font-mono); font-size: 10.5px; color: var(--text-tertiary); }
.chip.on .n { color: var(--text-secondary); }

.tunnel-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tunnel-row {
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  padding: 11px 14px;
  display: grid;
  grid-template-columns: 10px 1fr auto;
  grid-template-areas: 'dot main act' '. meta act';
  gap: 3px 12px;
  align-items: start;
  transition: border-color var(--transition-fast);
}
.tunnel-row:hover { border-color: var(--border-default); }
.tunnel-row.error { border-color: var(--danger-border); }
.tunnel-row.orphan { opacity: 0.75; }

.dot {
  grid-area: dot;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--text-tertiary);
  margin-top: 5px;
}
.dot.active {
  background: var(--accent-green);
  box-shadow: 0 0 6px color-mix(in srgb, var(--accent-green) 50%, transparent);
}
.dot.busy { background: var(--accent-yellow); animation: pulse 1.2s ease-in-out infinite; }
.dot.error { background: var(--accent-red); }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.row-main {
  grid-area: main;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.badge {
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  flex: none;
}
.badge.L {
  color: var(--tint-l, var(--accent-cyan));
  background: color-mix(in srgb, var(--tint-l, var(--accent-cyan)) 12%, transparent);
}
.badge.R {
  color: var(--tint-r, var(--accent-purple));
  background: color-mix(in srgb, var(--tint-r, var(--accent-purple)) 14%, transparent);
}
.badge.D {
  color: var(--tint-d, var(--accent-indigo));
  background: color-mix(in srgb, var(--tint-d, var(--accent-indigo)) 14%, transparent);
}
.badge.warn {
  color: var(--accent-yellow);
  background: color-mix(in srgb, var(--accent-yellow) 12%, transparent);
  font-weight: 400;
}

.rule-path {
  font-family: var(--font-mono);
  font-size: 12.5px;
}

.row-meta {
  grid-area: meta;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--text-tertiary);
}
.row-meta .conn-name { color: var(--text-secondary); }
.cnt {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--accent-green);
  background: color-mix(in srgb, var(--accent-green) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-green) 25%, transparent);
  border-radius: 8px;
  padding: 0 7px;
  line-height: 16px;
}
.orphan-tag { color: var(--accent-yellow); }

.row-actions {
  grid-area: act;
  display: flex;
  align-items: center;
  gap: 4px;
}
.copy-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--tint-l, var(--accent-cyan));
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
  cursor: pointer;
  transition: border-color var(--transition-fast);
}
.copy-chip:hover { border-color: var(--accent-primary); }
.copy-chip.ok {
  color: var(--accent-green);
  border-color: color-mix(in srgb, var(--accent-green) 40%, transparent);
}

.row-err {
  grid-area: meta;
  margin-top: 5px;
  font-size: 11.5px;
  color: var(--danger-text);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  border-radius: var(--radius-sm);
  padding: 5px 9px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.row-err .btn { margin-left: auto; }

.empty {
  text-align: center;
  padding: 50px 0;
  color: var(--text-tertiary);
  font-size: 13px;
}
.empty .d {
  font-size: 12px;
  margin-top: 4px;
}
.empty .btn { margin-top: 16px; }

.foot-hint {
  margin-top: 14px;
  font-size: 11.5px;
  color: var(--text-tertiary);
  display: flex;
  gap: 6px;
  align-items: baseline;
}
.foot-hint code {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
}
</style>
