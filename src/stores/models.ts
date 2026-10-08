/**
 * 模型列表 Store — 全局共享的模型配置列表
 * 供 ssh-ai store 与 AI 面板的模型选择器使用。
 * 独立成 store 是为了避免"ssh-ai ↔ models"的循环导入。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { api, type ModelConfig } from '@/api/client'

export const useModelsStore = defineStore('models', () => {
  const models = ref<ModelConfig[]>([])

  // in-flight 守卫：并发触发只下发一次 api.getModelConfigs()，消除启动阶段的重复请求。
  let fetchModelsPromise: Promise<void> | null = null
  async function fetchModels() {
    if (fetchModelsPromise) return fetchModelsPromise
    fetchModelsPromise = (async () => {
      try {
        models.value = await api.getModelConfigs()
      } catch (e) {
        // 留着上一次读到的列表：这份是只读缓存，GET 抖动时清空它的效果是
        // 「用户的模型全都不见了」—— 选择器当场变空，而下拉里那条模型其实还在库里。
        // 最坏情况只是少看到一条新加的模型，刷新一次就回来。
        console.error('[models] fetchModels failed:', e)
      } finally {
        fetchModelsPromise = null
      }
    })()
    return fetchModelsPromise
  }

  return { models, fetchModels }
})
