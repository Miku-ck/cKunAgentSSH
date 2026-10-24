<template>
  <Teleport to="body">
    <div class="palette-overlay" @mousedown.self="emit('close')">
      <div class="palette" role="dialog" aria-modal="true" aria-label="命令面板">
        <input
          ref="inputRef"
          v-model="query"
          class="palette-input"
          type="text"
          placeholder="搜索页面、终端、连接、快捷命令…"
          spellcheck="false"
          @keydown="onKeydown"
        />
        <div ref="listRef" class="palette-list">
          <div v-if="filtered.length === 0" class="palette-empty">无匹配项</div>
          <button
            v-for="(item, i) in filtered"
            :key="item.key"
            type="button"
            class="palette-item"
            :class="{ active: i === activeIndex }"
            @click="runItem(item)"
            @mousemove="activeIndex = i"
          >
            <span class="pi-group">{{ item.group }}</span>
            <span class="pi-label">{{ item.label }}</span>
            <span v-if="item.detail" class="pi-detail">{{ item.detail }}</span>
          </button>
        </div>
        <div class="palette-hint">
          <span>↑↓ 选择</span><span>Enter 执行</span><span>Esc 关闭</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
// CommandPalette.vue — Ctrl+Shift+P 命令面板（M1-P0）
// 四类条目：功能页 / 终端会话 / SSH 连接 / 快捷命令。
// 页面与会话动作经 emit 交回 SshView（导航/焦点逻辑集中在 useSshNavigation），
// 连接与快捷命令直接走 store —— 与 RemoteManager 行内连接、QuickCommandBar
// 下发是完全同一通道，不另造执行路径。
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import { useQuickCommandsStore, type QuickCommand } from '@/stores/quickCommands'
import { useToastStore } from '@/stores/toast'
import type { NavPage } from '@/composables/useSshNavigation'

interface PaletteItem {
  key: string
  group: string
  label: string
  detail?: string
  run: () => void
}

const emit = defineEmits<{
  close: []
  navigate: [page: Exclude<NavPage, 'home'>]
  goHome: []
  activateSession: [id: string]
  connect: [connId: string]
}>()

const sshStore = useSSHStore()
const qcStore = useQuickCommandsStore()

const query = ref('')
const activeIndex = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)
const listRef = ref<HTMLElement | null>(null)

qcStore.load()

// ── 页面条目（顺序与 PAGE_SHORTCUTS 一致，标题条分组也按这个语义族） ──
const PAGE_ITEMS: Array<{ page: Exclude<NavPage, 'home'>; label: string }> = [
  { page: 'remote-mgr', label: '打开：远程管理' },
  { page: 'sftp', label: '打开：SFTP 文件传输' },
  { page: 'dashboard', label: '打开：监控仪表盘' },
  { page: 'schedules', label: '打开：定时任务' },
  { page: 'tunnels', label: '打开：SSH 隧道' },
  { page: 'docker', label: '打开：Docker 管理' },
  { page: 'git', label: '打开：Git 工具' },
  { page: 'audit', label: '打开：运维轨迹' },
]

function buildItems(): PaletteItem[] {
  const items: PaletteItem[] = PAGE_ITEMS.map(({ page, label }) => ({
    key: `page:${page}`,
    group: '页面',
    label,
    run: () => emit('navigate', page),
  }))
  items.push({ key: 'page:home', group: '页面', label: '返回首页', run: () => emit('goHome') })

  for (const s of sshStore.sessions) {
    items.push({
      key: `session:${s.id}`,
      group: '终端',
      label: s.name,
      detail: s.type === 'local' ? s.shell : '远程',
      run: () => emit('activateSession', s.id),
    })
  }
  for (const c of sshStore.connections) {
    items.push({
      key: `conn:${c.id}`,
      group: '连接',
      label: c.name,
      detail: `${c.username}@${c.host}:${c.port}`,
      run: () => emit('connect', c.id),
    })
  }
  for (const cmd of qcStore.sortedCommands) {
    items.push({
      key: `qc:${cmd.id}`,
      group: '快捷命令',
      label: cmd.name,
      detail: cmd.command.length > 48 ? cmd.command.slice(0, 48) + '…' : cmd.command,
      run: () => runQuickCommand(cmd),
    })
  }
  return items
}

