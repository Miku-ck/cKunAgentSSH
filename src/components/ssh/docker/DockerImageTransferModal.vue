<script setup lang="ts">
/**
 * 镜像导出 / 加载弹窗（纯远端执行）
 *
 * tar 只落在远端主机上：docker save 的 stdout 是二进制流，走不了 sshExec 的文本通道
 * （1MB 截断 + UTF-8 解码），所以必须 -o 落盘；加载侧对称地 -i 读盘。两端都不经过
 * 本地，因此这里没有任何传输逻辑，只负责「选远端位置 + 校验 + 把命令交给父级」。
 *
 * 执行放在父级（DockerView）：审计、dockerExec 封装、成功后刷新列表都在那边，
 * 组件内自己 exec 会把这一整套复制一遍。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { imageLabel, suggestTarName, validateSaveName, ensureTarSuffix, validateRemoteDir, joinRemotePath, dockerLoadCmd } from '@/stores/docker-core'
import type { ImageRow } from '@/stores/docker-core'
import RemoteDirPicker from '../RemoteDirPicker.vue'

const props = defineProps<{
  mode: 'export' | 'import'
  sessionId: string
  /** export 必填：要导出的镜像 */
  image?: ImageRow | null
  /** 执行中：禁用全部输入并拒绝关闭（中途断 channel 只会得到残缺 tar） */
  running?: boolean
  /** 父级归一后的执行错误文案 */
  error?: string
  /** 已耗时秒数，仅在 running 时展示 */
  elapsed?: number
  /** 上次用的目录，避免每次打开都从 / 重新找 */
  initialDir?: string
}>()

const emit = defineEmits<{
  close: []
  submit: [payload: { mode: 'export'; ref: string; path: string; gz: boolean } | { mode: 'import'; path: string }]
}>()

const isExport = computed(() => props.mode === 'export')
/** 起始目录只取一次：它回流给 RemoteDirPicker 的话，picker 列完目录 → 父级 dir 变 →
 *  prop 再传下去 → 又列一次，白跑一轮请求。之后目录只单向向上报。 */
const startDir = props.initialDir || '/'
const dir = ref(startDir)
const name = ref(props.image ? suggestTarName(props.image.repo, props.image.tag, props.image.id) : '')
/** import 选中的文件；export 用 dir + name 现算，不存 */
const pickedPath = ref('')
/** 导出格式：false = docker save 原样落盘，true = 管道 gzip。
 * 加载侧不看它 —— docker load 按魔数识别压缩，.tar.gz 直接能读。 */
const gz = ref(false)
const overwrite = ref(false)
const checking = ref(false)

let checkTimer: ReturnType<typeof setTimeout> | null = null
let checkSeq = 0

const fullPath = computed(() => joinRemotePath(dir.value, ensureTarSuffix(name.value, gz.value)))
const nameErr = computed(() => (isExport.value ? validateSaveName(name.value) : null))
const dirErr = computed(() => validateRemoteDir(dir.value))
const canSubmit = computed(() =>
  !props.running && !dirErr.value && (isExport.value ? !nameErr.value && !!name.value.trim() : !!pickedPath.value))

/**
 * 覆盖预检：`docker save -o` 对已存在文件是静默截断，不提示就等于让用户
 * 在不知情的情况下覆盖掉上一次的备份。查不到（SFTP 未就绪等）按「不存在」放行，
 * 不因为一个探测失败把导出卡死。
 */
function scheduleOverwriteCheck(): void {
  if (checkTimer) clearTimeout(checkTimer)
  overwrite.value = false
  // 先领号再早退：表单一失效，在途探测就作废，不能等它回来把警告又点亮
  const mine = ++checkSeq
  const api = window.electronAPI
  const p = fullPath.value
  if (!api || !isExport.value || !props.sessionId || nameErr.value || dirErr.value || !name.value.trim()) return
  checkTimer = setTimeout(async () => {
    checking.value = true
    try {
      await api.sftpOpen(props.sessionId)
      const exists = await api.sftpExists(props.sessionId, p)
      if (mine === checkSeq) overwrite.value = exists
    } catch {
      /* 探测失败不阻塞：docker save 自己会截断，把提示让给执行结果 */
    } finally {
      if (mine === checkSeq) checking.value = false
    }
  }, 350)
}

// sessionId 也要看：换目标主机后同名文件在不在是另一回事，拿旧主机的结论会漏报覆盖
watch([fullPath, () => props.sessionId], scheduleOverwriteCheck, { immediate: true })

