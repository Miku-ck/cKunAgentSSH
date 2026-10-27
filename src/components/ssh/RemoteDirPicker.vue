<script setup lang="ts">
/**
 * 远端目录 / 文件选择器
 *
 * 只做「列远端目录」这一件事：sftpOpen（幂等）+ sftpList，不碰调用方的业务。
 * Docker 镜像导出/加载需要它来选远端落盘位置，两端都不经过本地。
 *
 * 竞态：连点「上一级」时慢的旧请求后到会把新目录覆盖回旧目录。与
 * HomeRemoteFilePicker 同一套序号写法（那边踩过，SFTP 面板是 B20）。
 */
import { computed, ref, watch } from 'vue'
import { toFriendlyMessage } from '@/api/client'
import { formatBytes } from '@/utils/format'

const props = withDefaults(defineProps<{
  /** 目标 SSH 会话；未连接时组件自身给一句提示，不向上抛错 */
  sessionId: string
  /** dir：只列目录（选保存位置）；file：目录可进，文件可选 */
  mode?: 'dir' | 'file'
  /** 初始目录；变化时重新列（父级切换目标主机） */
  initial?: string
  /** file 模式下的后缀白名单，如 ['.tar', '.tar.gz']；空表示不过滤 */
  accept?: string[]
  disabled?: boolean
}>(), { mode: 'dir', initial: '/', accept: () => [], disabled: false })

const emit = defineEmits<{
  /** 当前目录变化（dir 模式靠它拿到用户选的目录） */
  'update:dir': [path: string]
  /** file 模式选中一个文件 */
  pick: [entry: SftpEntry]
}>()

const cwd = ref(props.initial || '/')
/** 路径框独立于 cwd：用户边敲边看，回车才真的跳；直接绑 cwd 会让每次按键都触发列目录 */
const pathInput = ref(cwd.value)
const entries = ref<SftpEntry[]>([])
const loading = ref(false)
const error = ref('')
const selected = ref('')

let seq = 0

/** readdir 顺序不可读：目录置顶、按名排 */
const shown = computed(() => {
  const list = entries.value.filter((e) => {
    if (e.isDirectory) return true
    if (props.mode === 'dir') return false
    if (!props.accept.length) return true
    const n = e.name.toLowerCase()
    return props.accept.some(ext => n.endsWith(ext.toLowerCase()))
  })
  return list.sort((a, b) => Number(b.isDirectory) - Number(a.isDirectory) || a.name.localeCompare(b.name))
})

const atRoot = computed(() => cwd.value === '/')
/** 空列表的两种成因分开说：目录模式过滤掉了所有文件，说「没有匹配的文件」会让人以为目录是空的 */
const emptyText = computed(() => {
  if (!entries.value.length) return '空目录'
  if (props.mode === 'dir') return '此目录下没有子目录'
  // 白名单由调用方给，文案跟着它走 —— 写死后缀会在调用方放宽后悄悄过时
  return props.accept.length ? `没有 ${props.accept.join(' / ')}` : '此目录下没有文件'
})

function parentOf(path: string): string {
  const trimmed = path.endsWith('/') ? path.slice(0, -1) : path
  const idx = trimmed.lastIndexOf('/')
  return idx <= 0 ? '/' : trimmed.slice(0, idx)
}

async function load(path: string): Promise<void> {
  const api = window.electronAPI
  if (!api) {
    error.value = '远端文件仅在桌面端可用'
    return
  }
  if (!props.sessionId) {
    error.value = '当前没有远程终端会话'
    return
  }
  const target = String(path || '').trim() || '/'
  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    // 主进程所有 sftp* 都过 requireSftp，通道没开直接抛 SFTP_SESSION_REQUIRED。
    // openSftp 自身幂等（已开即 return），且不关：通道按会话键控，SFTP 面板共用同一条。
    await api.sftpOpen(props.sessionId)
    const list = await api.sftpList(props.sessionId, target)
    if (mine !== seq) return
    entries.value = list
    cwd.value = target
    pathInput.value = target
    selected.value = ''
    emit('update:dir', target)
  } catch (err) {
    if (mine !== seq) return
    error.value = toFriendlyMessage(err, '列目录失败')
  } finally {
    if (mine === seq) loading.value = false
  }
}

