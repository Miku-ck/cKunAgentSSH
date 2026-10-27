<template>
  <Teleport to="body">
    <div v-if="visible" class="imgp-overlay" @click.self="close">
      <div class="imgp-dialog" role="dialog" aria-modal="true" :aria-label="'图片预览 ' + name">
        <div class="imgp-head">
          <span class="imgp-title" :title="path">{{ name }}</span>
          <span v-if="sizeText" class="imgp-size">{{ sizeText }}</span>
          <button class="imgp-close" aria-label="关闭" @click="close">×</button>
        </div>
        <div class="imgp-body">
          <div v-if="loading" class="imgp-status">
            <svg class="imgp-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="22" height="22"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
            正在读取…
          </div>
          <div v-else-if="error" class="imgp-status imgp-error">{{ error }}</div>
          <img
            v-else-if="src"
            :src="src"
            :alt="name"
            class="imgp-img"
            :class="{ 'imgp-img-native': zoomed }"
            draggable="false"
            @click="zoomed = !zoomed"
          />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { imageMime } from '@/utils/language'
import { formatBytes } from '@/utils/format'

const props = defineProps<{
  visible: boolean
  /** 图片所在侧：local 走 fs:readBinary，remote 走 sftp:readBinary */
  side: 'local' | 'remote'
  /** 图片绝对路径 */
  path: string
  name: string
  sshSessionId: string
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const src = ref('')
const loading = ref(false)
const error = ref('')
const zoomed = ref(false)
const sizeText = ref('')

// 竞态守卫：连续切换查看不同图片时，慢的先返回的读取不得覆盖后发起的结果
// （沿用 SftpFileEditor loadSeq / store openSeq 同一手法）。
let loadSeq = 0

async function load(): Promise<void> {
  const my = ++loadSeq
  src.value = ''
  sizeText.value = ''
  error.value = ''
  zoomed.value = false
  const mime = imageMime(props.name)
  if (!mime) { error.value = '不支持的图片格式'; return }
  const api = window.electronAPI
  if (!api) { error.value = '非 Electron 环境，无法预览'; return }
  loading.value = true
  try {
    // 远程需会话 id；本地直接按绝对路径读。两侧二进制读取通道形态一致（{base64,size}），
    // 各自在主进程侧 stat 后挡 >5MB，超限抛友好文案原样透出。
    const { base64, size } = props.side === 'local'
      ? await api.fsReadBinary(props.path)
      : await api.sftpReadBinary(props.sshSessionId, props.path)
    if (my !== loadSeq) return // 有更新的读取在途，丢弃本次
    src.value = `data:${mime};base64,${base64}`
    sizeText.value = formatBytes(size)
  } catch (err) {
    if (my !== loadSeq) return
    // 主进程侧对 >5MB 抛「图片过大（超过 5MB），无法预览」等友好文案，直接透出
    error.value = (err as Error)?.message || '图片读取失败'
  } finally {
    if (my === loadSeq) loading.value = false
  }
}

watch(
  () => [props.visible, props.path] as const,
  ([v]) => { if (v) void load() },
  { immediate: true },
)

function close(): void {
  loadSeq++ // 作废在途读取，避免关闭后回填
  emit('close')
}
</script>

<style scoped>
.imgp-overlay {
  position: fixed; inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center;
  z-index: 1200;
}
.imgp-dialog {
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  width: min(1100px, 92vw);
  max-height: 90vh;
  display: flex; flex-direction: column;
  font-family: var(--font-sans);
  color: var(--text-primary);
  overflow: hidden;
}
.imgp-head {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-subtle);
}
.imgp-title {
  font-size: 13px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1; min-width: 0;
}
.imgp-size { font-size: 12px; opacity: 0.66; font-variant-numeric: tabular-nums; }
.imgp-close {
  border: none; background: transparent; color: var(--text-primary);
  font-size: 20px; line-height: 1; cursor: pointer;
  width: 26px; height: 26px; border-radius: var(--radius-sm);
}
.imgp-close:hover { background: var(--bg-input); }
.imgp-body {
  flex: 1; min-height: 240px;
  overflow: auto;
  display: flex; align-items: center; justify-content: center;
  /* 中性底：透明 PNG 的棋盘会喧宾夺主，用输入底色衬托即可 */
  background: var(--bg-input);
  padding: 16px;
}
.imgp-status { font-size: 13px; opacity: 0.8; display: flex; align-items: center; gap: 8px; }
.imgp-error { color: var(--danger-text); }
.imgp-spin { animation: imgp-rot 0.9s linear infinite; }
@keyframes imgp-rot { to { transform: rotate(360deg); } }
.imgp-img {
  max-width: 100%; max-height: calc(90vh - 64px);
  object-fit: contain;
  cursor: zoom-in;
  border-radius: var(--radius-sm);
}
.imgp-img-native {
  max-width: none; max-height: none;
  cursor: zoom-out;
  image-rendering: pixelated;
}
</style>
