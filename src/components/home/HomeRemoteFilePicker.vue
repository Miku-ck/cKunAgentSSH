<script setup lang="ts">
/**
 * ＋ 菜单里的远端文件选择器（spec 2026-09-29-home-command-panel 故事 5 的第五项）
 *
 * 只读不传：`sftpList` 列目录、`sftpReadFile` 取内容，挂成附件后与 `/chat/upload`
 * 返回的是同一形状，因此注入通路仍只有一条（见 ssh-ai.addRemoteAttachment）。
 * 刻意不再把内容上传到服务器 uploads/ 目录 —— 东西已经在渲染进程手上，再拷一遍
 * 只是多留一份副本。
 *
 * 浏览目录存在 store（remotePickerCwd）：SFTP 面板的 cwd 持久不丢，这里若是每次
 * 打开都从 '/' 开始，用户会以为"上次去的目录丢了"——两处必须同一个口径。
 */
import { computed, onMounted, ref } from 'vue'
import { toFriendlyMessage } from '@/api/client'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useSSHStore } from '@/stores/ssh'
import { formatBytes } from '@/utils/format'

/** 与后端 chat.py 的 MAX_UPLOAD_SIZE 同值：超过它的文件主进程那边本来就抛，
 *  灰在列表里比点下去吃一条错误提示省事。 */
const READABLE_MAX = 10 * 1024 * 1024

const emit = defineEmits<{
  picked: [payload: { path: string; filename: string }]
  back: []
}>()

const ssh = useSSHStore()
const sshAi = useSshAiStore()

// 目录放在 store（remotePickerCwd）：＋菜单 v-if 挂载，组件销毁不丢浏览位置，
// 重开仍停在用户上次所在目录（与 SFTP 面板 cwd 持久同一个口径）。
const cwd = computed(() => sshAi.remotePickerCwd)
const entries = ref<SftpEntry[]>([])
const loading = ref(false)
const error = ref('')
/** 正在读的文件路径 —— 读取比列目录慢得多，没有可见反馈就是一次"点了没反应" */
const reading = ref('')

/** 列目录与读取各一个序号：连点 ".." 时慢的旧请求后到会覆盖新目录（SFTP 面板踩过，
 *  那边是 B20）。两类请求互不相干，共用一个号会让"进目录"把在跑的读取判成过期。 */
let listSeq = 0
let readSeq = 0

const session = computed(() => ssh.sessions.find(s => s.id === ssh.activeSessionId))
/** 只有远程会话有远端文件系统：本地 shell 会话点了只会得到一句"会话未连接" */
const usable = computed(() => session.value?.type === 'remote' && !!ssh.activeSessionId)
const notText = (size: number) => size > READABLE_MAX

/** readdir 的顺序不是人能读的：目录置顶、按名排。/etc 那种两百条的目录里，
 *  不排序等于让用户滚着找。 */
const sorted = computed(() =>
  [...entries.value].sort((a, b) =>
    Number(b.isDirectory) - Number(a.isDirectory) || a.name.localeCompare(b.name),
  ),
)

function parentOf(path: string): string {
  const trimmed = path.endsWith('/') ? path.slice(0, -1) : path
  const idx = trimmed.lastIndexOf('/')
  return idx <= 0 ? '/' : trimmed.slice(0, idx)
}

async function load(path: string) {
  const api = window.electronAPI
  if (!api) {
    error.value = '远端文件仅在桌面端可用'
    return
  }
  if (!usable.value) {
    error.value = '当前没有远程终端会话可取文件'
    return
  }
  const sid = ssh.activeSessionId as string
  const seq = ++listSeq
  loading.value = true
  error.value = ''
  try {
    // 主进程所有 sftp* 调用都过 requireSftp —— 通道没开直接抛 SFTP_SESSION_REQUIRED。
    // 首页这条通路先前只能"顺便"用：你得先去 SFTP 面板连一次才能列目录。
    // openSftp 自身幂等（已开即 return），且不关：通道按会话键控，SFTP 面板共用同一条。
    await api.sftpOpen(sid)
    const list = await api.sftpList(sid, path)
    if (seq !== listSeq) return
    entries.value = list
    sshAi.remotePickerCwd = path
  } catch (err) {
    if (seq !== listSeq) return
    error.value = toFriendlyMessage(err, '列目录失败')
  } finally {
    if (seq === listSeq) loading.value = false
  }
}

