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
        <svg v-if="side === 'local'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="12" rx="2"/><line x1="2" y1="20" x2="22" y2="20"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
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
        <button class="pane-btn" title="新建文件夹" @click="openMkdirDialog">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>
        </button>
      </div>
    </div>

    <!-- 面包屑 / 路径编辑栏 -->
    <div class="breadcrumb-bar">
      <!-- 盘符切换按钮（仅本地侧 + Windows） -->
      <button
        v-if="side === 'local' && isWindows"
        class="drive-switcher"
        :class="{ active: showDriveDropdown }"
        title="切换磁盘"
        @click.stop="toggleDriveDropdown"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
      </button>
      <!-- 盘符下拉面板 -->
      <div v-if="showDriveDropdown" class="drive-dropdown" @click.stop>
        <div class="drive-dropdown-title">选择磁盘</div>
        <div
          v-for="drive in drives"
          :key="drive"
          class="drive-option"
          :class="{ active: isCurrentDrive(drive) }"
          @click="navigateToDrive(drive)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
          <span>{{drive}}</span>
        </div>
      </div>
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
        v-for="entry in displayEntries"
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
        <div class="file-size">{{ entry.isDirectory ? '—' : formatBytes(entry.size) }}</div>
        <div class="file-time">{{ formatTime(entry.modified) }}</div>
        <!-- permText 的两个分支都已自带类型位（远端 modeToString 的 "drwxr-xr-x"、
             本地兜底的 'drwx'/'-rw'），这里再拼一个 'd' 会显示成 ddrwxr-xr-x -->
        <div class="file-perm">{{ permText(entry) || '-' }}</div>
      </div>
      <!-- B16 修复：全量 v-for 无虚拟化 — 超大目录只渲染前 2000 行，超出给提示 -->
      <div v-if="entries.length > RENDER_LIMIT" class="pane-list-limit">
        仅显示前 {{ RENDER_LIMIT }} 个条目，共 {{ entries.length }} 个（请用筛选或进入子目录）
      </div>
    </div>

    <!-- 新建文件夹对话框（F-2 修复：替代 Electron 下恒返回 null 的 window.prompt） -->
    <div v-if="showMkdirDialog" class="mkdir-overlay" @click.self="cancelMkdir">
      <div class="mkdir-dialog">
        <div class="mkdir-title">新建文件夹</div>
        <input
          ref="mkdirInputRef"
          v-model="mkdirName"
          class="mkdir-input"
          placeholder="请输入文件夹名称"
          @keydown.enter="confirmMkdir"
          @keydown.esc="cancelMkdir"
        />
        <div class="mkdir-actions">
          <button class="mkdir-btn cancel" :disabled="mkdirBusy" @click="cancelMkdir">取消</button>
          <button class="mkdir-btn confirm" :disabled="mkdirBusy || !mkdirName.trim()" @click="confirmMkdir">
            {{ mkdirBusy ? '创建中…' : '确定' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 占位空状态(远程未连接) -->
    <div v-if="side === 'remote' && !store.online" class="pane-empty-overlay">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
      <div class="empty-title">未连接远程服务器</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { formatBytes } from '@/utils/format'
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
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

// ─── 盘符切换 ─────────────────────────
const isWindows = computed(() => {
  // 通过 userAgent 判断 Windows 平台（Electron 渲染进程）
  return typeof navigator !== 'undefined' && /Windows/i.test(navigator.userAgent)
})
const drives = ref<string[]>([])
const showDriveDropdown = ref(false)

async function toggleDriveDropdown(): Promise<void> {
  // C31 修复：先同步翻转可见性 — 原实现 await getDrives 在途时
  // showDriveDropdown 仍为 false，第二下"关闭"点击走不到同步关闭分支
  if (showDriveDropdown.value) {
    showDriveDropdown.value = false
    return
  }
  showDriveDropdown.value = true
  // 加载盘符列表
  drives.value = await store.getDrives()
}

function isCurrentDrive(drive: string): boolean {
  return store.localCwd.toUpperCase().startsWith(drive.toUpperCase())
}

async function navigateToDrive(drive: string): Promise<void> {
  showDriveDropdown.value = false
  await store.loadLocal(drive)
}

function closeDriveDropdown(): void {
  showDriveDropdown.value = false
}

// 点击外部关闭盘符下拉
onMounted(() => {
  document.addEventListener('click', closeDriveDropdown)
})
onUnmounted(() => {
  document.removeEventListener('click', closeDriveDropdown)
})

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

// B16 修复：渲染上限（不引第三方虚拟滚动库），超大目录只显示前 N 行
const RENDER_LIMIT = 2000
const displayEntries = computed<any[]>(() => entries.value.slice(0, RENDER_LIMIT))
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
    // 审查R2修复：split+filter(Boolean) 丢掉 POSIX 绝对路径的前导 /，
    // 面包屑点「home」会导航到相对路径 'home'（与 sftp.ts localParent 同款问题）。
    // 绝对路径（以根分隔符开头）导航时必须补回根分隔符；Windows 盘符路径不带。
    const root = store.localCwd.startsWith(sep) ? sep : ''
    const target = root + parts.slice(0, idx + 1).join(sep)
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

// ─── 新建文件夹对话框（F-2 修复） ──────────────────
// Electron 的 window.prompt() 恒返回 null，"新建文件夹"按钮完全无反应；
// 改为组件内小模态输入框（Enter 确认 / Esc 取消）
const showMkdirDialog = ref(false)
const mkdirName = ref('')
const mkdirBusy = ref(false)
const mkdirInputRef = ref<HTMLInputElement | null>(null)

function openMkdirDialog(): void {
  if (props.side === 'remote' && !store.online) {
    toast.info('请先连接远程服务器')
    return
  }
  mkdirName.value = ''
  showMkdirDialog.value = true
  nextTick(() => {
    mkdirInputRef.value?.focus()
  })
}

async function confirmMkdir(): Promise<void> {
  const name = mkdirName.value.trim()
  if (!name || mkdirBusy.value) return
  mkdirBusy.value = true
  try {
    if (props.side === 'local') await store.localMkdir(name)
    else await store.remoteMkdir(name)
    toast.success('已创建')
    showMkdirDialog.value = false
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '创建失败'))
  } finally {
    mkdirBusy.value = false
  }
}

