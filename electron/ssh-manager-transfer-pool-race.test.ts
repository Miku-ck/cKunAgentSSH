/**
 * 并发 acquire 的传输连接池覆盖回归。
 *
 * `acquireTransferSftp` 在建连前取走池的快照（无池时 `conns = []`），而建连要等一整
 * 个 SSH 握手。两路并发传输同时进来时各自握着一条空数组：先入池的那条被后一条的
 * `transferConns.set` 整体覆盖，从此再也没有引用能拿到它 —— 归还时 leased=false
 * 落进无人持有的对象，空闲回收与拆池（dropAllTransferConns）都遍历不到，
 * 于是每条并发传输在远端留下一只永不关闭的连接（服务端 sshd 计数与 keepalive
 * 定时器一起漏），并发越多漏越多，且全程不报错。
 * 修法：握手后重读池，把新连接并进"当下活着的那份"数组。
 */
import { describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: {
    getPath: () => '/tmp/ckun-agent-ssh-test',
    getAppPath: () => '/tmp/ckun-agent-ssh-test',
  },
}))
vi.mock('ssh2', () => ({ Client: class {} }))

import { SSHManager } from './ssh-manager'

/** 两条并发 acquire：createTransferConn 里的 await 让两者各自握着握手前的池快照 */
async function raceTwoAcquires() {
  const mgr: any = new SSHManager()
  const ended: string[] = []
  const created: any[] = []
  const makeConn = (compress: boolean) => {
    const conn = {
      client: { end: () => ended.push('client'), on: () => {} },
      sftp: { end: () => ended.push('sftp') },
      name: 'host',
      leased: false,
      dead: false,
      cleanupTimer: null,
      compress,
    }
    created.push(conn)
    return conn
  }
  mgr.sessions.set('ssh-1', { connectionId: 'cfg-1' })
  mgr.createTransferConn = async (_cfgId: string, compress: boolean) => {
    await Promise.resolve() // 让另一路 acquire 也走到"快照仍是空池"的分支
    return makeConn(compress)
  }
  const leases = await Promise.all([
    mgr.acquireTransferSftp('ssh-1', false),
    mgr.acquireTransferSftp('ssh-1', false),
  ])
  return { mgr, ended, created, leases }
}

describe('并发传输各占一条池内连接，互不覆盖', () => {
  it('两条并发 acquire 都在池里（旧形状：后 set 的覆盖前一条，池长 1）', async () => {
    const { mgr, created } = await raceTwoAcquires()
    expect(created).toHaveLength(2)
    const pool = mgr.transferConns.get('cfg-1')
    expect(pool).toHaveLength(2)
    for (const conn of created) expect(pool).toContain(conn)
  })

  it('两条租约彼此独立，不会共用同一条连接', async () => {
    const { leases, created } = await raceTwoAcquires()
    expect(leases[0].sftp).not.toBe(leases[1].sftp)
    expect(created.filter(c => c.leased)).toHaveLength(2)
  })

  it('拆池把两条连接都关掉（被覆盖的那条旧形状永远关不到）', async () => {
    const { mgr, created, ended, leases } = await raceTwoAcquires()
    for (const l of leases) l.release()
    expect(created.every(c => !c.leased)).toBe(true)
    mgr.dropAllTransferConns('cfg-1')
    expect(mgr.transferConns.has('cfg-1')).toBe(false)
    expect(ended, '两只 client + 两只 sftp 全部 end').toHaveLength(4)
  })
})
