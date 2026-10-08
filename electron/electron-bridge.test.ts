/**
 * S0-09 / S0-11 回归：桥接这条腿不再销毁 SSHError 的分类。
 *
 * 症状：dispatch 的 catch-all 把任意失败折成同一句「请求处理失败，请稍后重试」，
 * 而渲染层那条腿（main.ts handleTrusted → toIpcTransport）保住了 code ——
 * 同一个错误两条缝两种保真度。后端因此分不清「会话没了（可 ssh_connect 救回）」
 * 「命令合法超时」「会话数触顶」。
 *
 * 同时钉住 S0-11：ping 帧由 bridge_relay 就地应答，dispatch 没有也不该有这一支
 * （原先 docstring 与 BridgeOp 都声明了一个不存在的 ping op）。
 */
import { describe, it, expect, vi, type Mock } from 'vitest'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: {
    getPath: () => '/tmp/ckun-agent-ssh-test',
    getAppPath: () => '/tmp/ckun-agent-ssh-test',
  },
}))
// 桥接日志走 fs.appendFile，测试里不往磁盘写
vi.mock('fs', () => ({
  appendFile: (_p: string, _d: string, cb: () => void) => cb && cb(),
}))

import { ElectronBridge } from './electron-bridge'
import { SSHError } from './ssh-error'

function bridgeWith(exec: Mock): ElectronBridge {
  const sshManager = {
    execCommandWithExitCode: exec,
    connectSftp: vi.fn(),
    disconnect: vi.fn(),
  }
  return new ElectronBridge({ sshManager: sshManager as never, sshStore: undefined })
}

describe('dispatch 的失败保真度（S0-09）', () => {
  it('已分类的 SSHError 带回 code 与共享文案表的中文串', async () => {
    const exec = vi.fn().mockRejectedValue(new SSHError('SESSION_NOT_FOUND'))
    const r = await bridgeWith(exec).dispatch('ssh_exec', { sshSessionId: 'ssh-1', command: 'ls' })
    expect(r.status).toBe(500)
    const body = r.body as { success: boolean; error: string; code?: string; retryable?: boolean }
    expect(body.code).toBe('SESSION_NOT_FOUND')
    expect(body.error).toBe('SSH 会话不存在或已断开')
    expect(body.success).toBe(false)
  })

  it('超时类把 detail 一起渲染（「30秒」是用户判断的依据）', async () => {
    const exec = vi.fn().mockRejectedValue(new SSHError('EXEC_TIMEOUT', { detail: '30秒' }))
    const body = await bridgeWith(exec).dispatch('ssh_exec', { sshSessionId: 'ssh-1', command: 'sleep 999' })
      .then(x => x.body as Record<string, unknown>)
    expect(body.code).toBe('EXEC_TIMEOUT')
    expect(body.error).toContain('30秒')
  })

  it('未分类错误仍回通用句，且不带 code 字段（不把英文技术串端给用户）', async () => {
    const exec = vi.fn().mockRejectedValue(new Error('getaddrinfo ENOTFOUND hoppo'))
    const body = await bridgeWith(exec).dispatch('ssh_exec', { sshSessionId: 'ssh-1', command: 'ls' })
      .then(x => x.body as Record<string, unknown>)
    expect(body.error).toBe('请求处理失败，请稍后重试')
    expect('code' in body).toBe(false)
  })

  it('UNKNOWN 码：code 字段留着（机器可读），文案仍用通用句', async () => {
    const exec = vi.fn().mockRejectedValue(new SSHError('UNKNOWN', { detail: 'raw technical text' }))
    const body = await bridgeWith(exec).dispatch('ssh_exec', { sshSessionId: 'ssh-1', command: 'ls' })
      .then(x => x.body as Record<string, unknown>)
    expect(body.code).toBe('UNKNOWN')
    expect(body.error).toBe('请求处理失败，请稍后重试')
    expect(JSON.stringify(body)).not.toContain('getaddrinfo')
  })

  it('陌生 op 走 404 而不是被当作 ping 执行（S0-11）', async () => {
    const r = await bridgeWith(vi.fn()).dispatch('ping' as never, {})
    expect(r.status).toBe(404)
  })
})

/**
 * S0-06：`electron_bridge.py` 的 docstring 一直写着「关闭由 connect_ssh 建立的 SSH
 * 会话」，而 dispatch 只验 sessionId 是非空字符串 —— 承诺没有机制承载。
 */
describe('ssh_disconnect 的所有权（S0-06）', () => {
  function harness() {
    const owned = new Set<string>()
    const disconnect = vi.fn()
    const connectSftp = vi.fn(async () => ({ id: 'sftp-ffff0000-7', name: 'web-01' }))
    const sshStore = {
      list: async () => [{ id: 'c1', name: 'web-01', host: 'h', port: 22, username: 'u', authType: 'key' }],
    }
    const bridge = new ElectronBridge({
      sshManager: {
        execCommandWithExitCode: vi.fn(),
        connectSftp,
        disconnect,
        markBridgeSession: (id: string) => owned.add(id),
        isBridgeSession: (id: string) => owned.has(id),
      } as never,
      sshStore: sshStore as never,
    })
    return { bridge, disconnect, connectSftp, owned }
  }

  it('不是桥接建的会话：403 且一行拆除代码都不执行', async () => {
    const { bridge, disconnect } = harness()
    const r = await bridge.dispatch('ssh_disconnect', { sessionId: 'ssh-ffff0000-1' })
    expect(r.status).toBe(403)
    expect(disconnect).not.toHaveBeenCalled()
  })

  it('connect_ssh 建的会话被登记，因此随后的 disconnect 合法', async () => {
    const { bridge, disconnect, owned } = harness()
    const conn = await bridge.dispatch('ssh_connect', { connectionId: 'c1' })
    expect(conn.status).toBe(200)
    expect(owned.has('sftp-ffff0000-7')).toBe(true)

    const r = await bridge.dispatch('ssh_disconnect', { sessionId: 'sftp-ffff0000-7' })
    expect(r.status).toBe(200)
    expect(disconnect).toHaveBeenCalledWith('sftp-ffff0000-7')
  })
})
