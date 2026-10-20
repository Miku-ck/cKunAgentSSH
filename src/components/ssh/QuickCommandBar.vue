<template>
  <div class="quick-command-bar">
    <div class="qc-list">
      <template v-if="store.sortedCommands.length > 0">
        <button
          v-for="cmd in store.sortedCommands"
          :key="cmd.id"
          class="qc-item"
          :class="{ dragging: dragState.id === cmd.id, 'drag-over': dragState.overId === cmd.id }"
          :style="{ borderColor: cmd.color }"
          :title="cmd.command"
          draggable="true"
          @click="sendCommand($event, cmd)"
          @contextmenu.prevent="showContextMenu($event, cmd)"
          @dragstart="handleDragStart($event, cmd.id)"
          @dragover.prevent="handleDragOver($event, cmd.id)"
          @drop="handleDrop($event, cmd.id)"
          @dragend="handleDragEnd"
        >
          {{ cmd.name }}
        </button>
      </template>
      <div v-else class="qc-empty">
        暂无快捷命令
      </div>
    </div>

    <!-- 加号描边固定 1.8，与首页输入卡 ＋ 菜单那组图标同档（同为 viewBox 24 缩到
         14px，写 2.5 会渲染成 ~1.46px，比那组的 ~1.05px 明显更粗、看起来也更亮）。 -->
    <button class="qc-add-btn" title="添加快捷命令" @click="openAdd">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="14" height="14">
        <line x1="12" y1="5" x2="12" y2="19"/>
        <line x1="5" y1="12" x2="19" y2="12"/>
      </svg>
    </button>

    <!-- 右键菜单 -->
    <div
      v-if="contextMenu.visible"
      class="qc-ctx-menu"
      :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
      @click.stop
      @contextmenu.prevent
    >
      <div class="qc-ctx-item" @click="handleEdit">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
        </svg>
        编辑
      </div>
      <div class="qc-ctx-sep"></div>
      <div class="qc-ctx-item danger" @click="handleDelete">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
        </svg>
        删除
      </div>
    </div>

    <!-- 编辑弹窗 -->
    <QuickCommandEditor
      :visible="editorVisible"
      :edit-data="editingCommand"
      @close="closeEditor"
      @save="handleSave"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import { useQuickCommandsStore, type QuickCommand } from '@/stores/quickCommands'
import { useConfirm } from '@/composables/useConfirm'
import { useToastStore } from '@/stores/toast'
import { menuLeft, menuTop } from '@/utils/menu'
import QuickCommandEditor from './QuickCommandEditor.vue'

const sshStore = useSSHStore()
const store = useQuickCommandsStore()
const { confirm } = useConfirm()
const toast = useToastStore()

const editorVisible = ref(false)
const editingCommand = ref<QuickCommand | null>(null)

const contextMenu = ref({ visible: false, x: 0, y: 0, target: null as QuickCommand | null })
const dragState = ref({ id: '', overId: '' })

onMounted(() => {
  store.load()
  document.addEventListener('click', dismissContextMenu)
})

onUnmounted(() => {
  document.removeEventListener('click', dismissContextMenu)
})

function sendCommand(e: MouseEvent, cmd: QuickCommand) {
  // 点击后立即 blur，防止按钮保持焦点导致按 Enter 重复触发 click
  ;(e.currentTarget as HTMLElement).blur()
  const sessionId = sshStore.activeSessionId
  if (!sessionId) return
  const session = sshStore.sessions.find(s => s.id === sessionId)
  if (!session) return
  const api = window.electronAPI
  if (!api) return
  const writer = session.type === 'local' ? api.ptyWrite : api.sshWrite
  writer(sessionId, cmd.command).catch((err: any) => {
    console.error('[quickCommand] send failed:', sessionId, err)
    // S4-06：ssh:write / pty:write 在会话缺失时不再静默 no-op，而是抛
    // SESSION_NOT_FOUND / 本地终端已关闭 —— 这条 catch 从不可达死码变成唯一
    // 的用户反馈通道。带上原因，重连窗口里点快捷命令不会再「点了没反应」。
    toast.error(`命令未发送：${err?.message || String(err)}`)
  })
  // 发送命令后将焦点还给活动终端，用户可手动按 Enter 执行
  window.dispatchEvent(new CustomEvent('ssh:focus-terminal'))
}

function showContextMenu(e: MouseEvent, cmd: QuickCommand) {
  // 菜单 .qc-ctx-menu 是 position:fixed 且 min-width 120：此前只校了垂直方向，
  // 靠右边缘时光标处开出的菜单会溢出视口（与标签页菜单同一修法）
  contextMenu.value = {
    visible: true,
    x: menuLeft(e.clientX, 120),
    y: menuTop(e.clientY, 100),
    target: cmd,
  }
}

