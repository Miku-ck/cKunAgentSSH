<template>
  <div class="tunnel-view">
    <div class="page-head">
      <div class="page-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M4 20v-7a8 8 0 0 1 16 0v7"/><path d="M12 20v-7"/><path d="M2 20h20"/></svg>
        <span>隧道</span>
      </div>
      <span class="head-spacer"></span>
      <button class="btn btn-primary" :disabled="!hasConnections" :title="hasConnections ? '' : '请先在远程终端页创建连接'" @click="openCreate">新建隧道</button>
    </div>

    <!-- 正文：头部钉住，仅此处滚动 -->
    <div class="tunnel-body">

      <!-- 空态与行共用 .tunnel-list：额外的同级空壳 div 会平分 flex:1，把空态的垂直居中顶偏 -->
      <div class="tunnel-list">
        <div v-if="rows.length === 0" class="empty">
          <div>还没有隧道规则</div>
        </div>
        <div v-for="row in rows" :key="row.rule.id" class="tunnel-row" :class="{ error: row.state === 'error', orphan: row.orphan }">
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

const showModal = ref(false)
const editingRule = ref<TunnelRule | undefined>(undefined)
const busyId = ref('')
const confirmingId = ref('')
const copyingId = ref('')
let delTimer: ReturnType<typeof setTimeout> | null = null
let copyTimer: ReturnType<typeof setTimeout> | null = null

const hasConnections = computed(() => sshStore.connections.length > 0)

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
  // 与下面 copyAddr 同法：旧定时器不先 clear，A 行那 3 秒到点会把 B 行的确认态抹掉
  if (delTimer) clearTimeout(delTimer)
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
  /* 整页结构画布：走 --canvas-bg（默认=--bg-base，见 DashboardView 同注） */
  background: var(--canvas-bg);
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
  /* flex 纵向布局：列表区撑满剩余空间，空态在其中双向居中 */
  display: flex;
  flex-direction: column;
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

.tunnel-list {
  flex: 1;
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
  /* err 单独第三行：与 meta 同格会叠放，错误横幅盖住连接名那行 */
  grid-template-areas: 'dot main act' '. meta act' '. err err';
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
  border-radius: var(--radius-lg);
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
  grid-area: err;
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
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 50px 0;
  color: var(--text-tertiary);
  font-size: 13px;
}
</style>
