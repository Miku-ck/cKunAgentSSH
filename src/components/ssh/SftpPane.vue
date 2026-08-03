<template>
  <div
    class="sftp-pane"
    :class="[`side-${side}`, { 'drag-target': isDragOver }]"
    @dragover.prevent="onDragOver"
    @dragleave.prevent="onDragLeave"
    @drop.prevent="onDrop"
    @contextmenu.prevent="onPaneContextMenu"
  >
    <!-- 拖拽悬停提示（仅在 isDragOver 时显示） -->
    <div v-if="isDragOver" class="drag-hint">
      {{ dragHintText }}
    </div>
    <!-- 标题栏 -->
    <div class="pane-header">
      <div class="pane-title">
        <svg v-if="side === 'local'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
        <span>{{ side === 'local' ? '本地' : '远程' }}</span>
        <span v-if="side === 'remote'" class="badge" :class="store.online ? 'online' : 'offline'">
          {{ store.online ? 'online' : '未连接' }}
        </span>
        <span v-else class="badge">Local</span>
      </div>
      <div class="pane-actions">
        <button class="pane-btn" title="上一级" :disabled="!canUp" @click="goUp">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
        <button class="pane-btn" title="刷新" @click="refresh">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        </button>
        <button class="pane-btn" title="新建文件夹" @click="onMkdir">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>
        </button>
      </div>
    </div>

    <!-- 面包屑 / 路径编辑栏 -->
    <div
      v-if="!editingPath"
      class="breadcrumb"
      @click="onBreadcrumbClick"
    >
      <span
        v-for="(seg, i) in breadcrumbs"
        :key="i"
        class="crumb-group"
      >
        <span v-if="i > 0" class="crumb-sep">›</span>
        <span class="crumb" :class="{ current: i === breadcrumbs.length - 1 }" @click.stop="navigateTo(i)">{{ seg || '/' }}</span>
      </span>
    </div>
    <div v-else class="breadcrumb breadcrumb-edit">
      <input
        ref="pathInputRef"
        v-model="pathInput"
        class="path-input"
        type="text"
        spellcheck="false"
        @keydown.enter="commitPath"
        @keydown.esc="cancelPath"
        @blur="onPathInputBlur"
      />
    </div>

    <!-- 文件列表 -->
    <div class="file-list" ref="listRef">
      <div class="file-list-head">
        <div>名称</div>
        <div>大小</div>
        <div>修改时间</div>
        <div>权限</div>
      </div>
      <div v-if="loading" class="pane-loading">加载中...</div>
      <div v-else-if="error" class="pane-error">{{ error }}</div>
      <div v-else-if="entries.length === 0" class="pane-empty-list">目录为空</div>
      <div
        v-for="entry in entries"
        :key="entry.path"
        class="file-row"
        :class="{ selected: isSelected(entry.path) }"
        draggable="true"
        @click="onRowClick(entry, $event)"
        @dblclick="onRowDblClick(entry)"
        @dragstart="onDragStart($event, entry)"
        @contextmenu.prevent.stop="onRowContextMenu($event, entry)"
      >
        <div class="file-name">
          <svg class="file-icon" :class="iconClass(entry)" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path v-if="entry.isDirectory" d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            <template v-else>
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/>
            </template>
          </svg>
          <span>{{ entry.name }}</span>
        </div>
        <div class="file-size">{{ entry.isDirectory ? '—' : formatSize(entry.size) }}</div>
        <div class="file-time">{{ formatTime(entry.modified) }}</div>
        <div class="file-perm">{{ entry.isDirectory ? 'd' + (permText(entry) || 'rwx') : permText(entry) || '-' }}</div>
      </div>
    </div>

    <!-- 占位空状态(远程未连接) -->
    <div v-if="side === 'remote' && !store.online" class="pane-empty-overlay">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
      <div class="empty-title">未连接远程服务器</div>
      <div class="empty-desc">点击右上角"连接远程"按钮,选择一个 SSH 连接后即可浏览远程目录、拖拽上传/下载文件。</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick } from 'vue'
import { useSftpStore } from '@/stores/sftp'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'

const props = defineProps<{ side: 'local' | 'remote' }>()
const store = useSftpStore()
const toast = useToastStore()

const isDragOver = ref(false)
const isOsFileDrag = ref(false)  // 是否为 OS 原生文件拖拽（外部文件管理器拖入）
const listRef = ref<HTMLElement | null>(null)
const editingPath = ref(false)
const pathInput = ref('')
const pathInputRef = ref<HTMLInputElement | null>(null)

