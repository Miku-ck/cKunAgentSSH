/**
 * S0-12 回归：ssh2 `client.connect()` 的**同步抛出**必须过 SSHError.from。
 *
 * ssh2 在建立 socket 之前就会因配置问题同步 throw（client.js:203/243/261 ——
 * 私钥加密但没给 passphrase 时是
 * `Cannot parse privateKey: Encrypted private OpenSSH key detected, but no passphrase given`）。
 * 原实现把它放在 Promise executor 的最后一句且无 try/catch ⇒ executor throw 被
 * Promise 构造器转成 rejection，但带的是**裸 Error**：不进 classifyRawError、
 * 不进 handleTrusted 的 __sshError 序列化，到渲染端再被 IPC 信封（S0-13）
 * 二次吞掉 ⇒ 用户只看到「服务暂时不可用」，而 AUTH_PASSPHRASE 那条专门写的
 * 「私钥保护密码错误或缺失」永不出现。三个 connect 站点现在共用同一口径。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: {
    getPath: () => '/tmp/ckun-agent-ssh-test',
    getAppPath: () => '/tmp/ckun-agent-ssh-test',
  },
}))

// ssh-manager 只在运行期动态 import('ssh2')（启动懒加载优化），类型走 import type，
// 因此这里替换成会同步抛出的替身即可覆盖三个 connect 站点。
vi.mock('ssh2', () => ({
  Client: class {
    on(): this { return this }
    end(): void { /* 替身无 socket */ }
    connect(): void {
      throw new Error(
        'Cannot parse privateKey: Encrypted private OpenSSH key detected, but no passphrase given'
      )
    }
  },
}))

import { SSHManager, type SSHConnectionConfig } from './ssh-manager'
import { SSHError } from './ssh-error'

function config(): SSHConnectionConfig {
  return {
    id: 'conn-1',
    name: 'test',
    host: '10.0.0.1',
    port: 22,
    username: 'root',
    authType: 'privateKey',
    readyTimeout: 1,
  }
}

function manager(): SSHManager {
  const mgr = new SSHManager()
  const win = { isDestroyed: () => false, webContents: { send: () => { /* 断言不依赖事件 */ } } }
  mgr.setWindow(win as never)
  return mgr
}

async function rejectionOf(p: Promise<unknown>): Promise<unknown> {
  return p.then(() => { throw new Error('expected rejection') }, (err: unknown) => err)
}

describe('connect() 同步抛出的分类（S0-12）', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('connectSftp（doConnect 站点）抛 SSHError 且归类 AUTH_PASSPHRASE', async () => {
    const err = await rejectionOf(manager().connectSftp(config()))
    expect(err).toBeInstanceOf(SSHError)
    expect((err as SSHError).code).toBe('AUTH_PASSPHRASE')
    expect((err as SSHError).message).toBe('私钥保护密码（passphrase）错误或缺失')
  })

  it('openRawClient（隧道站点）同样抛 SSHError', async () => {
    const err = await rejectionOf(manager().openRawClient(config()))
    expect(err).toBeInstanceOf(SSHError)
    expect((err as SSHError).code).toBe('AUTH_PASSPHRASE')
  })

  it('失败连接不留会话（不占 64 上限名额）', async () => {
    const mgr = manager()
    await rejectionOf(mgr.connectSftp(config()))
    const sessions = (mgr as unknown as { sessions: Map<string, unknown> }).sessions
    expect(sessions.size).toBe(0)
  })

  it('testConnection 早已有的守卫与另两站产出同一文案', async () => {
    const res = await manager().testConnection(config())
    expect(res.ok).toBe(false)
    expect(res.message).toBe('私钥保护密码（passphrase）错误或缺失')
  })
})
