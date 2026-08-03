<template>
  <ConfirmDialog ref="dialogRef" />
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { registerConfirmDialog } from '@/composables/useConfirm'

const dialogRef = ref<InstanceType<typeof ConfirmDialog> | null>(null)

onMounted(() => {
  if (dialogRef.value) {
    registerConfirmDialog(dialogRef.value)
  }
})
</script>
