/**
 * 陈旧握手续体与 shell 分片解码回归。两条都在 doConnect 内部，而现有的
 * ssh-manager-reconnect.test.ts 把 doConnect 整个替换掉了 —— 结构上看不见这两条。
 *
 * 一、用户连点两次「重连」时，先启动的那次握手可能**后**落地。原实现在 shell 回调里
 * 无条件 `sessions.set(id, …)`，旧代于是把新代挤出会话池；紧接着 runReconnectAttempt
 * 的陈旧分支按身份拆掉"自己刚装的行"，拆的正是新代那条。结果：新连接还在往 ssh:data
 * 吐字，池里却没有这个 id —— 终端能看不能打字（write → SESSION_NOT_FOUND），远端那条
 * 连接谁也都关不掉。
 *
 * 二、`data.toString('utf-8')` 对每个 TCP 分片独立解码，被切在两片之间的三字节汉字会
 * 各产出一个 U+FFFD，写进 xterm 回滚缓冲后不可恢复。exec 通道早已改 StringDecoder，
 * 交互 shell 这条是同一缺陷的最后一个出口。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SSHManager, type SSHConnectionConfig } from './ssh-manager'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: {
    getPath: () => '/tmp/ckun-agent-ssh-test',
    getAppPath: () => '/tmp/ckun-agent-ssh-test',
  },
}))

// ssh2 只在 loadSsh2() 里动态 import；这里换成最小假实现，好让测试自己掌握
// 「哪一代先落地」的时序 —— 这正是本缺陷的触发条件
const fake = vi.hoisted(() => {
  const clients: unknown[] = []

  function mkEmitter() {
    const handlers: Record<string, Array<(...a: unknown[]) => void>> = {}
    return {
      on(ev: string, cb: (...a: unknown[]) => void) {
        ;(handlers[ev] ||= []).push(cb)
        return this
      },
      emit(ev: string, ...a: unknown[]) {
        for (const cb of handlers[ev] || []) cb(...a)
      },
    }
  }

  class FakeClient {
    emitter = mkEmitter()
    shellCb: ((err: null, stream: unknown) => void) | null = null
    stream: unknown = null
    endCalled = 0
    connectArgs: unknown = null
    constructor() {
      clients.push(this)
    }
    connect(cfg: unknown) {
      this.connectArgs = cfg
      return this
    }
    on = this.emitter.on
    emit = this.emitter.emit
    shell(_o: unknown, _e: unknown, cb: (err: null, stream: unknown) => void) {
      this.shellCb = cb
    }
    end() {
      this.endCalled++
    }
    /** 让本次握手的 ready + shell 回调落地（模拟 ssh2 的时序点） */
    land() {
      const stream = Object.assign(mkEmitter(), {
        writes: [] as string[],
        closeCalled: 0,
        write(x: string) {
          this.writes.push(x)
          return true
        },
        close() {
          this.closeCalled++
        },
      })
      this.stream = stream
      this.emit('ready')
      this.shellCb?.(null, stream)
    }
  }

  return { clients, FakeClient }
})

vi.mock('ssh2', () => ({ Client: fake.FakeClient }))

const ID = 'ssh-1'
const baseConfig = {
  id: 'conn-1',
  name: 'test',
  host: '10.0.0.1',
  port: 22,
  username: 'root',
  authType: 'password',
  password: 'x',
} as unknown as SSHConnectionConfig

type AnyMgr = {
  sessions: Map<string, { client: { endCalled: number }; stream: { emit: (e: string, b: Buffer) => void; closeCalled: number } | null }>
  doConnect: (id: string, c: SSHConnectionConfig, shell: boolean, cols?: number, rows?: number) => Promise<unknown>
}

function makeMgr() {
  const sent: Array<{ channel: string; data: Record<string, unknown> }> = []
  const win = {
    isDestroyed: () => false,
    webContents: {
      send: (channel: string, data: Record<string, unknown>) => sent.push({ channel, data }),
    },
  }
  const mgr = new SSHManager()
  mgr.setWindow(win as never)
  mgr.setConfigProvider(async () => baseConfig)
  return { mgr, sent, any: mgr as unknown as AnyMgr }
}

/** doConnect 里有两处 await（loadSsh2 / loadKnownHosts），后者是真 fs I/O，
 * 只排微任务不够 —— 必须让宏任务队列走一轮 */
async function flush(): Promise<void> {
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0))
}

beforeEach(() => {
  fake.clients.length = 0
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

type FakeShape = InstanceType<typeof fake.FakeClient>
function newClient(): FakeShape {
  return fake.clients[fake.clients.length - 1] as unknown as FakeShape
}

describe('陈旧握手不得替换会话池', () => {
  it('后落地的旧代不挤掉新代，且必须自己收口', async () => {
    const { any } = makeMgr()
    const pA = any.doConnect(ID, baseConfig, true, 80, 24)
    await flush()
    const pB = any.doConnect(ID, baseConfig, true, 90, 30)
    await flush()
    expect(fake.clients).toHaveLength(2)
    const [A, B] = fake.clients as unknown as [FakeShape, FakeShape]

    B.land()
    await pB
    expect(any.sessions.get(ID)?.client).toBe(B as never)

    // 旧代后到：原实现到这里就把池改写成 A，随后被陈旧分支连带拆掉
    A.land()
    await pA

    expect(any.sessions.get(ID)?.client, '池中必须仍是新代连接').toBe(B as never)
    expect(B.endCalled, '新代连接不得被拆').toBe(0)
    expect(A.endCalled, '旧代连接应被自己关掉').toBeGreaterThan(0)
    expect(any.sessions.size).toBe(1)
  })

  it('正常单代成功照旧入池（反向锁）', async () => {
    const { any } = makeMgr()
    const p = any.doConnect(ID, baseConfig, true, 80, 24)
    await flush()
    newClient().land()
    await p
    expect(any.sessions.get(ID)?.client).toBe(newClient() as never)
    expect(newClient().endCalled).toBe(0)
  })
})

describe('shell 流按 UTF-8 分片解码', () => {
  it('三字节汉字被切在两片之间时不得产生替换符', async () => {
    const { any, sent } = makeMgr()
    const p = any.doConnect(ID, baseConfig, true, 80, 24)
    await flush()
    const c = newClient()
    c.land()
    await p

    const bytes = Buffer.from('中文日志$', 'utf-8')
    const stream = any.sessions.get(ID)!.stream!
    stream.emit('data', bytes.subarray(0, 2)) // 刻意切在「文」的第二字节后
    stream.emit('data', bytes.subarray(2))

    const text = sent
      .filter((s) => s.channel === 'ssh:data' && s.data.id === ID)
      .map((s) => String(s.data.data))
      .join('')
    expect(text).toBe('中文日志$')
    expect(text).not.toContain('\uFFFD')
  })
})
