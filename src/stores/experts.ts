/**
 * 专家 Store — 运行策略配置包的列表与 CRUD。
 *
 * 边界：专家只改配置，不引入新可执行能力（那是插件）；「当前选中哪个专家」
 * 放在 ssh-ai store（属于「下一次发送带什么」），本 store 只管定义本身。
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { expertsApi, type Expert, type ExpertPayload } from '@/api/core'

export const useExpertsStore = defineStore('experts', () => {
  const experts = ref<Expert[]>([])
  const loading = ref(false)
  const error = ref('')

  const enabledExperts = computed(() => experts.value.filter(e => e.enabled === 1))
  const builtin = computed(() => experts.value.find(e => e.is_builtin === 1) || null)

  function byId(id: string): Expert | null {
    return experts.value.find(e => e.id === id) || null
  }

  async function fetchExperts(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      experts.value = await expertsApi.list()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function createExpert(payload: ExpertPayload & { slug: string }): Promise<Expert> {
    const created = await expertsApi.create(payload)
    await fetchExperts()
    return created
  }

  async function updateExpert(id: string, payload: Partial<ExpertPayload>): Promise<Expert> {
    const updated = await expertsApi.update(id, payload)
    await fetchExperts()
    return updated
  }

  async function duplicateExpert(id: string, slug: string, name?: string): Promise<Expert> {
    const copy = await expertsApi.duplicate(id, slug, name)
    await fetchExperts()
    return copy
  }

  async function setEnabled(id: string, enabled: boolean): Promise<void> {
    await expertsApi.setEnabled(id, enabled)
    await fetchExperts()
  }

  async function removeExpert(id: string): Promise<void> {
    await expertsApi.remove(id)
    await fetchExperts()
  }

  return {
    experts,
    loading,
    error,
    enabledExperts,
    builtin,
    byId,
    fetchExperts,
    createExpert,
    updateExpert,
    duplicateExpert,
    setEnabled,
    removeExpert,
  }
})
