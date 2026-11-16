/**
 * AI 专属 SSH 会话的行为守卫。
 *
 * 需求：AI 的 SSH 连接与用户终端隔离 —— 只有用户在 AI 输入框选中连接才建一条
 * **AI 专属**会话。它与用户终端 tab 的区别只有两点，但都是硬契约：
 *   1. 建连**不切换** activeSessionId（不抢用户正在看的终端 tab）；
 *   2. 会话带 `ai` 标记，终端页据此画「AI」徽标并禁输入。
 * 其余（连接配置取凭据、会话上限、lastConnectedAt 同步）与 connectRemote 同源。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSSHStore } from './ssh'

const g = globalThis as unknown as Record<string, unknown>

function conn(id: string) {
  return {
    id, name: 'prod', host: '10.0.0.1', port: 22, username: 'root',
    authType: 'password', tags: [], createdAt: 0, updatedAt: 0,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  g.window = { electronAPI: undefined }
})

describe('connectAiRemote', () => {
  it('建一条带 ai 标记的远程会话，但不切换用户活动 tab', async () => {
    const connect = vi.fn(async (_cfg: Record<string, unknown>) => ({ id: 'ai-1', name: 'root@10.0.0.1' }))
    g.window = { electronAPI: { sshConnect: connect } }
    const ssh = useSSHStore()
    ssh.connections = [conn('c1')] as never
    // 用户正停在一条本地终端上：AI 建连不该把它抢走
    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'

    const id = await ssh.connectAiRemote('c1', 80, 24)

    expect(id).toBe('ai-1')
    expect(ssh.activeSessionId).toBe('l1')
    const s = ssh.sessions.find(x => x.id === 'ai-1')
    expect(s).toBeTruthy()
    expect(s?.type).toBe('remote')
    expect(s?.ai).toBe(true)
    expect(s?.connectionId).toBe('c1')
    expect(s?.shell).toBe('root@10.0.0.1')
    // 凭据由主进程从连接配置取，渲染层只透传配置（与 connectRemote 同口径）
    expect(connect).toHaveBeenCalledTimes(1)
    const arg = connect.mock.calls[0][0]
    expect(arg.id).toBe('c1')
    expect(arg.cols).toBe(80)
    expect(arg.rows).toBe(24)
  })

  it('普通 connectRemote 建的会话不带 ai 标记（用户终端不被误标）', async () => {
    const connect = vi.fn(async () => ({ id: 'u-1', name: 'root@10.0.0.1' }))
    g.window = { electronAPI: { sshConnect: connect } }
    const ssh = useSSHStore()
    ssh.connections = [conn('c1')] as never

    await ssh.connectRemote('c1', 80, 24)

    const s = ssh.sessions.find(x => x.id === 'u-1')
    expect(s?.ai).toBeFalsy()
    // 用户终端建连仍然切到前台（既有行为不变）
    expect(ssh.activeSessionId).toBe('u-1')
  })

  it('连接配置不存在时抛错，不建会话', async () => {
    g.window = { electronAPI: { sshConnect: vi.fn() } }
    const ssh = useSSHStore()
    ssh.connections = []
    await expect(ssh.connectAiRemote('missing', 80, 24)).rejects.toThrow()
    expect(ssh.sessions).toHaveLength(0)
  })

  it('redial 重建 AI 会话时保留 ai 标记（换的是连接，不是归属）', async () => {
    const connect = vi.fn(async () => ({ id: 'ai-2', name: 'root@10.0.0.1' }))
    const disconnect = vi.fn(async () => undefined)
    g.window = { electronAPI: { sshConnect: connect, sshDisconnect: disconnect, sshGetCwd: async () => '', sshExec: vi.fn(async () => ({ stdout: '', stderr: '', exitCode: 0 })) } }
    const ssh = useSSHStore()
    ssh.connections = [conn('c1')] as never
    ssh.sessions = [{ id: 'ai-1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1', connectionId: 'c1', ai: true }]

    const newId = await ssh.redial('ai-1')

    expect(newId).toBe('ai-2')
    const s = ssh.sessions.find(x => x.id === 'ai-2')
    expect(s?.ai).toBe(true)
  })
})