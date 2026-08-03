<template>
  <div class="sftp-view">
    <!-- 会话状态栏 -->
    <div class="sftp-session-bar">
      <div class="session-left">
        <div class="session-dot" :class="store.online ? 'online' : 'offline'"></div>
        <span class="session-name">{{ store.online ? store.connectionName : '未连接远程' }}</span>
        <span v-if="store.online" class="session-host">{{ store.connectionHost }}</span>
        <span v-else class="session-host">仅本地模式</span>
      </div>
      <div class="session-right">
        <button v-if="!store.online" class="session-btn connect" @click="showConnPicker = true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
          连接远程
        </button>
        <template v-else>
          <button class="session-btn" @click="showConnPicker = true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            切换
          </button>
          <button class="session-btn" @click="handleDisconnect">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
            断开
          </button>
        </template>
      </div>
    </div>

    <!-- 双栏 -->
    <div class="sftp-panes">
      <SftpPane
        side="local"
        @transfer="onTransfer"
        @context-menu="onContextMenu"
        @open-file="onOpenFile"
      />
      <SftpPane
        side="remote"
        @transfer="onTransfer"
        @context-menu="onContextMenu"
        @open-file="onOpenFile"
      />
    </div>

    <!-- 底部传输队列 -->
    <TransferQueue />

    <!-- 连接选择模态 -->
    <div v-if="showConnPicker" class="conn-overlay" @click.self="showConnPicker = false">
      <div class="conn-dialog">
        <div class="conn-dialog-header">
          <span>选择 SSH 连接</span>
          <button class="conn-close" @click="showConnPicker = false">×</button>
        </div>
        <div class="conn-list">
          <div v-if="sshStore.connections.length === 0" class="conn-empty">
            暂无 SSH 连接配置,请先在"远程管理"中新建连接
          </div>
          <div
            v-for="conn in sshStore.connections"
            :key="conn.id"
            class="conn-item"
            :class="{ disabled: connecting }"
            @click="handleConnect(conn)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
            <div class="conn-item-info">
              <div class="conn-item-name">{{ conn.name }}</div>
              <div class="conn-item-host">{{ conn.username }}@{{ conn.host }}:{{ conn.port }}</div>
            </div>
            <svg v-if="connecting && pendingConnId === conn.id" class="spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
          </div>
        </div>
      </div>
    </div>

    <!-- 右键菜单 -->
    <div
      v-if="ctxMenu.visible"
      class="ctx-menu"
      :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
      @click="ctxMenu.visible = false"
      @contextmenu.prevent
    >
      <div class="ctx-item" @click="handleCtxAction('open')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        打开
      </div>
      <div v-if="ctxMenu.entry" class="ctx-item" @click="handleCtxAction('transfer')">
        <svg v-if="ctxMenu.side === 'local'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
        {{ ctxMenu.side === 'local' ? '上传' : '下载' }}
      </div>
      <div class="ctx-sep"></div>
      <div class="ctx-item" @click="handleCtxAction('newFolder')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>
        新建文件夹
      </div>
      <template v-if="ctxMenu.entry">
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="handleCtxAction('zip')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg>
          压缩
        </div>
        <div v-if="isZipFile(ctxMenu.entry)" class="ctx-item" @click="handleCtxAction('unzip')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/><path d="M12 12v5" stroke-dasharray="2 2"/></svg>
          解压
        </div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="handleCtxAction('copyPath')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          复制路径
        </div>
        <div class="ctx-item" @click="handleCtxAction('rename')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          重命名
        </div>
        <div class="ctx-item danger" @click="handleCtxAction('delete')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          删除
        </div>
      </template>
    </div>

    <!-- 重命名/新建对话框 -->
    <div v-if="renameDialog.visible" class="rename-overlay" @click.self="renameDialog.visible = false">
      <div class="rename-dialog">
        <div class="rename-title">{{ renameDialog.isNew ? '新建文件夹' : '重命名' }}</div>
        <input
          ref="renameInput"
          v-model="renameDialog.name"
          class="rename-input"
          @keydown.enter="confirmRename"
          @keydown.escape="renameDialog.visible = false"
        />
        <div class="rename-actions">
          <button class="rename-btn cancel" @click="renameDialog.visible = false">取消</button>
          <button class="rename-btn confirm" @click="confirmRename">确定</button>
        </div>
      </div>
    </div>

    <!-- 弹窗文件编辑器 -->
    <SftpFileEditor
      :visible="fileEditor.visible"
      :side="fileEditor.side"
      :path="fileEditor.path"
      :name="fileEditor.name"
      :ssh-session-id="store.sshSessionId"
      @close="fileEditor.visible = false"
      @saved="onFileSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, nextTick } from 'vue'
