/**
 * S4-04 / S0-18 回归：自动重连窗口对「确定性失败」的处理。
 *
 * 判据来自 ssh-error-core 的 isRetryable —— 认证失败、主机密钥变更这类
 * 必须用户改配置才能恢复的错误，不再跑满 12 次退避（约 4~5 分钟）：
 * 认证类连撞 12 次会踩 sshd MaxAuthTries 把源 IP 关进惩罚窗口，
 * 主机密钥变更会堆出 12 条「疑似中间人攻击」toast。
 * 同时 gave-up 必须带上已分类的 errorCode/errorMessage（S0-18），
 * 否则 UI 只能对所有原因说同一句话。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: {
    getPath: () => '/tmp/ckun-agent-ssh-test',
    getAppPath: () => '/tmp/ckun-agent-ssh-test',
  },
}))

import { SSHManager, type SSHConnectionConfig } from './ssh-manager'
import { SSHError } from './ssh-error'

const ID = 'ssh-1'
const CONNECTION_ID = 'conn-1'

interface Harness {
  mgr: SSHManager
  sent: Array<{ channel: string; data: Record<string, unknown> }>
  schedule: ReturnType<typeof vi.spyOn>
  doConnect: ReturnType<typeof vi.fn>
  run: (attempt: number) => Promise<void>
  entry: () => Record<string, unknown> | undefined
}

function harness(): Harness {
  const sent: Harness['sent'] = []
  const win = {
    isDestroyed: () => false,
    webContents: { send: (channel: string, data: Record<string, unknown>) => sent.push({ channel, data }) },
  }
  const mgr = new SSHManager()
  // setWindow 形参声明为 BrowserWindow，测试只用到 send/isDestroyed 两个成员
  mgr.setWindow(win as never)
  const config: SSHConnectionConfig = {
    id: CONNECTION_ID,
    name: 'test',
    host: '10.0.0.1',
    port: 22,
    username: 'root',
    authType: 'password',
    password: 'x',
  }
  mgr.setConfigProvider(async () => config)
  const anyMgr = mgr as unknown as {
    reconnects: Map<string, Record<string, unknown>>
    doConnect: (...args: unknown[]) => Promise<unknown>
    scheduleReconnectAttempt: (id: string, attempt: number) => void
    runReconnectAttempt: (id: string, attempt: number) => Promise<void>
  }
  anyMgr.reconnects.set(ID, {
    connectionId: CONNECTION_ID,
    generation: 1,
    timer: null,
    inFlight: false,
    cols: 80,
    rows: 24,
  })
  const doConnect = vi.fn()
  anyMgr.doConnect = doConnect
  const schedule = vi.spyOn(anyMgr, 'scheduleReconnectAttempt').mockImplementation(() => {})
  return {
    mgr,
    sent,
    schedule,
    doConnect,
    run: (attempt: number) => anyMgr.runReconnectAttempt(ID, attempt),
    entry: () => anyMgr.reconnects.get(ID),
  }
}

/** 只取重连状态事件 */
function reconnectEvents(sent: Harness['sent']) {
  return sent.filter((s) => s.channel === 'ssh:reconnect').map((s) => s.data)
}

/** 只取 gave-up 事件（retry/reconnecting 阶段的排程通知不计入） */
function gaveUpEvents(sent: Harness['sent']) {
  return reconnectEvents(sent).filter((d) => d.phase === 'gave-up')
}

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

describe('自动重连的错误分类', () => {
  it('不可重试错误一次即收摊，并带上已分类原因', async () => {
    const h = harness()
    h.doConnect.mockRejectedValue(new SSHError('AUTH_PASSWORD'))

    await h.run(1)

    const events = gaveUpEvents(h.sent)
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      id: ID,
      phase: 'gave-up',
      reason: 'not-retryable',
      errorCode: 'AUTH_PASSWORD',
    })
    expect(typeof events[0].errorMessage).toBe('string')
    expect(h.schedule).not.toHaveBeenCalled()
    // 条目保留：手动「重连」按钮据 id 取条目，删掉就等于按钮失效
    expect(h.entry()).toBeDefined()
    expect(h.entry()?.inFlight).toBe(false)
  })

  it('主机密钥变更同样不进重连窗口（toast 不再堆 12 条）', async () => {
    const h = harness()
    h.doConnect.mockRejectedValue(new SSHError('HOST_KEY_MISMATCH'))

    await h.run(3)

    const events = gaveUpEvents(h.sent)
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({ reason: 'not-retryable', errorCode: 'HOST_KEY_MISMATCH' })
    expect(h.schedule).not.toHaveBeenCalled()
  })

  it('可重试错误在未耗尽时继续排下一次', async () => {
    const h = harness()
    h.doConnect.mockRejectedValue(new SSHError('CONN_REFUSED'))

    await h.run(2)

    expect(gaveUpEvents(h.sent)).toHaveLength(0)
    expect(h.schedule).toHaveBeenCalledWith(ID, 3)
  })

  it('耗尽时给 exhausted 而不是丢掉分类结果', async () => {
    const h = harness()
    h.doConnect.mockRejectedValue(new SSHError('CONN_TIMEOUT'))

    await h.run(12)

    const events = gaveUpEvents(h.sent)
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      phase: 'gave-up',
      reason: 'exhausted',
      errorCode: 'CONN_TIMEOUT',
    })
    expect(h.entry()).toBeDefined()
  })

  it('未分类成 SSHError 的错误按可重试处理（保守继续退避）', async () => {
    const h = harness()
    h.doConnect.mockRejectedValue(new Error('boom'))

    await h.run(1)

    expect(h.schedule).toHaveBeenCalledWith(ID, 2)
    expect(gaveUpEvents(h.sent)).toHaveLength(0)
  })
})

