<template>
  <div class="remote-manager">
    <!-- 头部 -->
    <div class="rm-header">
      <div class="rm-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
        <span>远程管理</span>
      </div>
      <div class="rm-header-actions">
        <button v-if="remoteSessionCount > 1" class="btn" @click="showBulkModal = true" title="对当前活动远程会话批量下发命令并对比结果">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
          批量下发（{{ remoteSessionCount }} 台）
        </button>
        <button class="btn btn-primary" @click="showNewDialog = true">新建连接</button>
      </div>
    </div>

    <!-- 工具栏：两类连接共用一条搜索，类型由行内徽标区分 -->
    <div class="rm-toolbar">
      <div class="rm-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input v-model="searchText" placeholder="搜索名称/主机/用户名/备注..." />
      </div>
    </div>

    <!-- 列表：SSH 连接与 TLS 网关混排成一张表，类型由行内徽标区分 -->
    <div class="rm-list">
      <!-- 表头 (列名可点击排序) -->
      <div
        class="rm-row-header"
        :class="{ 'has-selection': selectedIds.size > 0 }"
        v-if="filteredRows.length > 0"
      >
        <div class="cell-check">
          <input type="checkbox" :checked="allSelected" :indeterminate.prop="someSelected" title="全选 SSH 连接" aria-label="全选 SSH 连接" @change="toggleSelectAll" />
        </div>
        <div class="cell-status"></div>
        <div class="cell-name sortable" @click="setSort('name')">
          名称
          <span class="sort-arrow" v-if="sortKey === 'name'">▾</span>
        </div>
        <div class="cell-host">主机</div>
        <div class="cell-auth">认证</div>
        <div class="cell-tags">备注</div>
        <div class="cell-time sortable" @click="setSort('recent')">
          最后连接
          <span class="sort-arrow" v-if="sortKey === 'recent'">▾</span>
        </div>
        <div class="cell-created sortable" @click="setSort('created')">
          创建时间
          <span class="sort-arrow" v-if="sortKey === 'created'">▾</span>
        </div>
        <div class="cell-actions"></div>
      </div>

      <!-- 行：SSH 行可多选批量删除；TLS 网关行不参与批量选择 -->
      <template v-for="row in filteredRows" :key="row.key">
        <ConnectionRow
          v-if="row.kind === 'ssh'"
          :connection="row.conn"
          :selected="selectedIds.has(row.conn.id)"
          :online="store.isOnline(row.conn.id)"
          :connecting="props.connecting"
          :pending-conn-id="props.pendingConnId"
          :now="nowTick"
          @connect="handleConnect"
          @toggle-select="handleToggleSelect"
          @menu="showSshMenu"
        />
        <GatewayRow
          v-else
          :gateway="row.gw"
          :status="accessStore.statusOf(row.gw.id)"
          :busy="gatewayBusyId === row.gw.id"
          :now="nowTick"
          @toggle="handleGatewayToggle(row.gw)"
          @menu="showGatewayMenu($event, row.gw)"
        />
      </template>

      <!-- 空状态：一句话双向居中（对齐 Docker 页 .empty 的做法）。新建连接在头部一直有，
           这里不放重复按钮；标题/说明拆两个 div 属冗余，压平成单行。 -->
      <div v-if="filteredRows.length === 0" class="rm-empty">
        {{ emptyText }}
      </div>
    </div>

    <!-- 批量操作栏：选中项只可能是 SSH 连接 -->
    <div v-if="selectedIds.size > 0" class="rm-batch-bar">
      <span class="batch-info">已选 {{ selectedIds.size }} 项</span>
      <button class="btn btn-danger" @click="handleDeleteBatch">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        删除
      </button>
      <button class="btn" @click="clearSelection">取消选择</button>
    </div>

    <!-- 右键菜单：按行类型给出不同动作（SSH 开终端；TLS 接入/断开） -->
    <Teleport to="body">
      <div
        v-if="menu.visible"
        class="ctx-menu"
        :style="menuStyle"
        @contextmenu.prevent
      >
        <template v-if="menu.target?.kind === 'ssh'">
          <div class="ctx-item" @click="handleMenuAction('connect')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
            打开终端
          </div>
          <div class="ctx-item" @click="handleMenuAction('edit')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
            编辑
          </div>
          <div class="ctx-item" @click="handleMenuAction('duplicate')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            复制配置
          </div>
          <div class="ctx-sep"></div>
          <div class="ctx-item danger" @click="handleMenuAction('delete')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            删除
          </div>
        </template>

        <template v-else-if="menu.target?.kind === 'gateway'">
          <div class="ctx-item" @click="handleMenuAction('toggle')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M12 3v9"/><path d="M6.6 6.6a8 8 0 1 0 10.8 0"/></svg>
            {{ gatewayToggleLabel(menu.target.gw.id) }}
          </div>
          <div class="ctx-item" @click="handleMenuAction('edit')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
            编辑
          </div>
          <div class="ctx-sep"></div>
          <div class="ctx-item danger" @click="handleMenuAction('delete')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            删除
          </div>
        </template>
      </div>
    </Teleport>

    <!-- 多节点批量下发（F 阶段） -->
    <BulkCommandModal v-if="showBulkModal" @close="showBulkModal = false" />

    <!-- 新建连接：SSH / TLS 两套表单在同一弹窗内用页签切换 -->
    <NewConnectionDialog
      v-if="showNewDialog"
      @close="showNewDialog = false"
      @saved-ssh="onNewSshSaved"
      @saved-tls="onNewTlsSaved"
    />

    <!-- 网关编辑与 TOFU 指纹确认（无可见输出，只有弹窗） -->
    <GatewayDialogs ref="gatewayDialogsRef" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted, triggerRef } from 'vue'