import SftpPane from './SftpPane.vue'
import SftpFileEditor from './SftpFileEditor.vue'
import TransferQueue from './TransferQueue.vue'
import { useSftpStore } from '@/stores/sftp'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { isPreviewable } from '@/utils/language'
import { toFriendlyMessage } from '@/api/client'

const store = useSftpStore()
const sshStore = useSSHStore()
const toast = useToastStore()
const { confirm } = useConfirm()

const showConnPicker = ref(false)
const connecting = ref(false)
const pendingConnId = ref('')

// ─── 进度订阅 ─────────────────────────
let unsubProgress: (() => void) | null = null
let unsubStalled: (() => void) | null = null
let unsubClosed: (() => void) | null = null
let unsubSshClose: (() => void) | null = null

onMounted(() => {
  const api = window.electronAPI!
  unsubProgress = api.onSftpProgress((data) => {
    store.applyProgress(data)
  })
  unsubStalled = api.onSftpStalled((data) => {
    const item = store.transfers.find(t => t.id === data.id)
    const name = item?.name || '文件'
    toast.warning(`"${name}" 传输停滞，正在尝试自动续传…`)
  })
  unsubClosed = api.onSftpClosed((data) => {
    if (data.id === store.sshSessionId) {
      store.disconnect()
      toast.warning('远程 SFTP 连接已断开')
    }
  })
  unsubSshClose = api.onSshClose((data) => {
    if (data.id === store.sshSessionId) {
      store.disconnect()
      toast.warning('远程 SFTP 连接已断开')
    }
  })
  document.addEventListener('click', dismissCtxMenu)
  if (!store.localCwd) store.initLocalHome()
})

onUnmounted(() => {
  unsubProgress?.()
  unsubStalled?.()
  unsubClosed?.()
  unsubSshClose?.()
  document.removeEventListener('click', dismissCtxMenu)
  showConnPicker.value = false
  ctxMenu.visible = false
  renameDialog.visible = false
})

// ─── 连接管理 ─────────────────────────
async function handleConnect(conn: SSHConnectionRecord): Promise<void> {
  if (connecting.value) return
  connecting.value = true
  pendingConnId.value = conn.id
  try {
    await store.connect(conn)
    showConnPicker.value = false
    toast.success(`已连接 ${conn.name}`)
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '连接失败'))
  } finally {
    connecting.value = false
    pendingConnId.value = ''
  }
}

async function handleDisconnect(): Promise<void> {
  const ok = await confirm({
    title: '断开 SFTP 连接',
    message: `确认断开与「${store.connectionName}」的 SFTP 连接?`,
    confirmText: '断开',
    danger: true,
  })
  if (ok) {
    await store.disconnect()
    toast.success('已断开')
  }
}

// ─── 跨栏传输(由 SftpPane drop 触发) ───
function onTransfer(payload: { side: 'local' | 'remote'; paths: string[] }): void {
  if (payload.side === 'local') {
    // 本地 → 远程: 上传
    store.uploadFiles(payload.paths)
  } else {
    // 远程 → 本地: 下载
    store.downloadFiles(payload.paths)
  }
}