/** 父级切换导出对象时（弹窗不重挂载），文件名要跟着换，否则会把 A 的 tar 名用在 B 上 */
watch(() => props.image, (im) => {
  if (im) name.value = suggestTarName(im.repo, im.tag, im.id, gz.value)
})
/** 换格式时把输入框里的后缀一起换掉：不能让用户看着 .tar 实际写出 .tar.gz */
function setFormat(next: boolean): void {
  if (props.running || gz.value === next) return
  gz.value = next
  name.value = ensureTarSuffix(name.value, next)
}
/** 换目录就清掉已选文件：列表已不显示它，留着会让「选中的」和「看到的」对不上 */
function onDirChange(p: string): void {
  dir.value = p
  pickedPath.value = ''
}
watch(() => props.sessionId, () => { pickedPath.value = '' })

/** 打包/加载在途时拒绝一切关闭路径：关掉面板不等于停掉远端进程 */
function onClose(): void {
  if (props.running) return
  emit('close')
}

function submit(): void {
  if (!canSubmit.value) return
  if (isExport.value) {
    emit('submit', { mode: 'export', ref: imageLabel(props.image as ImageRow), path: fullPath.value, gz: gz.value })
  } else {
    emit('submit', { mode: 'import', path: pickedPath.value })
  }
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') onClose()
}
document.addEventListener('keydown', onKey)
onUnmounted(() => {
  document.removeEventListener('keydown', onKey)
  if (checkTimer) clearTimeout(checkTimer)
})

const cmdPreview = computed(() => (pickedPath.value ? dockerLoadCmd(pickedPath.value) : ''))

/** 镜像行拆成 repo + tag 两段：长 repo 可截断，:vX.Y.Z 永远留在可视区 ——
 * 同一个 repo 的多个版本靠 tag 区分，把它截掉等于让用户猜导出的是哪个。 */
const imgText = computed(() => {
  const im = props.image
  if (!im) return { repo: '', tag: '' }
  if (!im.repo || im.repo === '<none>' || !im.tag || im.tag === '<none>') return { repo: imageLabel(im), tag: '' }
  return { repo: im.repo, tag: `:${im.tag}` }
})
</script>