import ConnectionRow from './ConnectionRow.vue'
import GatewayRow from './GatewayRow.vue'
import NewConnectionDialog from './NewConnectionDialog.vue'
import GatewayDialogs from './GatewayDialogs.vue'
import BulkCommandModal from './BulkCommandModal.vue'
import { connNote, useSSHStore } from '@/stores/ssh'
import type { SSHConnection } from '@/stores/ssh'
import { useAccessStore } from '@/stores/access'
import { useTunnelStore } from '@/stores/tunnel'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { toFriendlyMessage } from '@/api/client'

/** 统一列表的一行：两类连接各保留自己的记录与动作，只在表格里混排 */
type ConnectionRowItem =
  | { kind: 'ssh'; key: string; name: string; recent: number; created: number; conn: SSHConnection }
  | { kind: 'gateway'; key: string; name: string; recent: number; created: number; gw: GatewayRecord }

type MenuTarget =
  | { kind: 'ssh'; conn: SSHConnection }
  | { kind: 'gateway'; gw: GatewayRecord }

const emit = defineEmits<{
  connect: [id: string]
  edit: [conn: SSHConnection]
}>()

// 新增：连接 loading 状态（从父组件传入）
const props = defineProps<{
  connecting: boolean
  pendingConnId: string
}>()

const accessStore = useAccessStore()
const tunnelStore = useTunnelStore()
const gatewayDialogsRef = ref<{ openEdit(gw: GatewayRecord): void } | null>(null)
const gatewayBusyId = ref('')

const showNewDialog = ref(false)

function onNewSshSaved({ savedId, connectNow }: { savedId: string; connectNow: boolean }): void {
  if (connectNow) emit('connect', savedId)
}

function onNewTlsSaved({ rec, submittedPassword }: { rec: GatewayRecord; submittedPassword: boolean }): void {
  if (!submittedPassword) return
  void accessStore.connect(rec.id).catch(err => {
    toast.error(toFriendlyMessage(err, '网关连接失败'))
  })
}

const store = useSSHStore()
const toast = useToastStore()
const { confirm } = useConfirm()

// ── 多节点批量下发（F 阶段） ──
const showBulkModal = ref(false)
const remoteSessionCount = computed(() => store.sessions.filter(s => s.type === 'remote').length)

const searchText = ref('')
const debouncedSearch = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null

// 行内「最近连接」是相对时间，需要一个会动的基准；整张表共用一个定时器
// （与 SchedulesView 的下次执行倒计时同套路），随卸载清理。
const nowTick = ref(Date.now())
const relativeTick = setInterval(() => { nowTick.value = Date.now() }, 60_000)
const sortKey = ref<'recent' | 'name' | 'created'>('recent')
const selectedIds = ref<Set<string>>(new Set())

function setSort(key: 'recent' | 'name' | 'created') {
  sortKey.value = key
}

// 搜索防抖
watch(searchText, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    debouncedSearch.value = searchText.value
  }, 200)
})

const hasFilter = computed(() => debouncedSearch.value.trim() !== '')