// 拖拽悬停提示文案
const dragHintText = computed(() => {
  if (isOsFileDrag.value) {
    // OS 原生文件拖入：只在远程侧有意义（上传到远程）
    return props.side === 'remote' ? '松开以上传到当前远程目录' : '请拖到远程侧以上传'
  }
  // 应用内跨栏拖拽
  return props.side === 'local' ? '松开以下载到本地' : '松开以上传到远程'
})

const entries = computed<any[]>(() =>
  props.side === 'local' ? store.localEntries : store.remoteEntries
)
const loading = computed(() =>
  props.side === 'local' ? store.loadingLocal : store.loadingRemote
)
const error = computed(() =>
  props.side === 'local' ? store.localError : store.remoteError
)
const cwd = computed(() =>
  props.side === 'local' ? store.localCwd : store.remoteCwd
)

const canUp = computed(() => {
  if (props.side === 'remote') return store.online && store.remoteCwd && store.remoteCwd !== '/'
  return !!store.localCwd && store.localCwd !== '/'
})

const breadcrumbs = computed(() => {
  const path = cwd.value
  if (!path) return []
  if (props.side === 'remote') {
    // posix 路径
    if (path === '/') return ['/']
    const parts = path.split('/').filter(Boolean)
    return ['/'].concat(parts)
  } else {
    // win32 路径
    if (/^[a-z]:\\$/i.test(path)) return [path]
    return path.split(/[\\/]/).filter(Boolean)
  }
})

function isSelected(path: string): boolean {
  return props.side === 'local'
    ? store.selectedLocal.has(path)
    : store.selectedRemote.has(path)
}

function iconClass(entry: any): string {
  if (entry.isDirectory) return 'folder'
  const ext = entry.name.split('.').pop()?.toLowerCase() || ''
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'].includes(ext)) return 'image'
  if (['zip', 'gz', 'tar', 'rar', '7z'].includes(ext)) return 'archive'
  return 'text'
}

function permText(entry: any): string {
  if (props.side === 'remote' && entry.mode) return entry.mode
  return entry.isDirectory ? 'drwx' : '-rw'
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + ' ' + units[i]
}

