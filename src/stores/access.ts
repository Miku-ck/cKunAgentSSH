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
  /**
   * 已经由主进程推送落过的网关 id。statusAll 的快照是「请求那一刻」的视图，
   * 到达时可能已经晚于一次推送 —— 只补没推过的 id，不覆盖推过的，否则「连接中」会被印回 idle
   * 且不会再有第二次推送。
   */
  const pushedIds = new Set<string>()

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

  /** @returns true = 真的读到了。false = IPC 失败，调用方必须说破，不能让它看起来像「库里是空的」 */
  async function refresh(): Promise<boolean> {
    const api = window.electronAPI
    if (!api) return false
    try {
      gateways.value = await api.gatewayList()
      // 读到空列表也算读到了：否则随后判空态会把「确实是空的」说成「没读到」
      loaded.value = true
      return true
    } catch {
      return false
    }
  }

  /** 在 SshView onMounted 调用（组件常驻，订阅即全局存活） */
  async function init(): Promise<boolean> {
    const api = window.electronAPI
    if (!api) return false
    if (!unsubscribers.length) {
      unsubscribers.push(
        api.onGatewayStatus(s => {
          pushedIds.add(s.id)
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
    }
    try {
      const [list, all] = await Promise.all([api.gatewayList(), api.gatewayStatusAll()])
      gateways.value = list
      const map = { ...statuses.value }
      for (const s of all) {
        if (!pushedIds.has(s.id)) map[s.id] = s
      }
      statuses.value = map
      loaded.value = true
      return true
    } catch {
      // 读不到就维持 loaded=false：空态由 UI 决定怎么说，这里不把失败洗成空
      return false
    }
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
    pushedIds.delete(id)
    statuses.value = map
    // 待确认指纹跟着记录走：主进程的 pendingSpki 会随 drop() 清，但渲染层这一份
    // 是本地数组，不清就留下一张指向已删网关的确认弹窗（pendingHostKeys[0] 即弹），
    // 用户点「信任并连接」只会对着一个不存在的 id 报错。
    pendingHostKeys.value = pendingHostKeys.value.filter(p => p.id !== id)
    await refresh()
  }

  async function connect(id: string): Promise<GatewayStatus> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const s = await api.gatewayConnect(id)
    pushedIds.add(s.id)
    statuses.value = { ...statuses.value, [s.id]: s }
    return s
  }

  async function disconnect(id: string): Promise<GatewayStatus> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const s = await api.gatewayDisconnect(id)
    pushedIds.add(s.id)
    statuses.value = { ...statuses.value, [s.id]: s }
    return s
  }

  /** 确认 TOFU：主进程校验过「本次握手实际收到的那份」才落库，之后该条重新连接 */
  async function trustHostKey(id: string, spki: string): Promise<void> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    await api.gatewayTrustHostKey(id, spki)
    pendingHostKeys.value = pendingHostKeys.value.filter(p => !(p.id === id && p.spki === spki))
  }

  /**
   * 取消确认：除了收弹窗，还要让主进程把它手里那份待确认指纹清掉。
   * 只出队的话「取消」等于什么都没撤销 —— 下次拨号到同一个 pendingSpki 判定路径，
   * 用户会再看到一次确认请求，而那份指纹并没有被拒绝过。
   * 注意：指纹落库只发生在 trustHostKey（用户点头）之后，这里不涉及库。
   */
  async function dismissHostKey(id: string, spki: string): Promise<void> {
    pendingHostKeys.value = pendingHostKeys.value.filter(p => !(p.id === id && p.spki === spki))
    await window.electronAPI?.gatewayHostKeyReject(id)
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
    save,
    remove,
    connect,
    disconnect,
    trustHostKey,
    dismissHostKey,
    channels,
  }
})