/** 搜索命中的 SSH 记录：与网关共用一条搜索词，但批量选择只认 SSH */
function sshMatches(c: SSHConnection, q: string): boolean {
  if (!q) return true
  const note = connNote(c)
  return c.name.toLowerCase().includes(q) ||
    c.host.toLowerCase().includes(q) ||
    c.username.toLowerCase().includes(q) ||
    note.toLowerCase().includes(q)
}

function gatewayMatches(gw: GatewayRecord, q: string): boolean {
  if (!q) return true
  return gw.name.toLowerCase().includes(q) ||
    gw.host.toLowerCase().includes(q) ||
    gw.user.toLowerCase().includes(q)
}

const filteredRows = computed<ConnectionRowItem[]>(() => {
  const q = debouncedSearch.value.toLowerCase().trim()
  const rows: ConnectionRowItem[] = []

  for (const c of store.connections) {
    if (!sshMatches(c, q)) continue
    rows.push({
      kind: 'ssh',
      key: `ssh-${c.id}`,
      name: c.name,
      recent: c.lastConnectedAt || 0,
      created: c.createdAt,
      conn: c,
    })
  }

  for (const gw of accessStore.gateways) {
    if (!gatewayMatches(gw, q)) continue
    rows.push({
      kind: 'gateway',
      key: `gw-${gw.id}`,
      name: gw.name,
      // 网关无持久化的最后连接；接入成功时主进程打 sinceAt（断开不清），
      // 运行期内可当「最近接入」参与排序，重启进程后回落到底部
      recent: accessStore.statusOf(gw.id)?.sinceAt ?? 0,
      created: gw.createdAt,
      gw,
    })
  }

  switch (sortKey.value) {
    case 'recent':
      rows.sort((a, b) => b.recent - a.recent)
      break
    case 'name':
      rows.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
      break
    case 'created':
      rows.sort((a, b) => b.created - a.created)
      break
  }

  return rows
})

/** 批量选择只作用于当前可见的 SSH 连接 */
const filteredConnections = computed<SSHConnection[]>(() =>
  filteredRows.value.flatMap(r => r.kind === 'ssh' ? [r.conn] : [])
)

const emptyText = computed(() => {
  if (hasFilter.value) return '没有匹配的连接'
  // 列表空但网关没读到：说破，别把「没读到」洗成「还没有」
  if (!accessStore.loaded) return '没读到网关列表'
  return '还没有远程连接'
})

// 列表收窄后剪掉看不见的选中项：搜索/排序变化会让 selectedIds 残留已不在列表里的
// 连接，而「已选 N 项」与批量删除都按 selectedIds 走 —— 用户删的是当前界面根本
// 看不到的连接。
watch(filteredConnections, conns => {
  if (selectedIds.value.size === 0) return
  const visible = new Set(conns.map(c => c.id))
  let changed = false
  for (const id of Array.from(selectedIds.value)) {
    if (!visible.has(id)) {
      selectedIds.value.delete(id)
      changed = true
    }
  }
  if (changed) triggerRef(selectedIds)
})

const allSelected = computed(() =>
  filteredConnections.value.length > 0 &&
  filteredConnections.value.every(c => selectedIds.value.has(c.id))
)
const someSelected = computed(() =>
  selectedIds.value.size > 0 && !allSelected.value
)

function toggleSelectAll() {
  if (allSelected.value) {
    for (const c of filteredConnections.value) {
      selectedIds.value.delete(c.id)
    }
  } else {
    for (const c of filteredConnections.value) {
      selectedIds.value.add(c.id)
    }
  }
  triggerRef(selectedIds)
}

function handleToggleSelect(id: string) {
  if (selectedIds.value.has(id)) {
    selectedIds.value.delete(id)
  } else {
    selectedIds.value.add(id)
  }
  triggerRef(selectedIds)
}

function clearSelection() {
  selectedIds.value.clear()
  triggerRef(selectedIds)
}

// ─── 操作 ─────────────────────────────────
function handleConnect(id: string) {
  emit('connect', id)
}

function handleEdit(conn: SSHConnection) {
  emit('edit', conn)
}

async function handleDuplicate(id: string) {
  try {
    await store.duplicateConnection(id)
    toast.success('已复制连接')
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '复制失败'))
  }
}