describe('断连→重连后的 SFTP 子系统重开（S15-04）', () => {
  it('destroySession 在 closeSftp 之前把「断开前有 SFTP」记进重连条目', () => {
    const h = harness()
    const anyMgr = h.mgr as unknown as {
      sessions: Map<string, { id: string; client: unknown }>
      sftpSessions: Map<string, unknown>
      closeSftp: ReturnType<typeof vi.spyOn>
      startAutoReconnect: ReturnType<typeof vi.spyOn>
      destroySession: (id: string, client?: unknown) => void
    }
    const client = {}
    anyMgr.sessions.set(ID, { id: ID, client })
    anyMgr.sftpSessions.set(ID, { fake: 'sftp' })
    const closeSftp = vi.spyOn(anyMgr, 'closeSftp').mockImplementation(() => {})
    // 条目存在 = 意外断开会触发自动重连排程，本用例只验 sftpOpen 标记， mock 掉排程
    vi.spyOn(anyMgr, 'startAutoReconnect').mockImplementation(() => {})

    anyMgr.destroySession(ID, client)

    // 映射必须已被读取（记录发生在 closeSftp 真删之前——这里 mock 掉真删，
    // 用条目上的标记证明读取时机正确）
    expect(closeSftp).toHaveBeenCalledWith(ID)
    expect(h.entry()?.sftpOpen).toBe(true)
  })

  it('断开前没有 SFTP 时不标记，重连成功后也不重开', async () => {
    const h = harness()
    const openSftp = vi.spyOn(h.mgr as unknown as { openSftp: (id: string) => Promise<void> }, 'openSftp')
    h.doConnect.mockResolvedValue({ id: ID, name: 'test', client: {} })

    await h.run(1)

    expect(h.entry()?.sftpOpen).toBeFalsy()
    expect(openSftp).not.toHaveBeenCalled()
  })

  it('重连成功后按条目标记自动重开 SFTP', async () => {
    const h = harness()
    const openSftp = vi.spyOn(h.mgr as unknown as { openSftp: (id: string) => Promise<void> }, 'openSftp')
    h.doConnect.mockResolvedValue({ id: ID, name: 'test', client: {} })
    h.entry()!.sftpOpen = true

    await h.run(1)

    expect(openSftp).toHaveBeenCalledWith(ID)
    // success 事件先于重发出栈（重开失败也不能污染重连成功语义，见 catch）
    expect(reconnectEvents(h.sent).some((d) => d.phase === 'success')).toBe(true)
  })

  it('SFTP 重开抛错不阻断重连成功路径', async () => {
    const h = harness()
    const openSftp = vi.spyOn(h.mgr as unknown as { openSftp: (id: string) => Promise<void> }, 'openSftp')
      .mockRejectedValue(new Error('sftp init fail'))
    h.doConnect.mockResolvedValue({ id: ID, name: 'test', client: {} })
    h.entry()!.sftpOpen = true

    await expect(h.run(1)).resolves.toBeUndefined()
    expect(openSftp).toHaveBeenCalledWith(ID)
    expect(gaveUpEvents(h.sent)).toHaveLength(0)
  })
})

describe('write 的会话缺失行为（S4-06）', () => {
  it('会话不在池里时抛 SESSION_NOT_FOUND —— 渲染层的 .catch 由此可达', () => {
    const h = harness()
    expect(() => h.mgr.write('ssh-404', 'ls\n')).toThrowError(/不存在或已断开/)
  })

  it('会话在池里时照常写入（抛错只针对缺失）', () => {
    const h = harness()
    const stream = { write: vi.fn() }
    ;(h.mgr as unknown as { sessions: Map<string, unknown> }).sessions.set(ID, { stream })
    h.mgr.write(ID, 'ls\n')
    expect(stream.write).toHaveBeenCalledWith('ls\n')
  })
})