// ─── 右键菜单 ─────────────────────────
interface AnyEntry { name: string; path: string; isDirectory: boolean }
const ctxMenu = reactive({
  visible: false,
  x: 0, y: 0,
  side: '' as 'local' | 'remote',
  entry: null as AnyEntry | null,
})

function onContextMenu(e: MouseEvent, entry: AnyEntry | null, side: 'local' | 'remote'): void {
  const estimatedHeight = 200
  const viewportHeight = window.innerHeight
  const y = e.clientY + estimatedHeight > viewportHeight
    ? e.clientY - estimatedHeight
    : e.clientY
  ctxMenu.visible = true
  ctxMenu.x = e.clientX
  ctxMenu.y = y
  ctxMenu.entry = entry
  ctxMenu.side = side
}

async function handleCtxAction(action: string): Promise<void> {
  const entry = ctxMenu.entry
  const side = ctxMenu.side
  ctxMenu.visible = false

  if (action === 'open') {
    if (entry?.isDirectory) {
      if (side === 'local') await store.loadLocal(entry.path)
      else await store.loadRemote(entry.path)
    } else if (entry && isPreviewable(entry.name)) {
      openFileEditor(side, entry)
    }
    return
  }

  if (action === 'transfer' && entry) {
    // 收集选中项路径（如果当前项在选中集合中，传输所有选中项；否则只传输当前项）
    const paths = getCtxPaths(side, entry)
    if (side === 'local') store.uploadFiles(paths)
    else store.downloadFiles(paths)
    return
  }

  if (action === 'newFolder') {
    openRenameDialog(true, '', side)
    return
  }

  if (action === 'zip' && entry) {
    const paths = getCtxPaths(side, entry)
    const defaultName = paths.length === 1
      ? entry.name + '.zip'
      : 'archive.zip'
    const zipName = prompt('请输入压缩包文件名', defaultName)
    if (!zipName?.trim()) return
    const finalName = zipName.trim()
    // Bug 1 修复：检查目标 zip 是否已存在，提示用户是否覆盖
    const targetPath = side === 'local'
      ? await store.joinLocalPath(finalName)
      : store.joinRemotePath(finalName)
    const exists = side === 'local'
      ? await store.localPathExists(targetPath)
      : await store.remotePathExists(targetPath)
    if (exists) {
      const ok = await confirm({
        title: '文件已存在',
        message: `「${finalName}」已存在，是否覆盖？`,
        confirmText: '覆盖',
        danger: true,
      })
      if (!ok) return
    }
    try {
      if (side === 'local') await store.zipLocal(paths, finalName)
      else await store.zipRemote(paths, finalName)
    } catch { /* toast 已在 store 内弹出 */ }
    return
  }

  if (action === 'unzip' && entry) {
    const destName = entry.name.replace(/\.zip$/i, '') || 'unzipped'
    // Bug 1 修复：检查解压目标目录是否已存在，提示用户是否继续（内部同名文件会被覆盖）
    const targetPath = side === 'local'
      ? await store.joinLocalPath(destName)
      : store.joinRemotePath(destName)
    const exists = side === 'local'
      ? await store.localPathExists(targetPath)
      : await store.remotePathExists(targetPath)
    if (exists) {
      const ok = await confirm({
        title: '目录已存在',
        message: `解压目标「${destName}」已存在，内部同名文件将被覆盖，是否继续？`,
        confirmText: '继续',
        danger: true,
      })
      if (!ok) return
    }
    try {
      if (side === 'local') await store.unzipLocal(entry.path, destName)
      else await store.unzipRemote(entry.path, destName)
    } catch { /* toast 已在 store 内弹出 */ }
    return
  }

  if (action === 'copyPath' && entry) {
    try {
      await navigator.clipboard.writeText(entry.path)
      toast.success('已复制路径')
    } catch {
      toast.error('复制失败')
    }
    return
  }

  if (action === 'rename' && entry) {
    openRenameDialog(false, entry.name, side, entry.path)
    return
  }

  if (action === 'delete' && entry) {
    const paths = getCtxPaths(side, entry)
    const ok = await confirm({
      title: '删除',
      message: paths.length === 1
        ? `确认删除「${entry.name}」?\n此操作不可撤销。`
        : `确认删除 ${paths.length} 项?\n此操作不可撤销。`,
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    try {
      for (const p of paths) {
        if (side === 'local') await store.localDelete(p)
        else await store.remoteDelete(p)
      }
      toast.success('已删除')
    } catch (err: any) {
      toast.error(toFriendlyMessage(err, '删除失败'))
    }
  }
}

/** 判断是否为 zip 文件（用于右键菜单显示"解压"选项） */
function isZipFile(entry: AnyEntry): boolean {
  return !entry.isDirectory && /\.zip$/i.test(entry.name)
}

/**
 * 获取右键菜单作用对象路径列表：
 * - 若 entry 已被选中，则返回该栏所有选中项路径（支持批量操作）
 * - 否则只返回 entry 自身路径
 */
function getCtxPaths(side: 'local' | 'remote', entry: AnyEntry): string[] {
  const selected = side === 'local' ? store.selectedLocal : store.selectedRemote
  if (selected.has(entry.path)) {
    return Array.from(selected)
  }
  return [entry.path]
}

// ─── 重命名/新建 ──────────────────────
const renameDialog = reactive({
  visible: false,
  name: '',
  isNew: false,
  side: '' as 'local' | 'remote',
  oldPath: '',
})
const renameInput = ref<HTMLInputElement | null>(null)

function openRenameDialog(isNew: boolean, name: string, side: 'local' | 'remote', oldPath = ''): void {
  renameDialog.isNew = isNew
  renameDialog.name = name
  renameDialog.side = side
  renameDialog.oldPath = oldPath
  renameDialog.visible = true
  nextTick(() => {
    renameInput.value?.focus()
    if (!isNew) {
      const dot = name.lastIndexOf('.')
      renameInput.value?.setSelectionRange(0, dot > 0 ? dot : name.length)
    }
  })
}

async function confirmRename(): Promise<void> {
  const name = renameDialog.name.trim()
  if (!name) return
  try {
    if (renameDialog.isNew) {
      if (renameDialog.side === 'local') await store.localMkdir(name)
      else await store.remoteMkdir(name)
    } else {
      if (renameDialog.side === 'local') await store.localRename(renameDialog.oldPath, name)
      else await store.remoteRename(renameDialog.oldPath, name)
    }
    toast.success(renameDialog.isNew ? '已创建' : '已重命名')
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '操作失败'))
  }
  renameDialog.visible = false
}

