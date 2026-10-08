/**
 * S4-05 回归：同一连接在途期间的互斥。
 *
 * 原实现入口只看 2s 时间窗（CONNECT_DEBOUNCE_MS），不看「这一笔是否还在跑」：
 * 握手慢/认证慢的主机上（readyTimeout 默认 15s），等待期 Quick Start 卡片不渲染
 * connecting 态、毫无反馈，>2s 的第二次点击会真的开出第二条会话
 * （store.connectRemote 两次都 push，MAX_SESSIONS=20 挡不住 2 条）。
 * 附带修掉 releaseConnectLock 的后半边：debounce 分支过去会清掉自己那笔仍在跑的锁，
 * 让仍在连接的那一行重新可点。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSSHStore, type SSHConnection } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'

const { useSshNavigation } = await import('@/composables/useSshNavigation')

const CONN_A = { id: 'conn-1', name: 'web-03', host: '10.0.0.1' } as SSHConnection
const CONN_B = { id: 'conn-2', name: 'db-01', host: '10.0.0.2' } as SSHConnection

/** 防抖窗口之外、但首连仍未结束的时间点（readyTimeout 默认 15s，这里取 2.5s） */
const PAST_DEBOUNCE_MS = 2500

interface Mount {
  nav: ReturnType<typeof useSshNavigation>
  /** 每次 store.connectRemote 调用对应的 settle 函数，按调用顺序 */
  settles: Array<(value: string) => void>
  toastInfo: ReturnType<typeof vi.fn>
}

function mount(): Mount {
  setActivePinia(createPinia())
  const ssh = useSSHStore()
  const settles: Mount['settles'] = []
  vi.spyOn(ssh, 'connectRemote').mockImplementation(() => new Promise<string>((resolve) => {
    settles.push(resolve)
  }))
  const toastInfo = vi.fn()
  vi.spyOn(useToastStore(), 'info').mockImplementation((msg: string) => {
    toastInfo(msg)
    return 0
  })
  return { nav: useSshNavigation(() => {}, {}), settles, toastInfo }
}

describe('connectRemote 的在途互斥', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('过了防抖窗口但首连仍在途时，第二次点击不再开会话并给出提示', async () => {
    const m = mount()
    const first = m.nav.connectRemote(CONN_A)
    expect(m.settles).toHaveLength(1)

    vi.advanceTimersByTime(PAST_DEBOUNCE_MS)
    await m.nav.connectRemote(CONN_A)

    expect(m.settles).toHaveLength(1)
    expect(m.toastInfo).toHaveBeenCalledWith('该主机正在连接中，请稍候')
    // 锁仍归这一笔：转圈不消失，那一行不会重新可点
    expect(m.nav.connecting.value).toBe(true)
    expect(m.nav.pendingConnId.value).toBe(CONN_A.id)

    m.settles[0]('ssh-1')
    await first
    expect(m.nav.pendingConnId.value).toBe('')
  })

  it('解锁后同一连接可以再连（失败 toast 上的「重试」按钮仍可用）', async () => {
    const m = mount()
    const first = m.nav.connectRemote(CONN_A)
    m.settles[0]('ssh-1')
    await first

    vi.advanceTimersByTime(PAST_DEBOUNCE_MS)
    const second = m.nav.connectRemote(CONN_A)
    expect(m.settles).toHaveLength(2)
    m.settles[1]('ssh-2')
    await second
  })

  it('另一台主机的在途锁不会被本次连接的收尾越权清掉', async () => {
    const m = mount()
    const first = m.nav.connectRemote(CONN_A)
    const second = m.nav.connectRemote(CONN_B)
    expect(m.settles).toHaveLength(2)
    expect(m.nav.pendingConnId.value).toBe(CONN_B.id)

    // A 先结束：锁此刻归 B，A 的 finally 不得把 B 的转圈清掉
    m.settles[0]('ssh-1')
    await first
    expect(m.nav.pendingConnId.value).toBe(CONN_B.id)
    expect(m.nav.connecting.value).toBe(true)

    m.settles[1]('ssh-2')
    await second
    expect(m.nav.pendingConnId.value).toBe('')
    expect(m.nav.connecting.value).toBe(false)
  })
})
