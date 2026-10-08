// stores/access.ts — TLS 接入网关记录与会话状态
// 主进程类型见 electron/gateway-store.ts / gateway-manager.ts（渲染层副本在 src/env.d.ts，
// 两个 tsconfig 程序互不包含，沿用 tunnel.ts 的跨上下文复制模式）
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export const useAccessStore = defineStore('access', () => {
  const gateways = ref<GatewayRecord[]>([])
  /** 会话状态表（key = gatewayId）。无条目 = idle */
  const statuses = ref<Record<string, GatewayStatus>>({})
  /** 待用户确认的 TOFU 指纹（主进程推，确认/取消后出队） */
  const pendingHostKeys = ref<GatewayHostKeyEvent[]>([])
  const loaded = ref(false)
  let unsubscribers: (() => void)[] = []

  const onlineCount = computed(() =>
    Object.values(statuses.value).filter(s => s.state === 'online').length,
  )

  /** 状态条那一句「已连接至 X」：多个在线时并列，与 HSC 底栏同一口径 */
  const onlineNames = computed(() =>
    Object.values(statuses.value).filter(s => s.state === 'online').map(s => s.name || s.host),
  )

  function stateOf(id: string): GatewayStatus['state'] {
    return statuses.value[id]?.state ?? 'idle'
  }

  function statusOf(id: string): GatewayStatus | undefined {
    return statuses.value[id]
  }

  async function refresh(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    gateways.value = await api.gatewayList()
  }

  /** 在 SshView onMounted 调用（组件常驻，订阅即全局存活） */
  async function init(): Promise<void> {
    const api = window.electronAPI
    if (!api || unsubscribers.length) return
    unsubscribers.push(
      api.onGatewayStatus(s => {
        statuses.value = { ...statuses.value, [s.id]: s }
      }),
      api.onGatewayHostKeyNew(evt => {
        // 同一条网关重复推（重连时再遇见）只留最新一份，否则弹窗会叠
        pendingHostKeys.value = [
          ...pendingHostKeys.value.filter(p => p.id !== evt.id),
          evt,
        ]
      }),
    )
    const [list, all] = await Promise.all([api.gatewayList(), api.gatewayStatusAll()])
    gateways.value = list
    const map: Record<string, GatewayStatus> = {}
    for (const s of all) map[s.id] = s
    statuses.value = map
    loaded.value = true
  }

  function dispose(): void {
    unsubscribers.forEach(u => u())
    unsubscribers = []
  }

  async function save(rec: GatewayRecord): Promise<GatewayRecord> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const saved = await api.gatewaySave(rec)
    await refresh()
    return saved
  }

  async function remove(id: string): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    await api.gatewayDelete(id)
    const map = { ...statuses.value }
    delete map[id]
    statuses.value = map
    await refresh()
  }

  async function connect(id: string): Promise<GatewayStatus> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const s = await api.gatewayConnect(id)
    statuses.value = { ...statuses.value, [s.id]: s }
    return s
  }

  async function disconnect(id: string): Promise<GatewayStatus> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const s = await api.gatewayDisconnect(id)
    statuses.value = { ...statuses.value, [s.id]: s }
    return s
  }

  /** 确认 TOFU：指纹落库后该条重新连接；取消则留在列表里等用户显式再试 */
  async function trustHostKey(id: string, spki: string): Promise<void> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    await api.gatewayTrustHostKey(id, spki)
    pendingHostKeys.value = pendingHostKeys.value.filter(p => !(p.id === id && p.spki === spki))
  }

  function dismissHostKey(id: string, spki: string): void {
    pendingHostKeys.value = pendingHostKeys.value.filter(p => !(p.id === id && p.spki === spki))
  }

  async function channels(id: string): Promise<GatewayChannelRow[]> {
    const api = window.electronAPI
    if (!api) return []
    return api.gatewayChannels(id)
  }

  return {
    gateways,
    statuses,
    pendingHostKeys,
    loaded,
    onlineCount,
    onlineNames,
    stateOf,
    statusOf,
    init,
    refresh,
    dispose,
    save,
    remove,
    connect,
    disconnect,
    trustHostKey,
    dismissHostKey,
    channels,
  }
})
