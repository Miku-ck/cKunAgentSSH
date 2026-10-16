<template>
  <div class="sftp-view">
    <!-- 会话状态栏 -->
    <div class="sftp-session-bar">
      <div class="session-left">
        <div class="session-dot" :class="store.online ? 'online' : 'offline'"></div>
        <span class="session-name">SFTP</span>
        <span v-if="store.online" class="session-host">{{ store.connectionHost }}</span>
        
      </div>
      <div class="session-right">
        <!-- 有任务时入口本身就是读数：关掉弹窗也不再是"瞎了"，
             底边那条 2px 是队列总进度，随秒级 tick 走 -->
        <div
          v-if="store.transfers.length > 0"
          class="live-transfer"
          :class="{ active: showTransferList }"
          role="button"
          tabindex="0"
          :aria-pressed="showTransferList"
          title="传输任务"
          @click="showTransferList = !showTransferList"
          @keydown.enter.prevent="showTransferList = !showTransferList"
          @keydown.space.prevent="showTransferList = !showTransferList"
        >
          <span class="lt-ico">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
          </span>
          <span class="lt-num">{{ queueActiveCount }}</span>
          <span class="lt-sep" />
          <span class="lt-line">队列 <b>{{ store.queueStats.percent }}%</b></span>
          <span class="lt-bar" :style="{ width: store.queueStats.percent + '%' }" />
        </div>
        <button v-else class="btn transfer" @click="showTransferList = true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
          传输任务
        </button>
        <button v-if="!store.online" class="btn btn-primary" @click="showConnPicker = true">
          连接远程
        </button>
        <template v-else>
          <button class="btn" @click="showConnPicker = true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            切换
          </button>
          <button class="btn btn-danger" @click="handleDisconnect">
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

    <!-- 传输中心（队列汇总 / 目录分组 / 行内动作）—— 见 SftpTransferDialog.vue -->
    <SftpTransferDialog
      :visible="showTransferList"
      :now="now"
      @close="showTransferList = false"
    />

    <!-- 连接选择模态 -->
    <Teleport to="body">
    <div v-if="showConnPicker" class="conn-overlay" @click.self="showConnPicker = false">
      <div class="conn-dialog" role="dialog" aria-modal="true">
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
    </Teleport>

    <!-- 右键菜单 -->
    <div
      v-if="ctxMenu.visible"
      class="ctx-menu"
      :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
      @click="ctxMenu.visible = false"
      @contextmenu.prevent
    >
      <div v-if="ctxMenu.entry" class="ctx-item" @click="handleCtxAction('open')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        打开
      </div>
      <div v-if="ctxMenu.entry" class="ctx-item" @click="handleCtxAction('transfer')">
        <svg v-if="ctxMenu.side === 'local'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
        {{ ctxMenu.side === 'local' ? '上传' : '下载' }}
      </div>
      <div v-if="ctxMenu.entry" class="ctx-item" @click="handleCtxAction('copy')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        复制
      </div>
      <div v-if="canPaste" class="ctx-item" @click="handleCtxAction('paste')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>
        粘贴
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
    <Teleport to="body">
    <div v-if="renameDialog.visible" class="rename-overlay" @click.self="renameDialog.visible = false">
      <div class="rename-dialog" role="dialog" aria-modal="true">
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
    </Teleport>

    <!-- 压缩包文件名对话框 -->
    <Teleport to="body">
    <div v-if="zipDialog.visible" class="rename-overlay" @click.self="zipDialog.visible = false">
      <div class="rename-dialog" role="dialog" aria-modal="true">
        <div class="rename-title">压缩为 zip</div>
        <input
          ref="zipInput"
          v-model="zipDialog.name"
          class="rename-input"
          @keydown.enter="confirmZip"
          @keydown.escape="zipDialog.visible = false"
        />
        <div class="rename-actions">
          <button class="rename-btn cancel" @click="zipDialog.visible = false">取消</button>
          <button class="rename-btn confirm" @click="confirmZip">确定</button>
        </div>
      </div>
    </div>
    </Teleport>

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
import { menuTop } from '@/utils/menu'
import { ref, reactive, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import SftpPane from './SftpPane.vue'
import SftpFileEditor from './SftpFileEditor.vue'
import SftpTransferDialog from './SftpTransferDialog.vue'
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
const showTransferList = ref(false)

// ─── 秒级 tick ─────────────────────────
// 每行的「剩余」与速率是时间的函数。原实现在渲染期现读 Date.now() 却没有 tick：
// 被暂停的行、以及远端复制这类 total===0 的行收不到进度事件，数字
// 就永久停在最后一次更新上 —— 看起来像卡死。
//
// 时钟放在本组件而不是弹窗内部：会话栏徽标与弹窗里每一行必须读同一个 now，
// 否则同一屏上两处数字各跳各的。没有活动任务时停表，不让空页面白跑定时器。
const now = ref(Date.now())
let tickTimer: ReturnType<typeof setInterval> | null = null
function syncTick(): void {
  const shouldRun = store.activeTransfers.length > 0
  if (shouldRun && !tickTimer) {
    now.value = Date.now()
    tickTimer = setInterval(() => { now.value = Date.now() }, 1000)
  } else if (!shouldRun && tickTimer) {
    clearInterval(tickTimer)
    tickTimer = null
  }
}
watch(() => store.activeTransfers.length, syncTick, { immediate: true })
onUnmounted(() => {
  if (tickTimer) { clearInterval(tickTimer); tickTimer = null }
})

const queueActiveCount = computed(() => store.activeTransfers.length)

// ─── 进度订阅 ─────────────────────────
let unsubProgress: (() => void) | null = null
let unsubStalled: (() => void) | null = null
let unsubClosed: (() => void) | null = null
let unsubSshClose: (() => void) | null = null

onMounted(() => {
  // B9 修复：浏览器（非 Electron）环境无 electronAPI —
  // 原 `window.electronAPI!` 非空断言在此处 onMounted 即 TypeError
  const api = window.electronAPI
  if (!api) {
    console.warn('[SftpView] 非 Electron 环境，SFTP 功能不可用')
    return
  }
  unsubProgress = api.onSftpProgress((data) => {
    store.applyProgress(data)
  })
  unsubStalled = api.onSftpStalled((data) => {
    const item = store.transfers.find(t => t.id === data.id)
    const name = item?.name || '文件'
    toast.warning(`"${name}" 传输停滞，正在尝试自动续传…`)
  })
  // 同一次掉线主进程会同时发 sftp:closed 与 ssh:close 两条事件，两个回调都会
  // 命中这里的 sshSessionId 比对（disconnect 要 await 两次 IPC 才清空会话），
  // 于是断线提示弹两遍、disconnect() 重入。用一个「本次已提示过」的会话标记去重。
  let lostNotifiedFor = ''
  function onConnectionLost(data: { id: string }) {
    if (data.id !== store.sshSessionId || lostNotifiedFor === data.id) return
    lostNotifiedFor = data.id
    store.disconnect()
    toast.warning('远程 SFTP 连接已断开')
  }
  // 重新连接（含连回同一个 id）后解除标记，否则下一次真实掉线不再提示
  watch(() => store.sshSessionId, id => { if (id && id !== lostNotifiedFor) lostNotifiedFor = '' })
  unsubClosed = api.onSftpClosed(onConnectionLost)
  unsubSshClose = api.onSshClose(onConnectionLost)
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
// 审查修复（P2）：拖拽串行化 — 原实现 onTransfer 无并发守卫，快速连续拖拽
// 两份目录时，第二份在确认对话框出现前到达会直接走 upload/download，
// 第一份的确认被静默丢弃（用户以为取消了，实际第二份已在传；或反之），
// 且两批传输交叉时覆盖检查基于陈旧 cwd。用 Promise 链把所有拖拽请求
// 排队执行，保证"确认 → 传输"原子串行。
let transferQueue: Promise<unknown> = Promise.resolve()
function onTransfer(payload: { side: 'local' | 'remote'; paths: string[] }): Promise<void> {
  const run = transferQueue.then(() => doTransfer(payload))
  // 队列链吞掉单个失败，避免一次失败阻塞后续拖拽
  transferQueue = run.then(() => undefined, () => undefined)
  return run
}

async function doTransfer(payload: { side: 'local' | 'remote'; paths: string[] }): Promise<void> {
  // 覆盖确认：检查目标路径是否已存在同名文件
  const existingFiles: string[] = []
  if (payload.side === 'local') {
    // 上传：检查远程目标
    for (const lp of payload.paths) {
      const name = lp.split('/').pop()?.split('\\').pop() ?? lp
      const remotePath = store.remoteCwd === '/' ? '/' + name : store.remoteCwd + '/' + name
      const exists = await store.remotePathExists(remotePath)
      if (exists) existingFiles.push(name)
    }
  } else {
    // 下载：检查本地目标
    for (const rp of payload.paths) {
      const name = rp.split('/').pop() ?? rp
      // P3 修复：用 store.joinLocalPath（内部走 fsPathJoin）替代硬拼 '/' —
      // 硬拼在本地 cwd 已含分隔符或跨盘符时会产生错误路径，与 :625 下载展开逻辑不一致
      const localPath = await store.joinLocalPath(name)
      const exists = await store.localPathExists(localPath)
      if (exists) existingFiles.push(name)
    }
  }

  // 如果有文件已存在，弹出覆盖确认
  if (existingFiles.length > 0) {
    const fileList = existingFiles.slice(0, 3).join('、')
    const more = existingFiles.length > 3 ? ` 等 ${existingFiles.length} 个文件` : ''
    const ok = await confirm({
      title: '文件已存在',
      message: `目标位置已存在同名文件「${fileList}${more}」，是否覆盖？`,
      confirmText: '覆盖',
      danger: true,
    })
    if (!ok) return
  }

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
  // C32（两个右键菜单互不 dismiss）现在由结构保证：传输菜单只活在传输中心那层
  // 遮罩里，浮层在场时点不到底下的文件行，关浮层时它随组件一起收起。
  ctxMenu.visible = true
  ctxMenu.x = e.clientX
  ctxMenu.y = menuTop(e.clientY, 200)
  ctxMenu.entry = entry
  ctxMenu.side = side
}

/** 剪贴板中有来自同侧的数据时才可粘贴 */
const canPaste = computed(() => {
  if (!store.clipboard) return false
  return store.clipboard.side === ctxMenu.side
})

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

  if (action === 'copy' && entry) {
    const paths = getCtxPaths(side, entry)
    store.copyToClipboard(side, paths)
    toast.success(`已复制 ${paths.length} 项`)
    return
  }

  if (action === 'paste') {
    const cb = store.clipboard
    if (!cb) return
    // 确定目标目录：右键在目录上则粘贴到该目录，否则粘贴到当前 cwd
    const targetDir = entry?.isDirectory
      ? entry.path
      : (side === 'local' ? store.localCwd : store.remoteCwd)
    if (side === 'local') await store.localCopy(cb.paths, targetDir)
    else await store.remoteCopy(cb.paths, targetDir)
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
    openZipDialog(paths, defaultName, side)
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

// ─── 压缩 zip ──────────────────────────
const zipDialog = reactive({
  visible: false,
  name: '',
  side: '' as 'local' | 'remote',
  paths: [] as string[],
})
const zipInput = ref<HTMLInputElement | null>(null)

function openZipDialog(paths: string[], defaultName: string, side: 'local' | 'remote'): void {
  zipDialog.paths = paths
  zipDialog.name = defaultName
  zipDialog.side = side
  zipDialog.visible = true
  nextTick(() => {
    zipInput.value?.focus()
    // 预选主文件名部分（不含 .zip 扩展名）
    const dot = defaultName.lastIndexOf('.')
    if (dot > 0) zipInput.value?.setSelectionRange(0, dot)
  })
}

async function confirmZip(): Promise<void> {
  const finalName = zipDialog.name.trim()
  if (!finalName) return
  const { side, paths } = zipDialog
  zipDialog.visible = false
  // 检查目标 zip 是否已存在，提示用户是否覆盖
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
}

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
  // 先关窗再执行：确定按钮随弹窗销毁，连点不会再触发第二次 mkdir/rename
  // （原实现在 await 之后才置 visible=false，慢盘/远端抖动时双击会建两个目录，
  //  第二个报「已存在」—— 用户看到的是自己没点过的错误）
  renameDialog.visible = false
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
  /* 整页结构画布：走 --canvas-bg（默认=--bg-base；详见 DashboardView 同注）。 */
  background: var(--canvas-bg);
}

/* 会话状态栏：头部几何与 RemoteManager .rm-header 对齐（44px + 16px 侧距） */
.sftp-session-bar {
  height: var(--page-head-h);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  /* 结构工具条：走 --canvas-bg 与整页统一 */
  background: var(--canvas-bg);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.session-left { display: flex; align-items: center; gap: 8px; }
.session-dot {
  width: 8px; height: 8px; border-radius: 50%;
}
.session-dot.online { background: var(--accent-green); box-shadow: 0 0 6px color-mix(in srgb, var(--accent-green) 60%, transparent); }
.session-dot.offline { background: var(--text-tertiary); }
.session-name { color: var(--text-primary); font-size: var(--page-title-size); font-weight: 600; font-family: var(--font-sans); }
.session-host { color: var(--text-tertiary); font-family: var(--font-mono); font-size: 11px; }
.session-right { display: flex; gap: 8px; }
/* 按钮本体交给全局 .btn / .btn-primary / .btn-danger，这里只补角标锚点与图标尺寸 */
.session-right .btn { position: relative; }
.session-right .btn svg { width: 12px; height: 12px; }
/* 会话栏实时传输徽标：有任务时入口本身就是读数，底边 2px 是队列总进度。
   刻意不铺 accent 底 —— DESIGN.md 禁实心 CTA，语义只走字色与描边。 */
.live-transfer {
  display: flex; align-items: center; gap: 8px; position: relative; overflow: hidden;
  height: 30px; padding: 0 10px; cursor: pointer; user-select: none;
  border: 1px solid var(--border-default); border-radius: var(--radius-md);
  background: var(--bg-input); color: var(--text-secondary);
  font-family: var(--font-mono); font-size: 10px; line-height: 1;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}
.live-transfer:hover { background: var(--bg-hover); border-color: var(--accent-primary); }
.live-transfer.active { border-color: var(--accent-primary); }
.live-transfer .lt-ico { display: flex; color: var(--accent-primary); }
.live-transfer .lt-ico svg { width: 13px; height: 13px; }
.live-transfer .lt-num { font-family: var(--font-mono); font-size: 11px; font-weight: 600; color: var(--text-primary); }
.live-transfer .lt-sep { width: 1px; height: 12px; background: var(--border-default); }
.live-transfer .lt-line { font-family: var(--font-mono); font-size: 10px; color: var(--text-tertiary); white-space: nowrap; }
.live-transfer .lt-line b { color: var(--accent-primary); font-weight: 600; }
.live-transfer .lt-bar {
  position: absolute; left: 0; bottom: 0; height: 2px;
  background: var(--accent-primary); transition: width var(--transition-fast);
}

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
  background: var(--bg-elevated-solid);
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
  background: var(--bg-elevated-solid);
}
.ctx-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px;
  font-size: 12px; color: var(--text-secondary);
  cursor: pointer; border-radius: 4px;
}
.ctx-item:hover { background: var(--bg-hover); color: var(--text-primary); }
.ctx-item.danger:hover { background: color-mix(in srgb, var(--accent-red) 15%, transparent); color: var(--accent-red); }
.ctx-sep { height: 1px; margin: 4px 0; background: var(--border-subtle); }

/* 重命名/新建对话框 */
.rename-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 1001;
}
.rename-dialog {
  background: var(--bg-elevated-solid);
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
  background: var(--bg-input);
  color: var(--text-primary);
  font-size: 13px; outline: none;
  font-family: var(--font-sans);
}
.rename-input:focus { border-color: var(--accent-primary); }
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
.rename-btn.confirm { background: var(--accent-primary); color: var(--accent-on); border-color: var(--accent-primary); }
.rename-btn.confirm:hover { opacity: 0.9; }

</style>
