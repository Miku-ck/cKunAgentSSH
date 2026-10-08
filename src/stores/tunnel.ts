// stores/tunnel.ts — SSH 隧道规则与运行状态
// 主进程类型定义见 electron/tunnel-store.ts / tunnel-manager.ts（渲染层副本在 src/env.d.ts，
// 两个 tsconfig 程序互不包含，沿用 SSHConnectionRecord 的跨上下文复制模式）
import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useTunnelStore = defineStore('tunnel', () => {
  const rules = ref<TunnelRule[]>([])
  /** 运行状态表（key = rule.id）。无条目 = stopped */
  const statuses = ref<Record<string, TunnelStatus>>({})
  const loaded = ref(false)
  let unsubscribe: (() => void) | null = null

  /**
   * 初始化：拉规则 + 恢复运行状态 + 订阅推送。
   * 在 SshView onMounted 调用（组件常驻，订阅即全局存活）；
   * loaded 防重复订阅（SshView 只挂载一次，但 keep-alive 场景下仍需守卫）。
   */
  async function init(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    if (unsubscribe) return
    unsubscribe = api.onTunnelStatus(status => {
      statuses.value = { ...statuses.value, [status.id]: status }
    })
    const [list, all] = await Promise.all([
      api.tunnelList(),
      api.tunnelStatusAll(),
    ])
    rules.value = list
    const map: Record<string, TunnelStatus> = { ...statuses.value }
    for (const s of all) map[s.id] = s
    statuses.value = map
    loaded.value = true
  }

  function stateOf(ruleId: string): TunnelStatus['state'] {
    return statuses.value[ruleId]?.state ?? 'stopped'
  }

  async function refreshRules(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    rules.value = await api.tunnelList()
  }

  async function save(rule: TunnelRule): Promise<TunnelRule> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const saved = await api.tunnelSave(rule)
    await refreshRules()
    return saved
  }

  async function remove(id: string): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    await api.tunnelDelete(id)
    const map = { ...statuses.value }
    delete map[id]
    statuses.value = map
    await refreshRules()
  }

  /** 拉起；失败以 status 返回（state=error + message），不 throw */
  async function start(id: string): Promise<TunnelStatus> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const status = await api.tunnelStart(id)
    statuses.value = { ...statuses.value, [status.id]: status }
    return status
  }

  async function stop(id: string): Promise<TunnelStatus> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const status = await api.tunnelStop(id)
    statuses.value = { ...statuses.value, [status.id]: status }
    return status
  }

  return {
    rules,
    statuses,
    loaded,
    init,
    stateOf,
    refreshRules,
    save,
    remove,
    start,
    stop,
  }
})