<template>
  <div class="dt-overlay" @click.self="onClose">
    <div
      class="dt-dialog dt-dialog-auto imgx" role="dialog" aria-modal="true"
      :aria-label="isExport ? '导出镜像' : '加载镜像'"
    >
      <div class="dp-head">
        <svg class="imgx-hd" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <template v-if="isExport">
            <path d="M21 8v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8"/><path d="M2 3h20v5H2z"/><path d="M10 12h4"/>
          </template>
          <template v-else>
            <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>
          </template>
        </svg>
        <span class="dp-name">{{ isExport ? '导出镜像' : '加载镜像' }}</span>
        <button class="dp-close" :disabled="running" aria-label="关闭" @click="onClose">×</button>
      </div>

      <div class="imgx-body">
        <!-- 导出：给谁导出，一行说完 -->
        <div v-if="isExport && image" class="imgx-img">
          <span class="imgx-ref mono" :title="imageLabel(image)">{{ imgText.repo }}</span>
          <span v-if="imgText.tag" class="imgx-tag mono">{{ imgText.tag }}</span>
          <span class="imgx-size mono">{{ image.size }}</span>
        </div>

        <RemoteDirPicker
          :session-id="sessionId"
          :mode="isExport ? 'dir' : 'file'"
          :initial="startDir"
          :accept="isExport ? [] : ['.tar', '.tar.gz', '.tgz']"
          :disabled="!!running"
          @update:dir="onDirChange"
          @pick="pickedPath = $event.path"
        />

        <!-- 导出：文件名 + 完整路径 -->
        <template v-if="isExport">
          <div class="imgx-field">
            <span class="imgx-lbl">文件名</span>
            <input
              v-model="name" class="imgx-input mono" :disabled="!!running"
              spellcheck="false" autocomplete="off" aria-label="导出文件名"
            />
          </div>
          <div class="imgx-field">
            <span class="imgx-lbl">格式</span>
            <div class="imgx-fmt" role="group" aria-label="导出格式">
              <button
                type="button" :class="{ on: !gz }" :aria-pressed="!gz"
                :disabled="!!running" @click="setFormat(false)"
              >tar</button>
              <button
                type="button" :class="{ on: gz }" :aria-pressed="gz"
                :disabled="!!running" @click="setFormat(true)"
              >tar.gz</button>
            </div>
            <span v-if="gz" class="imgx-hint">gzip 压缩，打包更慢</span>
          </div>
          <div class="imgx-path mono" :title="fullPath">→ {{ fullPath }}</div>
          <div v-if="nameErr" class="imgx-warn">{{ nameErr }}</div>
          <div v-else-if="dirErr" class="imgx-warn">{{ dirErr }}</div>
          <div v-else-if="overwrite" class="imgx-warn">该文件已存在，导出会覆盖它</div>
          <div v-else-if="checking" class="imgx-hint">检查同名文件…</div>
        </template>

        <!-- 加载：命令预览，让用户看到将要执行什么 -->
        <template v-else>
          <div v-if="cmdPreview" class="imgx-path mono" :title="cmdPreview">{{ cmdPreview }}</div>
          <div v-else class="imgx-hint">选一个 .tar / .tar.gz / .tgz</div>
        </template>

        <div v-if="running" class="imgx-run">
          <svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
          {{ isExport ? '正在打包' : '正在加载' }}… <span class="mono">{{ elapsed ?? 0 }}s</span>
        </div>
        <div v-if="error" class="imgx-err">{{ error }}</div>
      </div>

      <div class="imgx-foot">
        <span v-if="running" class="imgx-note">{{ isExport ? '打包' : '加载' }}期间不可取消</span>
        <span class="imgx-spacer"></span>
        <button class="btn" :disabled="!!running" @click="onClose">取消</button>
        <button class="btn btn-primary" :disabled="!canSubmit" @click="submit">
          {{ isExport ? '导出' : '加载' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.imgx { width: min(420px, 100vw - 48px) }
.imgx-hd { width: 14px; height: 14px; flex-shrink: 0; color: var(--accent-text) }
.imgx-body { display: flex; flex-direction: column; gap: 9px; padding: 14px 18px; overflow-y: auto }

.imgx-img {
  display: flex; align-items: baseline; gap: 8px; min-width: 0;
  padding: 7px 10px; border-radius: var(--radius-md);
  background: var(--bg-input); border: 1px solid var(--border-subtle);
}
.imgx-ref { font-size: 12px; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.imgx-tag { flex-shrink: 0; font-size: 12px; color: var(--accent-text) }
.imgx-size { margin-left: auto; flex-shrink: 0; font-size: 11px; color: var(--text-tertiary) }

.imgx-field { display: flex; align-items: center; gap: 8px }
.imgx-lbl { flex-shrink: 0; font-size: 11px; color: var(--text-tertiary) }
.imgx-input {
  flex: 1; min-width: 0; height: 24px; padding: 0 8px;
  background: var(--bg-input); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm); color: var(--text-primary); font-size: 11px;
}
.imgx-input:focus { outline: none; border-color: var(--accent-primary) }

/* 格式切换：分段控件而不是下拉 —— 只有两个值，展开菜单是多余的一步 */
.imgx-fmt {
  display: inline-flex; gap: 2px; padding: 2px;
  background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
}
.imgx-fmt button {
  height: 18px; padding: 0 9px; border-radius: 3px;
  background: transparent; color: var(--text-secondary);
  font-family: var(--font-mono); font-size: 11px; cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.imgx-fmt button:hover:not(:disabled):not(.on) { color: var(--text-primary) }
.imgx-fmt button.on { background: color-mix(in srgb, var(--accent-primary) 12%, transparent); color: var(--accent-text) }
.imgx-fmt button:disabled { opacity: .45; cursor: default }

/* 完整路径一行小字：不占面板高度，又让用户随时能核对最终落盘位置 */
.imgx-path {
  font-size: 10px; color: var(--text-tertiary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.imgx-warn { font-size: 11px; color: var(--status-warn-text) }
.imgx-hint { font-size: 11px; color: var(--text-tertiary) }
.imgx-err {
  padding: 8px 10px; border-radius: var(--radius-md); font-size: 12px; line-height: 1.55;
  color: var(--status-error-text);
  background: color-mix(in srgb, var(--status-error-text) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--status-error-text) 25%, transparent);
}
.imgx-run {
  display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text-secondary);
}
.imgx-run svg { width: 12px; height: 12px; animation: imgx-spin .8s linear infinite }
@keyframes imgx-spin { to { transform: rotate(360deg) } }

.imgx-foot {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 18px; border-top: 1px solid var(--divider-primary);
}
.imgx-note { font-size: 10px; color: var(--text-tertiary) }
.imgx-spacer { flex: 1 }
/* 执行中关闭键只是看起来可点，点了不会有反应 —— 给个禁用观感 */
.imgx .dp-close:disabled { opacity: .3; cursor: default; pointer-events: none }
</style>