function cancelMkdir(): void {
  if (mkdirBusy.value) return
  showMkdirDialog.value = false
}

function onRowClick(entry: any, e: MouseEvent): void {
  if (e.ctrlKey || e.metaKey) {
    if (props.side === 'local') store.toggleSelectLocal(entry.path)
    else store.toggleSelectRemote(entry.path)
  } else {
    // 只清理当前栏：不带参数会连带清掉另一栏的选中（与下方 onDragStart 的 Bug 5 修复不一致），
    // 之后右键的删除/上传/压缩就只作用在单行上而非整批
    store.clearSelection(props.side)
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
  // S5-07 修复：Electron 32 起 File.path 被移除，原实现读到的恒为 undefined
  // —— 拖任意文件进远程面板都只弹「无法获取拖入文件的本地路径」，这个功能从
  // 升级那天起就是死的。改用 preload 暴露的 webUtils.getPathForFile（必须在
  // drop 事件同步上下文里取，返回值空串表示拿不到路径）
  const getter = window.electronAPI?.getPathForFile
  if (!getter) {
    toast.warning('当前版本不支持读取拖入文件的本地路径')
    return
  }
  const localPaths: string[] = []
  for (let i = 0; i < files.length; i++) {
    const fp = getter(files[i])
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
  background: var(--bg-base);
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
.badge.online { color: var(--accent-green); background: color-mix(in srgb, var(--accent-green) 12%, transparent); }
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

.breadcrumb-bar {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border-subtle);
  position: relative;
}
.drive-switcher {
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-base);
  border: none;
  border-right: 1px solid var(--border-subtle);
  color: var(--text-tertiary);
  cursor: pointer;
  flex-shrink: 0;
}
.drive-switcher:hover { background: var(--bg-hover); color: var(--text-secondary); }
.drive-switcher.active { background: var(--bg-hover); color: var(--accent-cyan); }
.drive-switcher svg { width: 14px; height: 14px; }

.drive-dropdown {
  position: absolute;
  top: 29px;
  left: 0;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: var(--shadow-lg);
  padding: 4px;
  min-width: 120px;
  z-index: 100;
  animation: ctx-fade-in 0.1s ease-out;
}
.drive-dropdown-title {
  font-size: 10px;
  color: var(--text-tertiary);
  padding: 4px 8px;
  font-weight: 600;
}
.drive-option {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 4px;
}
.drive-option:hover { background: var(--bg-hover); color: var(--text-primary); }
.drive-option.active { color: var(--accent-cyan); background: color-mix(in srgb, var(--accent-primary) 10%, transparent); }
.drive-option svg { flex-shrink: 0; }

.breadcrumb {
  height: 28px;
  display: flex;
  align-items: center;
  padding: 0 10px; gap: 2px;
  background: var(--bg-base);
  overflow-x: auto; flex: 1;
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
  background: var(--bg-input);
  border: 1px solid var(--accent-primary);
  border-radius: 3px;
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 12px;
  outline: none;
}

.file-list {
  flex: 1; overflow-y: auto; font-size: 12px;
  /* 纵向 flex：加载/错误/空目录提示撑满剩余空间双向居中 */
  display: flex; flex-direction: column;
}
.file-list-head {
  display: grid;
  grid-template-columns: 1fr 80px 130px 90px;
  height: 26px; align-items: center;
  padding: 0 10px;
  background: var(--bg-base);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-tertiary);
  font-size: 11px; font-weight: 500;
  position: sticky; top: 0; z-index: 1;
  text-align: left;
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
.file-row.selected { background: color-mix(in srgb, var(--accent-primary) 15%, transparent); border-color: color-mix(in srgb, var(--accent-primary) 30%, transparent); }

.file-row > div {
  text-align: left;
}

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
  flex: 1;
  display: flex; align-items: center; justify-content: center;
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

/* 文件列表渲染上限提示 */
.pane-list-limit {
  padding: 10px 12px;
  font-size: 11px;
  color: var(--text-tertiary);
  text-align: center;
  border-top: 1px dashed var(--border-subtle);
}

/* 新建文件夹对话框（风格与 SftpView 的 rename 对话框一致） */
.mkdir-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 1001;
}
.mkdir-dialog {
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 20px;
  width: 320px;
  box-shadow: var(--shadow-lg);
}
.mkdir-title {
  font-size: 14px; font-weight: 600;
  color: var(--text-primary); margin-bottom: 12px;
}
.mkdir-input {
  width: 100%; height: 32px; padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: 13px; outline: none;
  font-family: var(--font-sans);
}
.mkdir-input:focus { border-color: var(--accent-primary); }
.mkdir-actions {
  display: flex; justify-content: flex-end;
  gap: 8px; margin-top: 16px;
}
.mkdir-btn {
  height: 30px; padding: 0 14px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  font-size: 12px; cursor: pointer;
  font-family: var(--font-sans);
}
.mkdir-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.mkdir-btn.cancel { background: transparent; color: var(--text-secondary); }
.mkdir-btn.cancel:hover:not(:disabled) { background: var(--bg-hover); }
.mkdir-btn.confirm { background: var(--accent-primary); color: var(--accent-on); border-color: var(--accent-primary); }
.mkdir-btn.confirm:hover:not(:disabled) { opacity: 0.9; }

/* 拖拽目标视觉反馈 */
.sftp-pane.drag-target::after {
  content: '';
  position: absolute; inset: 0;
  background: color-mix(in srgb, var(--accent-green) 8%, transparent);
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

@keyframes ctx-fade-in {
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
}
</style>
