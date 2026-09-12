/**
 * 模型列表 Store — 全局共享的模型配置列表
 * 供模式聊天工厂（createModeChatStore）与 AI 面板的模型选择器使用。
 * 独立成 store 是为了避免"工厂 ↔ 调用方 store"的循环导入。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { api, type ModelConfig } from '@/api/client'

export const useModelsStore = defineStore('models', () => {
  const models = ref<ModelConfig[]>([])

  // in-flight 守卫：并发触发只下发一次 api.getModels()，消除启动阶段的重复请求。
  let fetchModelsPromise: Promise<void> | null = null
  async function fetchModels() {
    if (fetchModelsPromise) return fetchModelsPromise
    fetchModelsPromise = (async () => {
      try {
        models.value = await api.getModels()
      } catch (e) {
        console.error('[models] fetchModels failed:', e)
        models.value = []
      } finally {
        fetchModelsPromise = null
      }
    })()
    return fetchModelsPromise
  }

  return { models, fetchModels }
})
