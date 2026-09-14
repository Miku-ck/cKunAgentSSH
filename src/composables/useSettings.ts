import { ref } from 'vue'
import type { SettingCategory } from '@/stores/settings'

type SettingsDialogInstance = {
  open: (category?: SettingCategory) => void
  close: () => void
}

const dialogInstance = ref<SettingsDialogInstance | null>(null)

export function registerSettingsDialog(instance: SettingsDialogInstance) {
  dialogInstance.value = instance
}

export function useSettings() {
  function openSettings(category: SettingCategory = 'models') {
    dialogInstance.value?.open(category)
  }
  function closeSettings() {
    dialogInstance.value?.close()
  }
  return { openSettings, closeSettings }
}