function onPick(entry: SftpEntry): void {
  if (props.disabled) return
  if (entry.isDirectory) {
    void load(entry.path)
    return
  }
  selected.value = entry.path
  emit('pick', entry)
}

watch(() => props.sessionId, () => {
  void load(props.initial || '/')
}, { immediate: true })
</script>

<template>
  <div class="rdp">
    <div class="rdp-bar">
      <button
        type="button" class="rdp-ico" title="上一级"
        :disabled="atRoot || loading || disabled"
        @click="load(parentOf(cwd))"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>
      </button>
      <input
        v-model="pathInput" class="rdp-path mono" :disabled="disabled" spellcheck="false"
        autocomplete="off" aria-label="远端目录路径"
        @keydown.enter.prevent="load(pathInput)"
      />
      <button type="button" class="rdp-ico" title="重新列目录" :disabled="loading || disabled" @click="load(cwd)">
        <svg v-if="loading" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
      </button>
    </div>

    <div v-if="error" class="rdp-msg err">{{ error }}</div>
    <div v-else-if="loading && !entries.length" class="rdp-msg">列目录中…</div>
    <div v-else class="rdp-list">
      <button v-if="!atRoot" type="button" class="rdp-row" :disabled="disabled" @click="load(parentOf(cwd))">
        <svg class="rdp-file" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>
        <span class="rdp-nm">..</span>
      </button>
      <button
        v-for="e in shown" :key="e.path" type="button" class="rdp-row"
        :class="{ on: selected === e.path, dir: e.isDirectory }"
        :disabled="disabled" :title="e.path"
        @click="onPick(e)"
      >
        <svg v-if="e.isDirectory" class="rdp-file" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h6l2 2h10v10H3z"/></svg>
        <svg v-else class="rdp-file" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/></svg>
        <span class="rdp-nm">{{ e.name }}</span>
        <span v-if="!e.isDirectory" class="rdp-sz mono">{{ formatBytes(e.size) }}</span>
      </button>
      <div v-if="!shown.length" class="rdp-msg">{{ emptyText }}</div>
    </div>
  </div>
</template>

<style scoped>
.rdp { display: flex; flex-direction: column; gap: var(--spacing-6); min-width: 0 }
.rdp-bar { display: flex; align-items: center; gap: var(--spacing-4) }
.rdp-ico {
  width: 24px; height: 24px; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center;
  border: none; background: transparent; border-radius: var(--radius-sm);
  color: var(--text-secondary); cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.rdp-ico svg { width: 13px; height: 13px; }
.rdp-ico .spin { animation: rdp-spin .8s linear infinite; }
.rdp-ico:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-primary); }
.rdp-ico:disabled { opacity: .4; cursor: default; }
@keyframes rdp-spin { to { transform: rotate(360deg) } }

.rdp-path {
  flex: 1; min-width: 0; height: 24px; padding: 0 var(--spacing-8);
  background: var(--bg-input); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm); color: var(--text-primary); font-size: 0.8462rem;
}
.rdp-path:focus { outline: none; border-color: var(--accent-primary); }

.rdp-list {
  max-height: 150px; overflow-y: auto;
  border: 1px solid var(--border-subtle); border-radius: var(--radius-md);
  padding: var(--spacing-2); background: var(--bg-input);
}
.rdp-row {
  display: flex; align-items: center; gap: var(--spacing-6); width: 100%;
  padding: var(--spacing-4) 7px; border: none; background: transparent; border-radius: var(--radius-sm);
  text-align: left; font-size: 0.9231rem; color: var(--text-primary); cursor: pointer;
}
.rdp-row:hover:not(:disabled) { background: var(--bg-hover); }
.rdp-row.on { background: color-mix(in srgb, var(--accent-primary) 12%, transparent); }
.rdp-row.dir .rdp-nm { color: var(--accent-text); }
.rdp-row:disabled { opacity: .5; cursor: default; }
.rdp-file { width: 12px; height: 12px; flex-shrink: 0; color: var(--text-tertiary); }
.rdp-row.dir .rdp-file { color: var(--accent-text); }
.rdp-nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rdp-sz { flex-shrink: 0; font-size: 0.7692rem; color: var(--text-tertiary); }
.rdp-msg { padding: var(--spacing-10) var(--spacing-8); font-size: 0.8462rem; color: var(--text-tertiary); text-align: center; }
.rdp-msg.err { color: var(--status-error-text); }
</style>