/** 与 QuickCommandBar.sendCommand 同一通道：写入活动会话，不自动回车 */
function runQuickCommand(cmd: QuickCommand): void {
  const sessionId = sshStore.activeSessionId
  const session = sshStore.sessions.find(s => s.id === sessionId)
  if (!session) return
  const api = window.electronAPI
  if (!api) return
  const writer = session.type === 'local' ? api.ptyWrite : api.sshWrite
  writer(session.id, cmd.command).catch((err: unknown) => {
    console.error('[commandPalette] send failed:', sessionId, err)
    useToastStore().error(`命令未发送：${err instanceof Error ? err.message : String(err)}`)
  })
  emit('close')
  window.dispatchEvent(new CustomEvent('ssh:focus-terminal'))
}

// ── 模糊过滤：子序列命中 + 连续/词首加分（评分与 quickCommands 联想同一思路） ──
function fuzzyScore(text: string, q: string): number {
  if (!q) return 1
  const t = text.toLowerCase()
  let ti = 0
  let score = 0
  let streak = 0
  for (const ch of q.toLowerCase()) {
    const found = t.indexOf(ch, ti)
    if (found === -1) return 0
    streak = found === ti ? streak + 1 : 0
    const wordStart = found === 0 || /[\W_\s]/.test(t[found - 1])
    score += 1 + streak + (wordStart ? 1 : 0)
    ti = found + 1
  }
  return score
}

const filtered = computed<PaletteItem[]>(() => {
  const all = buildItems()
  const q = query.value.trim()
  if (!q) return all
  return all
    .map(item => ({ item, score: fuzzyScore(item.label + ' ' + (item.detail || ''), q) }))
    .filter(x => x.score > 0)
    .sort((a, b) => b.score - a.score)
    .map(x => x.item)
})

watch(filtered, () => { activeIndex.value = 0 })
watch(activeIndex, () => {
  nextTick(() => {
    const el = listRef.value?.children[activeIndex.value] as HTMLElement | undefined
    el?.scrollIntoView({ block: 'nearest' })
  })
})

function runItem(item: PaletteItem): void {
  emit('close')
  item.run()
}

// 挂载即聚焦输入框：v-if 打开后父组件无需编排焦点
onMounted(() => inputRef.value?.focus())

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    emit('close')
    return
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    activeIndex.value = Math.min(activeIndex.value + 1, filtered.value.length - 1)
    return
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault()
    activeIndex.value = Math.max(activeIndex.value - 1, 0)
    return
  }
  if (e.key === 'Enter') {
    e.preventDefault()
    const item = filtered.value[activeIndex.value]
    if (item) runItem(item)
  }
}

</script>

<style scoped>
.palette-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding-top: 12vh;
}

.palette {
  width: min(520px, 90vw);
  max-height: 60vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.palette-input {
  height: 40px;
  padding: 0 14px;
  border: none;
  border-bottom: 1px solid var(--border-subtle);
  background: transparent;
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  flex-shrink: 0;
}

.palette-input::placeholder {
  color: var(--text-tertiary);
}

.palette-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px;
}

.palette-empty {
  padding: 18px 0;
  text-align: center;
  font-size: 12px;
  color: var(--text-tertiary);
}

.palette-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  font-family: var(--font-sans);
  text-align: left;
  cursor: pointer;
}

.palette-item.active {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.pi-group {
  flex-shrink: 0;
  min-width: 48px;
  font-size: 10px;
  color: var(--text-tertiary);
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  padding: 1px 4px;
  text-align: center;
}

.pi-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pi-detail {
  flex-shrink: 0;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--text-tertiary);
}

.palette-hint {
  display: flex;
  gap: 14px;
  padding: 6px 12px;
  border-top: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}
</style>