function dismissContextMenu() {
  contextMenu.value.visible = false
}

function openAdd() {
  editingCommand.value = null
  editorVisible.value = true
}

function handleEdit() {
  const target = contextMenu.value.target
  contextMenu.value.visible = false
  if (!target) return
  editingCommand.value = target
  editorVisible.value = true
}

async function handleDelete() {
  const target = contextMenu.value.target
  contextMenu.value.visible = false
  if (!target) return
  const ok = await confirm({
    title: '删除快捷命令',
    message: `确认删除快捷命令「${target.name}」?`,
    confirmText: '删除',
    danger: true,
  })
  if (ok) {
    store.removeCommand(target.id)
  }
}

function closeEditor() {
  editorVisible.value = false
  editingCommand.value = null
}

function handleSave(data: { name: string; command: string; color: string }) {
  if (editingCommand.value) {
    store.updateCommand(editingCommand.value.id, data)
  } else {
    store.addCommand(data.name, data.command, data.color)
  }
  closeEditor()
}

// ─── 拖拽排序 ───
function handleDragStart(e: DragEvent, id: string) {
  dragState.value.id = id
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', id)
  }
}

function handleDragOver(e: DragEvent, id: string) {
  if (e.dataTransfer) {
    e.dataTransfer.dropEffect = 'move'
  }
  if (dragState.value.id && dragState.value.id !== id) {
    dragState.value.overId = id
  }
}

function handleDrop(e: DragEvent, targetId: string) {
  e.preventDefault()
  const sourceId = e.dataTransfer?.getData('text/plain') || dragState.value.id
  if (sourceId && sourceId !== targetId) {
    store.reorder(sourceId, targetId)
  }
  dragState.value = { id: '', overId: '' }
}

function handleDragEnd() {
  dragState.value = { id: '', overId: '' }
}
</script>

<style scoped>
.quick-command-bar {
  /* 38px 是这一条的固有高度（内容 26px + 上下呼吸 + 1px 上边框）。
     此前它读 --qc-h 由 SshView 用 ResizeObserver 反推，为的是与右侧 AI 面板的
     输入区分隔线对齐；面板退役后那条基线不存在了，高度回到自己的值。 */
  height: 38px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
  /* 与 .ssh-view/.term-tabs 同一层画布：手记的纸纹要一路 continuous，命令条画平色
     就是在终端底下横切一条不带纹理的带子（见 variables.css --canvas-bg 注） */
  background: var(--canvas-bg);
  background-attachment: fixed;
  border-top: 1px solid var(--border-subtle);
  position: relative;
  z-index: 15;
}

.qc-list {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow-x: auto;
  height: 100%;
}

.qc-list::-webkit-scrollbar {
  height: 0;
  display: none;
}

.qc-item {
  height: 26px;
  padding: 0 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  /* 不画自己的皮（同首页 .hcc-btn）：底色透画布，识别靠 borderColor=cmd.color 那条内联边框 */
  background: transparent;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-sans);
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
  user-select: none;
}

.qc-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.qc-item:active {
  background: var(--bg-active);
}

.qc-item.dragging {
  opacity: 0.4;
}

.qc-item.drag-over {
  background: var(--bg-hover);
  border-color: var(--accent-primary);
}



.qc-empty {
  color: var(--text-tertiary);
  font-size: 12px;
  user-select: none;
  padding: 0 4px;
}

.qc-add-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  background: transparent;
  border-radius: 4px;
  color: var(--text-secondary);
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.15s, color 0.15s;
}

.qc-add-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.qc-add-btn:active {
  background: var(--bg-active);
}

.qc-ctx-menu {
  position: fixed;
  z-index: 9999;
  min-width: 120px;
  padding: 4px 0;
  /* 浮起层：菜单归 --bg-elevated（variables.css 头部的两级背景约定），
     与同屏的 .tab-ctx-menu / .ntm-menu 同一张皮 */
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: var(--shadow-lg);
}

.qc-ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.1s;
}

.qc-ctx-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.qc-ctx-item.danger:hover {
  background: color-mix(in srgb, var(--accent-red) 15%, transparent);
  color: var(--accent-red);
}

.qc-ctx-sep {
  height: 1px;
  margin: 4px 0;
  background: var(--border-subtle);
}

/* 全局背景图模式下这条带子跟着画布一起让位（同 .ssh-view/.term-tabs/.titlebar） */
:global(.app.has-background) .quick-command-bar {
  background: transparent !important;
}
</style>