async function handleDelete(id: string) {
  const conn = store.connections.find(c => c.id === id)
  if (!conn) return
  const ok = await confirm({
    title: '删除连接',
    message: `确认删除连接「${conn.name}」?\n此操作不可撤销。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await store.removeConnection(id)
    selectedIds.value.delete(id)
    triggerRef(selectedIds)
    toast.success('已删除')
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '删除失败'))
  }
}

const deleting = ref(false)
async function handleDeleteBatch() {
  const count = selectedIds.value.size
  if (count === 0) return
  if (deleting.value) return // 防重复点击
  const ok = await confirm({
    title: '批量删除',
    message: `确认删除选中的 ${count} 个连接?\n此操作不可撤销。`,
    confirmText: '全部删除',
    danger: true,
  })
  if (!ok) return
  deleting.value = true
  const ids = Array.from(selectedIds.value)
  try {
    for (const id of ids) {
      await store.removeConnection(id)
    }
    clearSelection()
    toast.success(`已删除 ${count} 个连接`)
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '删除失败'))
  } finally {
    deleting.value = false
  }
}

// ─── TLS 网关动作 ─────────────────────────
function gatewayToggleLabel(id: string): string {
  const s = accessStore.stateOf(id)
  if (s === 'online') return '断开'
  if (s === 'reconnecting') return '停止重连'
  return '接入'
}

async function handleGatewayToggle(gw: GatewayRecord): Promise<void> {
  if (gatewayBusyId.value === gw.id) return
  gatewayBusyId.value = gw.id
  try {
    const s = accessStore.stateOf(gw.id)
    if (s === 'online' || s === 'reconnecting') await accessStore.disconnect(gw.id)
    else await accessStore.connect(gw.id)
  } catch (err) {
    toast.error(toFriendlyMessage(err, '网关操作失败'))
  } finally {
    gatewayBusyId.value = ''
  }
}

function openGatewayEdit(gw: GatewayRecord): void {
  gatewayDialogsRef.value?.openEdit(gw)
}

async function handleGatewayDelete(gw: GatewayRecord): Promise<void> {
  const ok = await confirm({
    title: '删除连接',
    message: `确定删除连接「${gw.name}」？引用它的隧道规则将变为「网关未连接」。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await accessStore.remove(gw.id)
    // 删除已经成功，刷新失败不能再报成「删除失败」
    await tunnelStore.refreshRules().catch(() => {
      toast.error('隧道规则刷新失败，切到隧道页会重新加载')
    })
  } catch (err) {
    toast.error(toFriendlyMessage(err, '删除失败'))
  }
}

// ─── 右键菜单 ─────────────────────────────
const menu = ref({ visible: false, x: 0, y: 0, target: null as MenuTarget | null })
const MENU_MIN_WIDTH = 160
const MENU_MAX_HEIGHT = 280

const menuStyle = computed(() => {
  const margin = 8
  const vw = window.innerWidth
  const vh = window.innerHeight
  // 横向: 右侧空间不足时改为向左展开
  const left = menu.value.x + MENU_MIN_WIDTH + margin > vw
    ? Math.max(margin, menu.value.x - MENU_MIN_WIDTH)
    : menu.value.x
  // 纵向: 下方空间不足时向上收缩
  const top = menu.value.y + MENU_MAX_HEIGHT + margin > vh
    ? Math.max(margin, vh - MENU_MAX_HEIGHT - margin)
    : menu.value.y
  return { left: left + 'px', top: top + 'px' }
})

let dismissBound = false

function showMenu(e: MouseEvent, target: MenuTarget) {
  e.stopPropagation()
  e.preventDefault()
  // 坐标只存原始光标位置：越界校正是 menuStyle 的职责（两个方向都按 MENU_MAX_HEIGHT
  // 与 margin 收过，这里再翻一次等于对同一维度做两次钳制）
  menu.value = { visible: true, x: e.clientX, y: e.clientY, target }
  // 在菜单显示后才注册 document mousedown 监听(bubble 阶段,普通事件流)
  if (!dismissBound) {
    document.addEventListener('mousedown', handleDocMouseDown)
    dismissBound = true
  }
}

function showSshMenu(e: MouseEvent, conn: SSHConnection) {
  showMenu(e, { kind: 'ssh', conn })
}

function showGatewayMenu(e: MouseEvent, gw: GatewayRecord) {
  showMenu(e, { kind: 'gateway', gw })
}

function handleDocMouseDown(e: MouseEvent) {
  // 如果点击在菜单内部 (.ctx-menu),忽略,让菜单项的 @click 自己处理
  const target = e.target as HTMLElement
  if (target.closest('.ctx-menu')) return
  menu.value.visible = false
  // 关闭后解绑,等下次 showMenu 时再注册,避免常驻监听
  document.removeEventListener('mousedown', handleDocMouseDown)
  dismissBound = false
}

function handleMenuAction(action: string) {
  const target = menu.value.target
  menu.value.visible = false
  if (!target) return
  if (target.kind === 'ssh') {
    switch (action) {
      case 'connect': handleConnect(target.conn.id); break
      case 'edit': handleEdit(target.conn); break
      case 'duplicate': handleDuplicate(target.conn.id); break
      case 'delete': handleDelete(target.conn.id); break
    }
    return
  }
  switch (action) {
    case 'toggle': void handleGatewayToggle(target.gw); break
    case 'edit': openGatewayEdit(target.gw); break
    case 'delete': void handleGatewayDelete(target.gw); break
  }
}

onUnmounted(() => {
  // C33 修复：卸载时清理搜索防抖定时器（原实现残留，卸载后回调仍会写入 ref）
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  clearInterval(relativeTick)
  if (dismissBound) {
    document.removeEventListener('mousedown', handleDocMouseDown)
    dismissBound = false
  }
})
</script>

<style scoped>
.remote-manager {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  /* 整页结构画布：走 --canvas-bg（默认=--bg-base；详见 DashboardView 同注）。 */
  background: var(--canvas-bg);
}

/* 头部 */
.rm-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--page-head-h);
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.rm-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--page-title-size);
  font-weight: 600;
  color: var(--text-primary);
  font-family: var(--font-sans);
}