async function pick(entry: SftpEntry) {
  const api = window.electronAPI
  if (!api || !usable.value || entry.isDirectory || notText(entry.size)) return
  const sid = ssh.activeSessionId as string
  const seq = ++readSeq
  reading.value = entry.path
  error.value = ''
  try {
    // 归属必须在 await 之前定下来：session 是跟着 activeSessionId 走的计算属性，
    // 读一个几 MB 的文件期间切到 B 会话的话，旧写法会把 chip 写成 B、而注入的
    // 内容来自 A（同名文件时尤其骗人）。sid 已经钉住了读取的那条会话，来源同源即可。
    const source = session.value?.shell || session.value?.name || 'remote'
    const file = await api.sftpReadFile(sid, entry.path)
    if (seq !== readSeq) return
    sshAi.addRemoteAttachment({
      path: entry.path,
      content: file.content,
      size: file.size,
      source,
    })
    emit('picked', { path: entry.path, filename: entry.name })
  } catch (err) {
    if (seq !== readSeq) return
    error.value = toFriendlyMessage(err, '读取失败')
  } finally {
    if (seq === readSeq) reading.value = ''
  }
}

onMounted(() => { load(cwd.value || '/') })
</script>

<template>
  <div class="hrp">
    <div class="hrp-bar">
      <!-- ‹ 只负责"上一级"：原先它 emit('back') 直接关掉整个选择器，在深层目录里
           点它像"返回跳回标准页面"——列表里的 .. 与它语义重复却更不可见。 -->
      <button
        type="button"
        class="hrp-back"
        title="上一级"
        :disabled="cwd === '/' || loading"
        @click="load(parentOf(cwd))"
      >‹</button>
      <code class="hrp-path" :title="cwd">{{ cwd }}</code>
      <button
        type="button"
        class="hrup-refresh"
        title="重新列目录"
        :disabled="loading"
        @click="load(cwd)"
      >↻</button>
      <button type="button" class="hrp-back" title="关闭（返回 ＋ 菜单）" @click="emit('back')">×</button>
    </div>

    <p v-if="!usable" class="hrp-hint">先在终端里连上一台服务器，才能取它的文件。</p>
    <p v-else-if="error" class="hrp-hint err">{{ error }}</p>
    <p v-else-if="loading" class="hrp-hint">列目录中…</p>

    <ul v-else class="hrp-list">
      <li v-if="cwd !== '/'">
        <button type="button" class="hrp-row up" @click="load(parentOf(cwd))">..</button>
      </li>
      <li v-for="e in sorted" :key="e.path">
        <button
          type="button"
          class="hrp-row"
          :class="{ dir: e.isDirectory }"
          :disabled="!e.isDirectory && (notText(e.size) || reading === e.path)"
          :title="!e.isDirectory && notText(e.size) ? '超过 10MB，读不了' : e.path"
          @click="e.isDirectory ? load(e.path) : pick(e)"
        >
          <span class="nm">{{ e.name }}</span>
          <span v-if="e.isDirectory" class="sz">/</span>
          <span v-else class="sz">{{ reading === e.path ? '读取中…' : formatBytes(e.size) }}</span>
        </button>
      </li>
      <li v-if="!entries.length && cwd !== '/'"><p class="hrp-hint">空目录</p></li>
    </ul>
  </div>
</template>

<style scoped>
/* 这套样式跟着 .hcc-menu.wide 的盒子走（220px 宽、260px 高、可滚）。
   只用各主题都已有的令牌 —— 手记主题下它就是米纸上一份紧凑列表，别的主题下
   也不该出现"这块没跟着换肤"的洞。 */
.hrp { display: flex; flex-direction: column; gap: var(--spacing-2) }
.hrp-bar {
  display: flex;
  align-items: center;
  gap: var(--spacing-4);
  padding: var(--spacing-2) var(--spacing-2) 5px;
  border-bottom: 1px solid var(--divider-primary);
}
.hrp-back,
.hrup-refresh {
  width: 18px;
  height: 18px;
  line-height: 16px;
  padding: 0;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
}
.hrp-back:hover,
.hrup-refresh:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-primary) }
.hrp-path {
  flex: 1;
  min-width: 0;
  font-size: 0.8462rem;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  /* 目录是"我在哪"，不是要读的代码 —— 让它比 .mono 轻一点 */
  letter-spacing: 0;
}
.hrp-list { list-style: none; margin: 0; padding: 0; max-height: 208px; overflow-y: auto }
.hrp-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-6);
  width: 100%;
  text-align: left;
  padding: var(--spacing-4) var(--spacing-6);
  border-radius: var(--radius-sm);
  font-size: 0.9231rem;
  color: var(--text-primary);
}
.hrp-row:hover:not(:disabled) { background: var(--bg-hover) }
.hrp-row:disabled { opacity: .55; cursor: default }
.hrp-row .nm {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hrp-row.dir .nm { color: var(--accent-text) }
.hrp-row .sz { font-family: var(--font-mono); font-size: 0.7692rem; color: var(--text-tertiary) }
.hrp-row.up { color: var(--text-secondary) }
.hrp-hint { padding: var(--spacing-6); font-size: 0.8462rem; color: var(--text-tertiary) }
.hrp-hint.err { color: var(--danger-text) }
</style>
