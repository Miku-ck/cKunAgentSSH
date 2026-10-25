<template>
  <ConfirmDialog ref="dialogRef" />
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { registerConfirmDialog } from '@/composables/useConfirm'

const dialogRef = ref<InstanceType<typeof ConfirmDialog> | null>(null)

let unregister: (() => void) | null = null

onMounted(() => {
  if (dialogRef.value) {
    unregister = registerConfirmDialog(dialogRef.value)
  }
})

// 卸载注销：module 单例里不留已销毁组件的引用（R-F1）
onUnmounted(() => {
  unregister?.()
  unregister = null
})
</script>