.rm-header-actions {
  display: flex;
  gap: 8px;
}

/* 工具栏 */
.rm-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  flex-wrap: wrap;
}

.rm-search {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 240px;
  height: 30px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  transition: border-color var(--transition-fast);
}

.rm-search:focus-within {
  border-color: var(--accent-primary);
}

.rm-search input {
  flex: 1;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-sans);
  outline: none;
  min-width: 0;
}

.rm-search input::placeholder {
  color: var(--text-tertiary);
}

/* 列表 */
.rm-list {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  /* Grid 模板: 9 列布局,行和表头严格对齐 */
  --rm-grid-cols: 22px 12px minmax(110px, 1.1fr) minmax(160px, 1.4fr) 60px minmax(80px, 0.9fr) 84px 84px 112px;
}

.rm-row-header {
  display: grid;
  grid-template-columns: var(--rm-grid-cols);
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 12px;
  border-bottom: 1px solid var(--border-default);
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  position: sticky;
  top: 0;
  background: var(--bg-base);
  z-index: 1;
  text-align: left;
}

.rm-row-header > div {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.rm-row-header .cell-check {
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.rm-row-header:hover .cell-check,
.rm-row-header.has-selection .cell-check {
  opacity: 1;
}

.rm-row-header .cell-check input {
  width: 13px;
  height: 13px;
  accent-color: var(--accent-primary);
  cursor: pointer;
}
.rm-row-header .cell-status { width: 100%; }
.rm-row-header .cell-time,
.rm-row-header .cell-created { text-align: left; }

/* 表头排序指示 */
.sortable {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 4px;
  transition: color var(--transition-fast);
}

.sortable:hover {
  color: var(--text-secondary);
}

/* 当前排序列只靠 ▾ 箭头表意：原先再上一道强调色，表头里就那一列与其他列不同色 */
.sort-arrow {
  font-size: 10px;
  display: inline-block;
}

/* 空状态：撑满列表区双向居中，单行短句 */
.rm-empty {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 20px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
  text-align: center;
}

/* 按钮 */
/* 批量栏 */
.rm-batch-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 40px;
  padding: 0 16px;
  border-top: 1px solid var(--border-default);
  /* 底部批量操作栏：结构面走 --canvas-bg 与整页统一 */
  background: var(--canvas-bg);
  flex-shrink: 0;
}

.batch-info {
  font-size: 12px;
  color: var(--text-secondary);
  margin-right: auto;
}

/* 右键菜单 */
.ctx-menu {
  position: fixed;
  z-index: 9999;
  min-width: 160px;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: var(--shadow-lg);
}

.ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 4px;
  transition: background 0.1s;
  font-family: var(--font-sans);
}

.ctx-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.ctx-item.danger {
  color: var(--accent-red);
}

.ctx-item.danger:hover {
  background: color-mix(in srgb, var(--accent-red) 15%, transparent);
  color: var(--accent-red);
}

.ctx-sep {
  height: 1px;
  margin: 4px 0;
  background: var(--border-subtle);
}
</style>
