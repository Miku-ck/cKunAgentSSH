<template>
  <div class="remote-manager">
    <!-- 头部 -->
    <div class="rm-header">
      <div class="rm-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
        <span>远程管理</span>
      </div>
      <div class="rm-header-actions">
        <button class="rm-btn primary" @click="$emit('new-connection')" title="新建连接">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          新建连接
        </button>
      </div>
    </div>

    <!-- 工具栏 -->
    <div class="rm-toolbar">
      <div class="rm-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input v-model="searchText" placeholder="搜索名称/主机/用户名/备注..." />
      </div>
    </div>

    <!-- 列表 -->
    <div class="rm-list">
      <!-- 表头 (列名可点击排序) -->
      <div
        class="rm-row-header"
        :class="{ 'has-selection': selectedIds.size > 0 }"
        v-if="filteredConnections.length > 0"
      >
        <div class="cell-check">
          <input type="checkbox" :checked="allSelected" :indeterminate.prop="someSelected" @change="toggleSelectAll" />
        </div>
        <div class="cell-status"></div>
        <div class="cell-name sortable" :class="{ active: sortKey === 'name' }" @click="setSort('name')">
          名称
          <span class="sort-arrow" v-if="sortKey === 'name'">▾</span>
        </div>
        <div class="cell-host">主机</div>
        <div class="cell-auth">认证</div>
        <div class="cell-tags">备注</div>
        <div class="cell-time sortable" :class="{ active: sortKey === 'recent' }" @click="setSort('recent')">
          最后连接
          <span class="sort-arrow" v-if="sortKey === 'recent'">▾</span>
        </div>
        <div class="cell-created sortable" :class="{ active: sortKey === 'created' }" @click="setSort('created')">
          创建时间
          <span class="sort-arrow" v-if="sortKey === 'created'">▾</span>
        </div>
        <div class="cell-actions"></div>
      </div>

      <!-- 行 -->
      <ConnectionRow
        v-for="conn in filteredConnections"
        :key="conn.id"
        :connection="conn"
        :selected="selectedIds.has(conn.id)"
        :online="store.isOnline(conn.id)"
        :connecting="props.connecting"
        :pending-conn-id="props.pendingConnId"
        @connect="handleConnect"
        @edit="handleEdit"
        @duplicate="handleDuplicate"
        @delete="handleDelete"
        @toggle-select="handleToggleSelect"
        @menu="showMenu"
      />

      <!-- 空状态 -->
      <div v-if="filteredConnections.length === 0" class="rm-empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="48" height="48"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
        <div class="empty-title">{{ hasFilter ? '没有匹配的连接' : '还没有远程连接' }}</div>
        <div class="empty-desc">{{ hasFilter ? '尝试调整搜索或过滤条件' : '新建第一个连接,开始管理远程服务器' }}</div>
        <button v-if="!hasFilter" class="rm-btn primary" @click="$emit('new-connection')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          新建连接
        </button>
      </div>
    </div>

    <!-- 批量操作栏 -->
    <div v-if="selectedIds.size > 0" class="rm-batch-bar">
      <span class="batch-info">已选 {{ selectedIds.size }} 项</span>
      <button class="rm-btn danger" @click="handleDeleteBatch">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        删除
      </button>
      <button class="rm-btn" @click="clearSelection">取消选择</button>
    </div>

    <!-- 右键菜单 -->
    <Teleport to="body">
      <div
        v-if="menu.visible"
        class="ctx-menu"
        :style="menuStyle"
        @contextmenu.prevent
      >
        <div class="ctx-item" @click="handleMenuAction('connect')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
          连接
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
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, triggerRef } from 'vue'
import ConnectionRow from './ConnectionRow.vue'
import { useSSHStore } from '@/stores/ssh'
import type { SSHConnection } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { toFriendlyMessage } from '@/api/client'

const emit = defineEmits<{
  connect: [id: string]
  edit: [conn: SSHConnection]
  'new-connection': []
}>()

// 新增：连接 loading 状态（从父组件传入）
const props = defineProps<{
  connecting: boolean
  pendingConnId: string
}>()

const store = useSSHStore()
const toast = useToastStore()
const { confirm } = useConfirm()

const searchText = ref('')
const debouncedSearch = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null
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

