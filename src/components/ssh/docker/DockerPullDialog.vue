<template>
  <div class="dk-overlay" @click.self="closePull">
    <div class="dk-dialog" role="dialog" aria-modal="true" aria-label="拉取镜像">
      <div class="dk-header">
        <span>拉取镜像</span>
        <button class="dk-close" :disabled="pulling" aria-label="关闭" @click="closePull">×</button>
      </div>
      <form class="pull-body" @submit.prevent="doPull">
        <label class="pull-label" for="docker-pull-ref">镜像引用</label>
        <input
          id="docker-pull-ref"
          ref="pullInputEl"
          v-model="pullRef"
          class="pull-input mono"
          placeholder="nginx:1.27 或 harbor.example.com/app:tag"
          autocomplete="off"
          spellcheck="false"
          :disabled="pulling"
        />
        <div v-if="pullRefInvalid" class="pull-invalid">引用格式不合法：不含空白，且不能以 - 开头</div>
        <div v-if="pullError" class="pull-err">
          <div>{{ pullError.message }}</div>
          <div v-if="pullError.hint" class="pull-hint">{{ pullError.hint }}</div>
        </div>
        <div class="pull-foot">
          <span v-if="pulling" class="pull-elapsed mono">{{ pullElapsed }}s</span>
          <span class="pull-spacer"></span>
          <button type="button" class="btn" :disabled="pulling" @click="closePull">取消</button>
          <button type="submit" class="btn btn-primary" :disabled="pulling || !pullRef.trim() || pullRefInvalid">
            <svg v-if="pulling" class="pull-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
            拉取
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
// DockerPullDialog.vue — 拉取镜像：引用先校验，再走 600s 预算的 docker pull（层下载远超读命令）。
// 在途不可关闭：关掉再开会让两条 pull 的 finally 互相踩状态；页面隐藏由父级整树收起。
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useToastStore } from '@/stores/toast'
import { pullCmd, type DockerError } from '@/stores/docker-core'
import { AUDIT_ACTION_DOCKER } from '@/composables/remoteAudit'
import { useDockerHost } from './context'

const emit = defineEmits<{
  close: []
  /** 拉取成功：把发起时的宿主 id 一并给出，父级据此决定要不要重拉列表 */
  pulled: [targetId: string]
}>()
const { dockerExec, targetId } = useDockerHost()
const toast = useToastStore()

// 层下载可能远超读命令：主进程把 ssh:exec 超时钳到 600s，这里用满；
// --quiet 只留摘要行，避免进度条把 1MB 输出上限撑爆。
const PULL_TIMEOUT_SEC = 600
const IMAGE_REF_RE = /^[a-zA-Z0-9][a-zA-Z0-9._:@/-]*$/
const pullRef = ref('')
const pullInputEl = ref<HTMLInputElement | null>(null)
const pulling = ref(false)
const pullError = ref<DockerError | null>(null)
const pullElapsed = ref(0)
let pullClock: ReturnType<typeof setInterval> | null = null
const pullRefInvalid = computed(() => {
  const ref = pullRef.value.trim()
  return !!ref && (ref.startsWith('-') || !IMAGE_REF_RE.test(ref))
})

function startPullClock(): void {
  stopPullClock()
  pullElapsed.value = 0
  pullClock = setInterval(() => { pullElapsed.value += 1 }, 1000)
}
function stopPullClock(): void {
  if (pullClock) { clearInterval(pullClock); pullClock = null }
}

// 在途拉取不可关闭：关掉再开会让两条 pull 的 finally 互相踩状态；页面隐藏走父级强制收起。
function closePull(): void {
  if (pulling.value) return
  emit('close')
  stopPullClock()
}

async function doPull(): Promise<void> {
  if (pulling.value) return
  const ref = pullRef.value.trim()
  if (!ref || ref.startsWith('-') || !IMAGE_REF_RE.test(ref)) return
  const id = targetId.value
  pulling.value = true
  pullError.value = null
  startPullClock()
  try {
    await dockerExec(pullCmd(ref), id, PULL_TIMEOUT_SEC, AUDIT_ACTION_DOCKER)
    toast.success(`已拉取 ${ref}`)
    emit('close')
    emit('pulled', id)
  } catch (e) {
    pullError.value = e as DockerError
  } finally {
    pulling.value = false
    stopPullClock()
  }
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') closePull()
}
onMounted(() => {
  document.addEventListener('keydown', onKey)
  void nextTick(() => pullInputEl.value?.focus())
})
onUnmounted(() => {
  stopPullClock()
  document.removeEventListener('keydown', onKey)
})
</script>
