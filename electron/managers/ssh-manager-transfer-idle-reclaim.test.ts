/**
 * 空闲回收排程在"定时器到点时仍有租约"后的失效回归。
 *
 * `scheduleTransferConnCleanup` 把同一个 timer 写到池内**每条**连接上；timer 到点
 * 触发 `dropAllTransferConns` 时若恰有一条在租（早退），其余条目上的 cleanupTimer
 * 引用原实现不清 —— 在租那条归还后 `release()` 再进 `scheduleTransferConnCleanup`，
 * 被 `conns.some(c => c.cleanupTimer)`（陈旧引用）挡住，永不重排：该主机最多 8 条
 * 传输连接（含 keepalive）常驻到 disconnect 为止，全程不报错。
 * 修法：`dropAllTransferConns` 无论是否早退，先把全部 cleanupTimer 引用清空。
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

function makeConn(leased: boolean) {
  return {
    client: { end: () => {} },
    sftp: { end: () => {} },
    name: 'host',
    leased,
    dead: false,
    cleanupTimer: null as NodeJS.Timeout | null,
    compress: false,
  }
}

describe('空闲回收：定时器到点仍有租约后必须能重新排程', () => {
  it('早退分支清空陈旧 cleanupTimer，归还后可重新排程（修复前：永不重排）', () => {
    const mgr: any = new SSHManager()
    const timer = setTimeout(() => {}, 10_000)
    const idle = makeConn(false)
    const leased = makeConn(true)
    idle.cleanupTimer = timer
    leased.cleanupTimer = timer
    mgr.transferConns.set('cfg-1', [idle, leased])

    // timer 到点触发：leased 在租 → 早退（不回收）
    mgr.dropAllTransferConns('cfg-1')
    expect(idle.cleanupTimer).toBeNull()
    expect(leased.cleanupTimer).toBeNull()
    expect(mgr.transferConns.has('cfg-1')).toBe(true)

    // 在租那条归还：不得再被陈旧引用挡住，必须能重新排程
    leased.leased = false
    mgr.scheduleTransferConnCleanup('cfg-1')
    expect(idle.cleanupTimer).not.toBeNull()
    expect(leased.cleanupTimer).not.toBeNull()

    clearTimeout(idle.cleanupTimer!)
  })

  it('无在途租约时到点回收整批（原有语义保持）', () => {
    const mgr: any = new SSHManager()
    const ended: string[] = []
    const a = makeConn(false)
    const b = makeConn(false)
    a.client.end = () => ended.push('client-a')
    a.sftp.end = () => ended.push('sftp-a')
    b.client.end = () => ended.push('client-b')
    b.sftp.end = () => ended.push('sftp-b')
    a.cleanupTimer = setTimeout(() => {}, 10_000)
    mgr.transferConns.set('cfg-1', [a, b])

    mgr.dropAllTransferConns('cfg-1')
    expect(mgr.transferConns.has('cfg-1')).toBe(false)
    expect(ended.sort()).toEqual(['client-a', 'client-b', 'sftp-a', 'sftp-b'])
  })
})
