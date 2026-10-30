<template>
  <Teleport to="body">
    <div class="dt-overlay" @click.self="emit('close')">
      <div ref="dialogRef" class="dt-dialog dt-dialog-auto k8s-scale" role="dialog" aria-modal="true" aria-label="改副本数" tabindex="-1">
        <div class="dp-head">
          <span class="dp-name mono">{{ row.name }}</span>
          <span class="badge">{{ row.kind }}</span>
          <span class="badge mono">{{ row.namespace }}</span>
          <button class="dp-close" title="关闭" aria-label="关闭" @click="emit('close')">×</button>
        </div>
        <div class="dp-body dp-body-auto">
          <label class="field">
            <span class="field-k">副本数</span>
            <input
              ref="inputRef" v-model="value" class="host-sel num" type="text" inputmode="numeric"
              aria-label="副本数" :aria-invalid="!!error" @keydown.enter.prevent="submit"
            />
            <span class="field-cur mono">当前 {{ row.desired }}</span>
          </label>
          <div v-if="error" class="field-err" role="alert">{{ error }}</div>
          <p class="note">{{ ACTION_CONSEQUENCE.scale }}</p>
        </div>
        <div class="dlg-foot">
          <button class="btn" @click="emit('close')">取消</button>
          <button class="btn btn-primary" :disabled="!!error || submitting" @click="submit">
            {{ submitting ? '下发中…' : `缩放到 ${value}` }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
// K8sScaleDialog.vue — 改副本数。输入即时校验（kubectl 那边也有白名单，但把错误
// 拦在这里才不会先下发一条注定失败的命令、再在审计链里留一条噪声）。
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'
import { ACTION_CONSEQUENCE, validateReplicas, type WorkloadRow } from '@/stores/k8s-core'

const props = defineProps<{ row: WorkloadRow; submitting: boolean }>()
const emit = defineEmits<{ close: []; submit: [replicas: number] }>()

const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef)
function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') emit('close')
}

const value = ref(String(props.row.desired))
const error = ref<string | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)

watch(value, (v) => { error.value = validateReplicas(v) })

onMounted(() => {
  activate()
  window.addEventListener('keydown', onKeydown)
  void nextTick(() => inputRef.value?.select())
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  deactivate()
})

function submit(): void {
  error.value = validateReplicas(value.value)
  if (error.value) return
  emit('submit', Number(value.value))
}
</script>
