import { ref } from 'vue'
import type { SettingCategory } from '@/stores/settings'

type SettingsDialogInstance = {
  open: (category?: SettingCategory) => void
  close: () => void
}

const dialogInstance = ref<SettingsDialogInstance | null>(null)

/**
 * 注册全局唯一的设置对话框实例，返回注销函数。
 *
 * dialogInstance 是模块级单例，不会跟着组件走。持有者卸载时必须注销：留着一个
 * 已卸载组件的实例引用，之后 openSettings 就静默变成空操作 —— 点了没反应，
 * 且没有任何报错线索（调用链上只有一处可选链）。
 */
export function registerSettingsDialog(instance: SettingsDialogInstance): () => void {
  dialogInstance.value = instance
  return () => {
    if (dialogInstance.value === instance) dialogInstance.value = null
  }
}

export function useSettings() {
  function openSettings(category: SettingCategory = 'models') {
    dialogInstance.value?.open(category)
  }
  return { openSettings }
}
