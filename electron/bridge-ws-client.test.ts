/**
 * S0-05 回归：res 帧投递失败时回收 ssh_connect 建出来的孤儿会话。
 *
 * 链路：后端下发 ssh_connect → 主进程真的建了会话 → res 帧因 socket 已死而
 * 蒸发 → 后端只看到 WS_LOST，永远拿不到 sessionId ⇒ 一条健康、无人引用、界面上
 * 看不见的会话常驻（keepaliveInterval 让远端不回收它，destroySession 只在 TCP
 * close 时触发）。累计到 SESSION_LIMIT=64 后**所有** connect 抛错，含用户自己的
 * 交互式终端。原实现 sendJson 静默 return，连日志都没有。
 */
import { describe, it, expect, vi, type Mock } from 'vitest'
import { BridgeWsClient } from './bridge-ws-client'

type Priv = {
  ws: unknown
  handleReq: (id: unknown, op: string, payload: Record<string, unknown>) => Promise<void>
  sendJson: (obj: unknown) => boolean
}

function client(dispatch: Mock): { c: BridgeWsClient; p: Priv } {
  const c = new BridgeWsClient(dispatch)
  return { c, p: c as unknown as Priv }
}

describe('res 投递失败的补偿（S0-05）', () => {
  it('socket 已死 ⇒ ssh_connect 的会话被立即 disconnect 回收', async () => {
    const dispatch = vi.fn().mockResolvedValue({ status: 200, body: undefined })
    const { p } = client(dispatch)
    p.ws = { readyState: 3, send: vi.fn() } // WS_CLOSING，非 OPEN
    dispatch.mockImplementation(async (op: string) =>
      op === 'ssh_connect'
        ? { status: 200, body: { success: true, sessionId: 'sftp-7', name: 'hoppo' } }
        : { status: 200, body: { success: true } }
    )

    await p.handleReq(1, 'ssh_connect', { connectionId: 'conn-1' })

    expect(dispatch).toHaveBeenCalledWith('ssh_disconnect', { sessionId: 'sftp-7' })
  })

  it('socket 正常 ⇒ 不回收（否则会把刚交给后端的会话杀掉）', async () => {
    const sent: string[] = []
    const dispatch = vi.fn().mockResolvedValue({ status: 200, body: undefined })
    const { p } = client(dispatch)
    p.ws = { readyState: 1, send: (s: string) => sent.push(s) }
    dispatch.mockImplementation(async (op: string) =>
      op === 'ssh_connect'
        ? { status: 200, body: { success: true, sessionId: 'sftp-8' } }
        : { status: 200, body: { success: true } }
    )

    await p.handleReq(2, 'ssh_connect', { connectionId: 'conn-1' })

    expect(sent).toHaveLength(1)
    expect(dispatch).toHaveBeenCalledTimes(1)
    expect(dispatch).not.toHaveBeenCalledWith('ssh_disconnect', expect.anything())
  })

  it('send 抛错也算未投递 ⇒ 同样回收', async () => {
    const dispatch = vi.fn().mockImplementation(async (op: string) =>
      op === 'ssh_connect'
        ? { status: 200, body: { success: true, sessionId: 'sftp-9' } }
        : { status: 200, body: { success: true } }
    )
    const { p } = client(dispatch)
    p.ws = { readyState: 1, send: () => { throw new Error('socket closed') } }

    await p.handleReq(3, 'ssh_connect', { connectionId: 'conn-1' })

    expect(dispatch).toHaveBeenCalledWith('ssh_disconnect', { sessionId: 'sftp-9' })
  })

  it('只对 ssh_connect 且 200 补偿：exec/disconnect/失败响应都不动', async () => {
    const calls: string[] = []
    const dispatch = vi.fn().mockImplementation(async (op: string) => {
      calls.push(op)
      return { status: 500, body: { success: false, error: 'boom', code: 'SESSION_NOT_FOUND' } }
    })
    const { p } = client(dispatch)
    p.ws = null

    await p.handleReq(4, 'ssh_exec', { sshSessionId: 'ssh-1', command: 'ls' })
    expect(calls).toEqual(['ssh_exec'])

    // 500 的 ssh_connect 没有新会话可回收
    calls.length = 0
    dispatch.mockImplementation(async (op: string) => {
      calls.push(op)
      return { status: 500, body: { success: false, error: 'boom' } }
    })
    await p.handleReq(5, 'ssh_connect', { connectionId: 'conn-1' })
    expect(calls).toEqual(['ssh_connect'])
  })

  it('补偿自身抛错不外溢（res 已经发不出去，绝不能再把 handleReq 炸穿）', async () => {
    const dispatch = vi.fn().mockImplementation(async (op: string) => {
      if (op === 'ssh_connect') return { status: 200, body: { success: true, sessionId: 'sftp-10' } }
      throw new Error('disconnect failed')
    })
    const { p } = client(dispatch)
    p.ws = null
    await expect(p.handleReq(6, 'ssh_connect', {})).resolves.toBeUndefined()
  })

  it('sendJson 的返回值即投递结果（静默 return 的旧形态不可回退）', () => {
    const { p } = client(vi.fn())
    p.ws = null
    expect(p.sendJson({ type: 'res' })).toBe(false)
    p.ws = { readyState: 1, send: vi.fn() }
    expect(p.sendJson({ type: 'res' })).toBe(true)
  })
})
