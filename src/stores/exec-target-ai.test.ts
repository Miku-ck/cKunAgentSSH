/**
 * AI 专属 SSH 落点的行为守卫（exec-target 的 SSH 分支）。
 *
 * 核心契约（与用户终端隔离）：
 *   - SSH 落点只认 AI 专属会话 aiSessionId，**不再**从用户活动会话反查 ——
 *     用户终端页开着远程 tab，AI 也借不到（kind 不因 remote activeSession 变 ssh）。
 *   - 选中连接 = 建一条 AI 专属会话（connectAiRemote），不切用户活动 tab。
 *   - chip 读数（主机名 / 远程目录）从 AI 专属会话读。
 *   - 取消选择 / 换连接 → 断开旧专属会话。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useExecTargetStore } from './exec-target'
import { useSSHStore } from './ssh'

const g = globalThis as unknown as Record<string, unknown>

function aiSession(id: string, connectionId = 'c1') {
  return { id, name: 'prod', type: 'remote' as const, shell: 'root@10.0.0.1', connectionId, ai: true }
}

beforeEach(() => {
  setActivePinia(createPinia())
  g.window = { electronAPI: undefined }
})

describe('SSH 落点只认 AI 专属会话', () => {
  it('用户终端页的远程活动会话不再让 kind 变 ssh（隔离：AI 借不到用户终端）', () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [{ id: 'r1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r1'
    expect(landing.kind).toBeNull()
  })

  it('选中连接建 AI 专属会话：kind=ssh，且不切用户活动 tab', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'
    const connect = vi.spyOn(ssh, 'connectAiRemote').mockImplementation(async () => {
      ssh.sessions = [...ssh.sessions, aiSession('ai-1')]
      return 'ai-1'
    })

    await landing.chooseSshConnection('c1')

    expect(connect).toHaveBeenCalledWith('c1', 80, 24)
    expect(landing.aiSessionId).toBe('ai-1')
    expect(landing.kind).toBe('ssh')
    // 用户活动 tab 不被抢走
    expect(ssh.activeSessionId).toBe('l1')
  })

  it('AI 会话被关掉后 kind 回落，不臆造 ssh 落点', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    vi.spyOn(ssh, 'connectAiRemote').mockImplementation(async () => {
      ssh.sessions = [...ssh.sessions, aiSession('ai-1')]
      return 'ai-1'
    })
    await landing.chooseSshConnection('c1')
    expect(landing.kind).toBe('ssh')

    ssh.sessions = []
    expect(landing.kind).toBeNull()
    expect(landing.aiSessionId).toBe('')
  })

  it('chip 读数从 AI 专属会话读，不受用户活动会话干扰', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.connections = [{ id: 'c1', name: '生产库', host: '10.0.0.1', port: 22, username: 'root' } as never]
    // 用户活动会话是另一台本地终端：chip 不该读它
    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'
    vi.spyOn(ssh, 'connectAiRemote').mockImplementation(async () => {
      ssh.sessions = [...ssh.sessions, aiSession('ai-1')]
      return 'ai-1'
    })
    await landing.chooseSshConnection('c1')

    expect(landing.activeRemoteLabel).toBe('生产库')
    ssh.setTerminalCwd('ai-1', '/home/ckun/www/app')
    expect(landing.activeDirLabel).toBe('app')
    expect(landing.activeDirPath).toBe('/home/ckun/www/app')
  })
})

describe('取消选择与换连接', () => {
  it('clearSsh 断开 AI 专属会话并清空落点', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    vi.spyOn(ssh, 'connectAiRemote').mockImplementation(async () => {
      ssh.sessions = [...ssh.sessions, aiSession('ai-1')]
      return 'ai-1'
    })
    const close = vi.spyOn(ssh, 'closeSession').mockResolvedValue(undefined)
    await landing.chooseSshConnection('c1')

    await landing.clearSsh()

    expect(close).toHaveBeenCalledWith('ai-1')
    expect(landing.aiSessionId).toBe('')
    expect(landing.kind).toBeNull()
  })

  it('换连接先断开旧 AI 会话，再建新的（不堆两条专属连接）', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    let n = 0
    vi.spyOn(ssh, 'connectAiRemote').mockImplementation(async (cid: string) => {
      const id = `ai-${++n}`
      ssh.sessions = [...ssh.sessions, aiSession(id, cid)]
      return id
    })
    const close = vi.spyOn(ssh, 'closeSession').mockResolvedValue(undefined)

    await landing.chooseSshConnection('c1')
    await landing.chooseSshConnection('c2')

    expect(close).toHaveBeenCalledWith('ai-1')
    expect(landing.aiSessionId).toBe('ai-2')
    expect(landing.aiConnectionId).toBe('c2')
  })

  it('建连失败把原因写进 error，不留半截落点', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    vi.spyOn(ssh, 'connectAiRemote').mockRejectedValue(new Error('认证失败'))

    await landing.chooseSshConnection('c1')

    expect(landing.error).toContain('认证失败')
    expect(landing.aiSessionId).toBe('')
    expect(landing.kind).toBeNull()
  })

  it('重选当前这条连接是空操作：不拆了重连，免得打断正在跑的 AI', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    const connect = vi.spyOn(ssh, 'connectAiRemote').mockImplementation(async () => {
      ssh.sessions = [...ssh.sessions, aiSession('ai-1')]
      return 'ai-1'
    })
    const close = vi.spyOn(ssh, 'closeSession').mockResolvedValue(undefined)
    await landing.chooseSshConnection('c1')

    await landing.chooseSshConnection('c1')

    expect(connect).toHaveBeenCalledTimes(1)
    expect(close).not.toHaveBeenCalled()
    expect(landing.aiSessionId).toBe('ai-1')
  })
})

describe('选回本地 / 工作树释放 AI 专属会话', () => {
  it('选中 SSH 后再选本地：断开专属会话，kind 回落 local', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    vi.spyOn(ssh, 'connectAiRemote').mockImplementation(async () => {
      ssh.sessions = [...ssh.sessions, aiSession('ai-1')]
      return 'ai-1'
    })
    const close = vi.spyOn(ssh, 'closeSession').mockResolvedValue(undefined)
    vi.spyOn(ssh, 'createLocalTerminal').mockImplementation(async () => {
      ssh.sessions = [...ssh.sessions, { id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
      ssh.activeSessionId = 'l1'
      return 'l1'
    })
    await landing.chooseSshConnection('c1')
    expect(landing.kind).toBe('ssh')

    await landing.chooseLocal()

    expect(close).toHaveBeenCalledWith('ai-1')
    expect(landing.aiSessionId).toBe('')
    expect(landing.kind).toBe('local')
  })

  it('选中 SSH 后再选工作树：断开专属会话，kind 回落 worktree', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    vi.spyOn(ssh, 'connectAiRemote').mockImplementation(async () => {
      ssh.sessions = [...ssh.sessions, aiSession('ai-1')]
      return 'ai-1'
    })
    const close = vi.spyOn(ssh, 'closeSession').mockResolvedValue(undefined)
    vi.spyOn(ssh, 'createLocalTerminal').mockImplementation(async (_shell, cwd) => {
      const id = `wt-${cwd}`
      ssh.sessions = [...ssh.sessions, { id, name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
      ssh.activeSessionId = id
      return id
    })
    await landing.chooseSshConnection('c1')

    await landing.chooseWorktree({ path: '/repo-wt', head: 'abc', branch: 'main', detached: false, bare: false, locked: false, prunable: false, main: false })

    expect(close).toHaveBeenCalledWith('ai-1')
    expect(landing.aiSessionId).toBe('')
    expect(landing.kind).toBe('worktree')
  })
})