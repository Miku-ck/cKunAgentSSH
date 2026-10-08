<template>
  <SettingsDialog ref="dialogRef" />
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import SettingsDialog from './SettingsDialog.vue'
import { registerSettingsDialog } from '@/composables/useSettings'

const dialogRef = ref<InstanceType<typeof SettingsDialog> | null>(null)
let unregister: (() => void) | null = null

onMounted(() => {
  if (dialogRef.value) {
    unregister = registerSettingsDialog(dialogRef.value)
  }
})

// 不注销的话，模块级单例会一直攥着这个已卸载组件的实例，openSettings 从此静默失效
onBeforeUnmount(() => {
  unregister?.()
  unregister = null
})
</script>