function formatTime(ms: number): string {
  if (!ms) return '—'
  const d = new Date(ms)
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${m}-${day} ${hh}:${mm}`
}

// ─── 操作 ─────────────────────────────
function goUp(): void {
  if (props.side === 'local') store.localUp()
  else store.remoteUp()
}

function refresh(): void {
  if (props.side === 'local') store.localRefresh()
  else store.remoteRefresh()
}

async function navigateTo(idx: number): Promise<void> {
  if (props.side === 'remote') {
    const segs = breadcrumbs.value.slice(0, idx + 1)
    if (idx === 0) await store.loadRemote('/')
    else await store.loadRemote('/' + segs.slice(1).join('/'))
  } else {
    const sep = store.localCwd.includes('\\') ? '\\' : '/'
    const parts = store.localCwd.split(/[\\/]/).filter(Boolean)
    const target = parts.slice(0, idx + 1).join(sep)
    // Windows 盘符根目录补反斜杠
    const finalPath = /^[a-z]:$/i.test(target) ? target + '\\' : target
    await store.loadLocal(finalPath || '/')
  }
}

// 点击面包屑空白处进入路径编辑模式（远程未连接时不允许）
function onBreadcrumbClick(): void {
  if (props.side === 'remote' && !store.online) {
    toast.info('请先连接远程服务器')
    return
  }
  pathInput.value = cwd.value || ''
  editingPath.value = true
  nextTick(() => {
    pathInputRef.value?.focus()
    pathInputRef.value?.select()
  })
}

function cancelPath(): void {
  editingPath.value = false
  pathInput.value = ''
}

async function commitPath(): Promise<void> {
  const target = pathInput.value.trim()
  if (!target) {
    cancelPath()
    return
  }

  editingPath.value = false
  pathInput.value = ''

  if (props.side === 'local') {
    await store.loadLocal(target)
    if (store.localError) {
      toast.error(toFriendlyMessage(store.localError, `路径不存在或无法访问: ${target}`))
    }
  } else {
    await store.loadRemote(target)
    if (store.remoteError) {
      toast.error(toFriendlyMessage(store.remoteError, `路径不存在或无法访问: ${target}`))
    }
  }
}

// 失焦时：如果用户按了 Enter，commitPath 会先执行并把 editingPath 设为 false；
// 其他原因失焦（如点击外部）直接退出编辑，不提交，避免误跳转。
function onPathInputBlur(): void {
  if (editingPath.value) {
    cancelPath()
  }
}

async function onMkdir(): Promise<void> {
  const name = prompt('请输入文件夹名称')
  if (!name?.trim()) return
  try {
    if (props.side === 'local') await store.localMkdir(name.trim())
    else await store.remoteMkdir(name.trim())
    toast.success('已创建')
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '创建失败'))
  }
}

function onRowClick(entry: any, e: MouseEvent): void {
  if (e.ctrlKey || e.metaKey) {
    if (props.side === 'local') store.toggleSelectLocal(entry.path)
    else store.toggleSelectRemote(entry.path)
  } else {
    store.clearSelection()
    if (props.side === 'local') store.toggleSelectLocal(entry.path)
    else store.toggleSelectRemote(entry.path)
  }
}

async function onRowDblClick(entry: any): Promise<void> {
  if (entry.isDirectory) {
    if (props.side === 'local') await store.loadLocal(entry.path)
    else await store.loadRemote(entry.path)
  } else {
    emit('open-file', { side: props.side, entry })
  }
}

// ─── 拖拽 ─────────────────────────────
function onDragStart(e: DragEvent, entry: any): void {
  let paths: string[]
  if (isSelected(entry.path)) {
    paths = props.side === 'local'
      ? Array.from(store.selectedLocal)
      : Array.from(store.selectedRemote)
  } else {
    paths = [entry.path]
    // Bug 5 修复：只清理当前栏，不影响另一栏的选中状态
    store.clearSelection(props.side)
    if (props.side === 'local') store.toggleSelectLocal(entry.path)
    else store.toggleSelectRemote(entry.path)
  }
  const payload = JSON.stringify({ side: props.side, paths })
  e.dataTransfer?.setData('application/x-sftp-payload', payload)
  e.dataTransfer!.effectAllowed = 'copy'
}

function onDragOver(e: DragEvent): void {
  const types = e.dataTransfer?.types || []
  const hasSftpPayload = types.includes('application/x-sftp-payload')
  // Bug 4：支持 OS 原生文件拖拽（仅远程侧接受，意义为上传）
  const hasOsFiles = types.includes('Files') && props.side === 'remote' && store.online
  if (!hasSftpPayload && !hasOsFiles) return
  // 远程未连接时不接受
  if (props.side === 'remote' && !store.online) return
  e.dataTransfer!.dropEffect = 'copy'
  isOsFileDrag.value = hasOsFiles && !hasSftpPayload
  isDragOver.value = true
}

function onDragLeave(e: DragEvent): void {
  const related = e.relatedTarget as Node | null
  const pane = e.currentTarget as HTMLElement
  if (!related || !pane.contains(related)) {
    isDragOver.value = false
    isOsFileDrag.value = false
  }
}

function onDrop(e: DragEvent): void {
  isDragOver.value = false
  isOsFileDrag.value = false

  // 1. 优先处理应用内 sftp payload
  const raw = e.dataTransfer?.getData('application/x-sftp-payload')
  if (raw) {
    let payload: { side: 'local' | 'remote'; paths: string[] }
    try { payload = JSON.parse(raw) } catch { return }
    if (payload.side === props.side) return  // 同侧忽略
    emit('transfer', payload)
    return
  }

  // 2. Bug 4：处理 OS 原生文件拖拽（仅远程侧 + 在线时）
  if (props.side !== 'remote' || !store.online) return
  const files = e.dataTransfer?.files
  if (!files || files.length === 0) return
  // Electron 扩展：File 对象包含 path 属性指向本地真实路径
  const localPaths: string[] = []
  for (let i = 0; i < files.length; i++) {
    const fp = (files[i] as any).path as string | undefined
    if (fp) localPaths.push(fp)
  }
  if (localPaths.length === 0) {
    toast.warning('无法获取拖入文件的本地路径')
    return
  }
  emit('transfer', { side: 'local', paths: localPaths })
}

// ─── 右键菜单 ─────────────────────────
const emit = defineEmits<{
  transfer: [payload: { side: 'local' | 'remote'; paths: string[] }]
  'context-menu': [e: MouseEvent, entry: any | null, side: 'local' | 'remote']
  'open-file': [payload: { side: 'local' | 'remote'; entry: any }]
}>()

function onPaneContextMenu(e: MouseEvent): void {
  emit('context-menu', e, null, props.side)
}

function onRowContextMenu(e: MouseEvent, entry: any): void {
  emit('context-menu', e, entry, props.side)
}
</script>

<style scoped>
.sftp-pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  position: relative;
  background: var(--bg-base);
}
.sftp-pane.side-local { border-right: 1px solid var(--divider-primary); }

.pane-header {
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.pane-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--text-secondary); font-weight: 500;
}
.pane-title svg { width: 13px; height: 13px; }
.badge {
  padding: 1px 6px; font-size: 10px; border-radius: 8px;
  background: var(--bg-active); color: var(--text-tertiary);
}
.badge.online { color: var(--accent-green); background: rgba(78,201,176,0.12); }
.badge.offline { color: var(--text-tertiary); background: var(--bg-hover); }

.pane-actions { display: flex; gap: 1px; }
.pane-btn {
  width: 22px; height: 22px;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: none; color: var(--text-tertiary);
  cursor: pointer; border-radius: 3px;
}
.pane-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-secondary); }
.pane-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.pane-btn svg { width: 12px; height: 12px; }

.breadcrumb {
  height: 28px;
  display: flex;
  align-items: center;
  padding: 0 10px; gap: 2px;
  background: var(--bg-base);
  border-bottom: 1px solid var(--border-subtle);
  overflow-x: auto; flex-shrink: 0;
  font-family: var(--font-mono); font-size: 11px;
  cursor: text;
}
.breadcrumb::-webkit-scrollbar { display: none; }
.breadcrumb-edit { cursor: default; padding: 0 6px; }
.crumb-group { display: flex; align-items: center; gap: 2px; }
.crumb { color: var(--text-tertiary); cursor: pointer; white-space: nowrap; padding: 2px 4px; border-radius: 3px; }
.crumb:hover { color: var(--text-secondary); background: var(--bg-hover); }
.crumb.current { color: var(--accent-cyan); }
.crumb-sep { color: var(--text-tertiary); opacity: 0.5; }
.path-input {
  width: 100%;
  height: 22px;
  padding: 0 6px;
  background: var(--bg-elevated);
  border: 1px solid var(--accent-blue);
  border-radius: 3px;
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 12px;
  outline: none;
}

.file-list {
  flex: 1; overflow-y: auto; font-size: 12px;
}
.file-list-head {
  display: grid;
  grid-template-columns: 1fr 80px 130px 90px;
  height: 26px; align-items: center;
  padding: 0 10px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-tertiary);
  font-size: 11px; font-weight: 500;
  position: sticky; top: 0; z-index: 1;
}
.file-row {
  display: grid;
  grid-template-columns: 1fr 80px 130px 90px;
  align-items: center;
  padding: 0 10px; height: 28px;
  cursor: default;
  border-bottom: 1px solid transparent;
}
.file-row:hover { background: var(--bg-hover); }
.file-row.selected { background: rgba(0,120,212,0.15); border-color: rgba(0,120,212,0.3); }

.file-name { display: flex; align-items: center; gap: 8px; overflow: hidden; }
.file-name span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-icon { width: 14px; height: 14px; flex-shrink: 0; }
.file-icon.folder { color: var(--accent-orange); }
.file-icon.text { color: var(--accent-cyan); }
.file-icon.image { color: var(--accent-purple); }
.file-icon.archive { color: var(--accent-red); }
.file-size, .file-time, .file-perm {
  color: var(--text-tertiary); font-family: var(--font-mono); font-size: 11px;
}

.pane-loading, .pane-error, .pane-empty-list {
  padding: 16px; text-align: center;
  font-size: 12px; color: var(--text-tertiary);
}
.pane-error { color: var(--accent-red); }

.pane-empty-overlay {
  position: absolute;
  inset: 32px 0 0 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  background: var(--bg-base);
  padding: 24px; text-align: center;
  color: var(--text-tertiary); gap: 10px;
  z-index: 2;
}
.pane-empty-overlay svg { width: 36px; height: 36px; opacity: 0.4; }
.pane-empty-overlay .empty-title { font-size: 13px; color: var(--text-secondary); }
.pane-empty-overlay .empty-desc { font-size: 11px; line-height: 1.6; max-width: 240px; }

/* 拖拽目标视觉反馈 */
.sftp-pane.drag-target::after {
  content: '';
  position: absolute; inset: 0;
  background: rgba(78,201,176,0.08);
  border: 2px dashed var(--accent-green);
  pointer-events: none; z-index: 5;
}
.sftp-pane.drag-target .drag-hint {
  position: absolute; top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  background: var(--bg-active);
  border: 1px solid var(--accent-green);
  color: var(--accent-green);
  padding: 8px 14px; border-radius: 6px;
  font-size: 12px; font-weight: 500;
  z-index: 6; pointer-events: none;
  box-shadow: var(--shadow-lg);
}
</style>