// ─── 关闭右键菜单 ─────────────────────
function dismissCtxMenu(): void {
  ctxMenu.visible = false
}

// ─── 弹窗编辑器 ───────────────────────
const fileEditor = reactive({
  visible: false,
  side: 'local' as 'local' | 'remote',
  path: '',
  name: '',
})

function openFileEditor(side: 'local' | 'remote', entry: AnyEntry): void {
  fileEditor.side = side
  fileEditor.path = entry.path
  fileEditor.name = entry.name
  fileEditor.visible = true
}

function onOpenFile(payload: { side: 'local' | 'remote'; entry: AnyEntry }): void {
  openFileEditor(payload.side, payload.entry)
}

async function onFileSaved(): Promise<void> {
  if (fileEditor.side === 'local') await store.localRefresh()
  else await store.remoteRefresh()
}
</script>

<style scoped>
.sftp-view {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-base);
}

/* 会话状态栏 */
.sftp-session-bar {
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.session-left { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.session-dot {
  width: 8px; height: 8px; border-radius: 50%;
}
.session-dot.online { background: var(--accent-green); box-shadow: 0 0 6px rgba(78,201,176,0.6); }
.session-dot.offline { background: var(--text-tertiary); }
.session-name { color: var(--text-primary); font-weight: 500; }
.session-host { color: var(--text-tertiary); font-family: var(--font-mono); font-size: 11px; }
.session-right { display: flex; gap: 6px; }
.session-btn {
  height: 26px; padding: 0 10px; font-size: 12px;
  background: transparent; border: 1px solid var(--border-default);
  color: var(--text-secondary); border-radius: 4px; cursor: pointer;
  display: flex; align-items: center; gap: 5px;
  font-family: var(--font-sans);
}
.session-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.session-btn svg { width: 12px; height: 12px; }
.session-btn.connect { color: var(--accent-green); border-color: rgba(78,201,176,0.4); }
.session-btn.connect:hover { background: rgba(78,201,176,0.1); }

/* 双栏 */
.sftp-panes {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  min-height: 0;
}

/* 连接选择模态 */
.conn-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.conn-dialog {
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  width: 420px;
  max-height: 60vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
}
.conn-dialog-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 13px; font-weight: 500; color: var(--text-primary);
}
.conn-close {
  background: transparent; border: none; color: var(--text-tertiary);
  font-size: 20px; cursor: pointer; padding: 0; line-height: 1;
}
.conn-close:hover { color: var(--text-primary); }
.conn-list { padding: 6px; overflow-y: auto; }
.conn-empty {
  padding: 24px 16px; text-align: center;
  font-size: 12px; color: var(--text-tertiary);
}
.conn-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-radius: 4px; cursor: pointer;
  color: var(--text-tertiary);
}
.conn-item:hover { background: var(--bg-hover); color: var(--text-secondary); }
.conn-item.disabled { opacity: 0.5; cursor: not-allowed; }
.conn-item-info { flex: 1; min-width: 0; }
.conn-item-name { font-size: 13px; color: var(--text-primary); }
.conn-item-host { font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono); }
.conn-item .spinner {
  width: 12px; height: 12px;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* 右键菜单 */
.ctx-menu {
  position: fixed;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: var(--shadow-lg);
  padding: 4px;
  min-width: 160px;
  z-index: 9999;
  background: var(--bg-elevated);
}
.ctx-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px;
  font-size: 12px; color: var(--text-secondary);
  cursor: pointer; border-radius: 4px;
}
.ctx-item:hover { background: var(--bg-hover); color: var(--text-primary); }
.ctx-item.danger:hover { background: rgba(248, 81, 73, 0.15); color: var(--accent-red); }
.ctx-sep { height: 1px; margin: 4px 0; background: var(--border-subtle); }

/* 重命名/新建对话框 */
.rename-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.5);
  display: flex; align-items: center; justify-content: center;
  z-index: 1001;
}
.rename-dialog {
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 20px;
  width: 320px;
  box-shadow: var(--shadow-lg);
}
.rename-title {
  font-size: 14px; font-weight: 600;
  color: var(--text-primary); margin-bottom: 12px;
}
.rename-input {
  width: 100%; height: 32px; padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: 13px; outline: none;
  font-family: var(--font-sans);
}
.rename-input:focus { border-color: var(--accent-blue); }
.rename-actions {
  display: flex; justify-content: flex-end;
  gap: 8px; margin-top: 16px;
}
.rename-btn {
  height: 30px; padding: 0 14px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  font-size: 12px; cursor: pointer;
  font-family: var(--font-sans);
}
.rename-btn.cancel { background: transparent; color: var(--text-secondary); }
.rename-btn.cancel:hover { background: var(--bg-hover); }
.rename-btn.confirm { background: var(--accent-blue); color: #fff; border-color: var(--accent-blue); }
.rename-btn.confirm:hover { opacity: 0.9; }
</style>
