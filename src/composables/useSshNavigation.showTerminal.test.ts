import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSSHStore, type TerminalSession } from '@/stores/ssh'

const { useSshNavigation } = await import('@/composables/useSshNavigation')

const session = (id: string): TerminalSession => ({
  id,
  name: id,
  type: 'local',
  shell: 'PowerShell',
  createdAt: Date.now(),
})

function mount(sessions: TerminalSession[], activeId = '') {
  setActivePinia(createPinia())
  const ssh = useSSHStore()
  ssh.sessions = sessions
  ssh.activeSessionId = activeId
  const switchSession = vi.spyOn(ssh, 'switchSession')
  const nav = useSshNavigation(() => {}, {})
  return { nav, switchSession }
}

describe('showTerminal：顶栏终端入口', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('没有会话时落到远程管理页（终端默认页），不伪造会话', () => {
    const { nav, switchSession } = mount([])
    nav.showTerminal()
    expect(nav.activeNavPage.value).toBe('remote-mgr')
    expect(nav.homeTabActive.value).toBe(false)
    expect(nav.terminalVisible.value).toBe(false)
    expect(switchSession).not.toHaveBeenCalled()
  })

  it('从远程管理页新建本地终端时收回终端页，避免管理页盖住新终端', async () => {
    const { nav } = mount([])
    nav.toggleRemoteManager()
    expect(nav.activeNavPage.value).toBe('remote-mgr')
    vi.spyOn(useSSHStore(), 'createLocalTerminal').mockResolvedValue('local-1')
    await nav.openLocalShell('PowerShell')
    expect(nav.activeNavPage.value).toBe('home')
    expect(nav.terminalVisible.value).toBe(true)
  })

  it('有活动会话时回到该会话', () => {
    const { nav, switchSession } = mount([session('s1'), session('s2')], 's2')
    nav.showTerminal()
    expect(nav.terminalVisible.value).toBe(true)
    expect(switchSession).toHaveBeenCalledWith('s2')
  })

  it('有会话但没有活动会话时落到第一个', () => {
    const { nav, switchSession } = mount([session('s1'), session('s2')])
    nav.showTerminal()
    expect(nav.terminalVisible.value).toBe(true)
    expect(switchSession).toHaveBeenCalledWith('s1')
  })

  it('从独立功能页进入终端时把导航页收回 home', () => {
    const { nav } = mount([session('s1')], 's1')
    nav.toggleDocker()
    expect(nav.activeNavPage.value).toBe('docker')
    nav.showTerminal()
    expect(nav.activeNavPage.value).toBe('home')
    expect(nav.terminalVisible.value).toBe(true)
  })
})
