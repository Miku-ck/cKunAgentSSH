/**
 * safeSend 行为守卫：与改造前 7 处内联 try/catch 守卫逐字等价。
 * 不启动 Electron，用最小 BrowserWindow 替身覆盖正常 / 已销毁 / 空窗口 / send 抛错四种路径。
 */
import { describe, expect, it, vi } from 'vitest'
import { safeSend } from './safe-send'

function fakeWin(opts: { destroyed?: boolean; throws?: boolean } = {}) {
  const send = vi.fn(() => {
    if (opts.throws) throw new Error('EPIPE')
  })
  const win = {
    isDestroyed: () => opts.destroyed ?? false,
    webContents: { send },
  }
  return { win, send }
}

describe('safeSend', () => {
  it('窗口正常时把频道与数据原样透传', () => {
    const { win, send } = fakeWin()
    const data = { id: 't1', transferred: 10 }
    safeSend(win as never, 'sftp:progress', data)
    expect(send).toHaveBeenCalledTimes(1)
    expect(send).toHaveBeenCalledWith('sftp:progress', data)
  })

  it('窗口已销毁时不发送', () => {
    const { win, send } = fakeWin({ destroyed: true })
    expect(() => safeSend(win as never, 'ssh:data', {})).not.toThrow()
    expect(send).not.toHaveBeenCalled()
  })

  it('窗口为 null / undefined 时不抛也不发送', () => {
    expect(() => safeSend(null, 'ssh:data', {})).not.toThrow()
    expect(() => safeSend(undefined, 'ssh:data', {})).not.toThrow()
  })

  it('webContents.send 抛错（EPIPE）时静默忽略，不重抛', () => {
    const { win, send } = fakeWin({ throws: true })
    expect(() => safeSend(win as never, 'ghaccel:download', { id: 'x' })).not.toThrow()
    expect(send).toHaveBeenCalledTimes(1)
  })
})
