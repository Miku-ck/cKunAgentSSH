<template>
  <SettingsDialog ref="dialogRef" />
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import SettingsDialog from './SettingsDialog.vue'
import { registerSettingsDialog } from '@/composables/useSettings'

const dialogRef = ref<InstanceType<typeof SettingsDialog> | null>(null)

onMounted(() => {
  if (dialogRef.value) {
    registerSettingsDialog(dialogRef.value)
  }
})
</script>