const filteredConnections = computed(() => {
  const q = debouncedSearch.value.toLowerCase().trim()
  let list = store.connections.slice()

  if (q) {
    list = list.filter(c => {
      const note = c.note || (c.tags || [])[0] || ''
      return c.name.toLowerCase().includes(q) ||
        c.host.toLowerCase().includes(q) ||
        c.username.toLowerCase().includes(q) ||
        note.toLowerCase().includes(q)
    })
  }

  switch (sortKey.value) {
    case 'recent':
      list.sort((a, b) => (b.lastConnectedAt || 0) - (a.lastConnectedAt || 0))
      break
    case 'name':
      list.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
      break
    case 'created':
      list.sort((a, b) => b.createdAt - a.createdAt)
      break
  }

  return list
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

// ─── 右键菜单 ─────────────────────────────
const menu = ref({ visible: false, x: 0, y: 0, conn: null as SSHConnection | null })
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

function showMenu(e: MouseEvent, conn: SSHConnection) {
  e.stopPropagation()
  e.preventDefault()
  const estimatedHeight = 150
  const viewportHeight = window.innerHeight
  const y = e.clientY + estimatedHeight > viewportHeight
    ? e.clientY - estimatedHeight
    : e.clientY
  menu.value = { visible: true, x: e.clientX, y, conn }
  // 在菜单显示后才注册 document mousedown 监听(bubble 阶段,普通事件流)
  if (!dismissBound) {
    document.addEventListener('mousedown', handleDocMouseDown)
    dismissBound = true
  }
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
  const conn = menu.value.conn
  menu.value.visible = false
  if (!conn) return
  switch (action) {
    case 'connect': handleConnect(conn.id); break
    case 'edit': handleEdit(conn); break
    case 'duplicate': handleDuplicate(conn.id); break
    case 'delete': handleDelete(conn.id); break
  }
}

onUnmounted(() => {
  // C33 修复：卸载时清理搜索防抖定时器（原实现残留，卸载后回调仍会写入 ref）
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
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
  background: var(--bg-base);
}

/* 头部 */
.rm-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 44px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.rm-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
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
  border-color: var(--accent-blue);
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
  --rm-grid-cols: 22px 12px minmax(110px, 1.1fr) minmax(160px, 1.4fr) 60px minmax(80px, 0.9fr) 84px 84px 96px;
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
  accent-color: var(--accent-blue);
  cursor: pointer;
}
.rm-row-header .cell-status { width: 100%; }
.rm-row-header .cell-time,
.rm-row-header .cell-created { text-align: right; }

/* 表头排序指示 */
.sortable {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 4px;
  transition: color var(--transition-fast);
}

.sortable:hover {
  color: var(--text-secondary);
}

.sortable.active {
  color: var(--accent-blue);
}

.sort-arrow {
  font-size: 10px;
  display: inline-block;
}

/* 空状态 */
.rm-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px 20px;
  color: var(--text-tertiary);
  text-align: center;
}

.rm-empty svg {
  color: var(--text-tertiary);
  opacity: 0.4;
  margin-bottom: 16px;
}

.empty-title {
  font-size: 14px;
  color: var(--text-secondary);
  font-weight: 500;
  margin-bottom: 4px;
}

.empty-desc {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-bottom: 16px;
}

/* 按钮 */
.rm-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font-sans);
  transition: all var(--transition-fast);
}

.rm-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.rm-btn.primary {
  background: var(--accent-blue);
  border-color: var(--accent-blue);
  color: #fff;
  font-weight: 500;
}

.rm-btn.primary:hover {
  filter: brightness(1.1);
  color: #fff;
}

.rm-btn.danger {
  color: var(--accent-red);
  border-color: rgba(244, 135, 113, 0.3);
}

.rm-btn.danger:hover {
  background: rgba(244, 135, 113, 0.1);
  color: var(--accent-red);
}

/* 批量栏 */
.rm-batch-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 40px;
  padding: 0 16px;
  border-top: 1px solid var(--border-default);
  background: var(--bg-elevated);
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
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
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
  background: rgba(244, 135, 113, 0.15);
  color: var(--accent-red);
}

.ctx-sep {
  height: 1px;
  margin: 4px 0;
  background: var(--border-subtle);
}
</style>
