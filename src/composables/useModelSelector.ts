/**
 * 模型选择器 Composable
 * 从 SideChatPanel 中提取的通用模型选择逻辑（聊天与 SSH AI 面板共用同一宿主）。
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import type { ModelConfig } from '@/api/client'

interface UseModelSelectorOptions {
  /** 当前选中的模型 ID（getter） */
  getSelectedId: () => string
  /** 更新选中的模型 ID（setter） */
  setSelectedId: (id: string) => void
  /** 可用模型列表 */
  models: () => ModelConfig[]
}

export function useModelSelector(options: UseModelSelectorOptions) {
  const { getSelectedId, setSelectedId, models } = options

  const showModelDropdown = ref(false)
  const modelSelectorRef = ref<HTMLElement | null>(null)

  const currentModelName = computed(() => {
    const id = getSelectedId()
    const list = models()
    const model = list.find(m => m.id === id)
    // 优先展示友好名称，其次才是 model_id / id
    return model?.name || model?.model_id || model?.id || '选择模型'
  })

  function selectModel(id: string) {
    setSelectedId(id)
    showModelDropdown.value = false
  }

  function handleModelClickOutside(e: MouseEvent) {
    if (modelSelectorRef.value && !modelSelectorRef.value.contains(e.target as Node)) {
      showModelDropdown.value = false
    }
  }

  onMounted(() => {
    document.addEventListener('click', handleModelClickOutside)
  })

  onUnmounted(() => {
    document.removeEventListener('click', handleModelClickOutside)
  })

  return {
    showModelDropdown,
    modelSelectorRef,
    currentModelName,
    selectModel,
  }
}
