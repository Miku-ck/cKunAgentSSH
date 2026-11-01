/**
 * 更新执行器的 IO 编排守卫（electron/updater.ts）。
 *
 * 这里不联网、不解压、不启进程：三个重活都走构造参数注入，测试只验"什么时候调什么、
 * 失败之后现场是什么样"。重点守两类真实事故：
 *   - 下载/解压失败后 staging 半成品没清掉，下次"目录已存在"行为不确定；
 *   - 状态停在 error/applying 之后没有合法出口，用户点了没反应。
 */
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Updater, type UpdaterOptions } from './updater'

let root = ''
let appDir = ''

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'ckun-updater-'))
  appDir = join(root, 'cKunAgentSSH')
  mkdirSync(appDir, { recursive: true })
})

afterEach(() => {
  vi.restoreAllMocks()
})

const SHA = 'b'.repeat(64)

function makeUpdater(over: Partial<UpdaterOptions> = {}) {
  const opts: UpdaterOptions = {
    appDir,
    userDataDir: join(root, 'userData'),
    currentVersion: '0.1.0',
    exeName: 'cKunAgentSSH.exe',
    platform: 'win32',
    arch: 'x64',
    feedUrl: 'https://feed.example/latest.json',
    fetchJson: async () => ({
      version: '0.2.0',
      notes: '更稳',
      releases: [{ platform: 'win32', arch: 'x64', url: 'https://feed.example/a.tar.gz', sha256: SHA }],
    }),
    ...over,
  }
  return new Updater(opts)
}

describe('check()', () => {
  it('未配置 feedUrl 时直接 idle，连网络都不碰', async () => {
    const fetchJson = vi.fn()
    const u = makeUpdater({ feedUrl: '', fetchJson })
    const s = await u.check()
    expect(fetchJson).not.toHaveBeenCalled()
    expect(s.phase).toBe('idle')
  })

  it('env（CKUN_UPDATE_FEED）给的明文地址在发请求之前就被拦下', async () => {
    // main.ts 把 env 值原样当 feedUrl 传进来，不经过 setFeed —— check() 是这条
    // 旁路上唯一还会判协议的地方，所以它必须自己判，而不是"反正前面挡过了"。
    const fetchJson = vi.fn()
    const u = makeUpdater({ feedUrl: 'http://mirror.example/latest.json', fetchJson })
    const s = await u.check()
    expect(fetchJson).not.toHaveBeenCalled()
    expect(s.phase).toBe('error')
    expect(s.error).toContain('https')
  })

  it('有更新时进 available，并带出目标版本与说明', async () => {
    const u = makeUpdater()
    const s = await u.check()
    expect(s.phase).toBe('available')
    expect(s.targetVersion).toBe('0.2.0')
    expect(s.notes).toBe('更稳')
  })

  it('清单拉不到时静默回 idle（离线不该在界面上留红字）', async () => {
    const u = makeUpdater({ fetchJson: async () => { throw new Error('ENOTFOUND') } })
    const s = await u.check()
    expect(s.phase).toBe('idle')
    expect(s.error).toBeNull()
  })

  it('没有本平台产物时同样回 idle，而不是 error', async () => {
    const u = makeUpdater({
      fetchJson: async () => ({
        version: '0.2.0',
        releases: [{ platform: 'darwin', arch: 'arm64', url: 'https://feed.example/m.tar.gz', sha256: SHA }],
      }),
    })
    const s = await u.check()
    expect(s.phase).toBe('idle')
    expect(s.targetVersion).toBeNull()
  })

  it('error 之后还能再检查一次（不锁死）', async () => {
    let call = 0
    const u = makeUpdater({
      fetchJson: async () => {
        call++
        if (call === 1) throw new Error('boom')
        return { version: '0.2.0', releases: [{ platform: 'win32', arch: 'x64', url: 'https://x/a.tar.gz', sha256: SHA }] }
      },
    })
    await u.check()
    const s = await u.check()
    expect(s.phase).toBe('available')
  })
})

describe('download()', () => {
  it('哈希不匹配：进 error、删掉下载文件、绝不产生 staging', async () => {
    const u = makeUpdater({
      downloadFile: async (_url, dest) => {
        writeFileSync(dest, 'x')
        return 'c'.repeat(64)
      },
    })
    await u.check()
    const s = await u.download()
    expect(s.phase).toBe('error')
    expect(s.error).toContain('sha256 不匹配')
    expect(existsSync(join(root, '.ckun-update-0.2.0'))).toBe(false)
  })

  it('校验通过后解压到 appDir 的兄弟目录（同盘 rename 的前提）', async () => {
    const extractTar = vi.fn(async (_a: string, dest: string) => {
      mkdirSync(dest, { recursive: true })
      writeFileSync(join(dest, 'cKunAgentSSH.exe'), 'new')
    })
    const u = makeUpdater({
      downloadFile: async (_url, dest) => {
        writeFileSync(dest, 'x')
        return SHA
      },
      extractTar,
    })
    await u.check()
    const s = await u.download()
    expect(s.phase).toBe('ready')
    expect(s.percent).toBe(100)
    expect(extractTar).toHaveBeenCalledTimes(1)
    expect(existsSync(join(root, '.ckun-update-0.2.0', 'cKunAgentSSH.exe'))).toBe(true)
  })

  it('解压失败时清掉半成品 staging（下次重试不踩"目录已存在"）', async () => {
    const u = makeUpdater({
      downloadFile: async (_url, dest) => {
        writeFileSync(dest, 'x')
        return SHA
      },
      extractTar: async (_a, dest) => {
        mkdirSync(dest, { recursive: true })
        throw new Error('tar 退出码 2')
      },
    })
    await u.check()
    const s = await u.download()
    expect(s.phase).toBe('error')
    expect(existsSync(join(root, '.ckun-update-0.2.0'))).toBe(false)
  })
})

describe('apply() / pending 标记', () => {
  async function readyUpdater() {
    const spawnApplier = vi.fn()
    const u = makeUpdater({
      downloadFile: async (_url, dest) => {
        writeFileSync(dest, 'x')
        return SHA
      },
      extractTar: async (_a, dest) => {
        mkdirSync(dest, { recursive: true })
      },
      spawnApplier,
    })
    await u.check()
    await u.download()
    return { u, spawnApplier }
  }

  it('未就绪时拒绝应用，不写标记', async () => {
    const u = makeUpdater({ spawnApplier: vi.fn() })
    expect(u.apply(1234)).toBe(false)
    expect(existsSync(u.pendingFile)).toBe(false)
  })

  it('就绪后写 pending.json + 带 BOM 的助手脚本，并拉起助手', async () => {
    const { u, spawnApplier } = await readyUpdater()
    expect(u.apply(4321)).toBe(true)
    const pending = JSON.parse(readFileSync(u.pendingFile, 'utf8'))
    expect(pending).toMatchObject({ version: '0.2.0', pid: 4321, exeName: 'cKunAgentSSH.exe', appDir })
    const script = readFileSync(join(u.updateDir, 'applier.ps1'), 'utf8')
    // BOM：缺了它 Windows PowerShell 5.1 会把中文注释按 ANSI 解析
    expect(script.charCodeAt(0)).toBe(0xfeff)
    expect(script).toContain('Get-Process -Id 4321')
    expect(spawnApplier).toHaveBeenCalledTimes(1)
    expect(u.getState().phase).toBe('applying')
  })

  it('助手启动失败要回到 error 并删掉标记（否则下次启动会误判为待换包）', async () => {
    const { u } = await readyUpdater()
    const bad = makeUpdater({
      downloadFile: async (_url, dest) => {
        writeFileSync(dest, 'x')
        return SHA
      },
      extractTar: async (_a, dest) => {
        mkdirSync(dest, { recursive: true })
      },
      spawnApplier: () => {
        throw new Error('spawn EPERM')
      },
    })
    await bad.check()
    await bad.download()
    expect(bad.apply(1)).toBe(false)
    expect(existsSync(bad.pendingFile)).toBe(false)
    expect(bad.getState().phase).toBe('error')
    // 同一次构建里另一个实例不受影响
    expect(u.getState().phase).toBe('ready')
  })

  it('标记版本 == 当前版本 → 判定换包成功，标记作废', async () => {
    const { u } = await readyUpdater()
    u.apply(1)
    // 换包后新版本已经在跑：同一个 userData、更高的 currentVersion
    const resumed = makeUpdater({ currentVersion: '0.2.0' })
    const r = resumed.consumePending()
    expect(r.applied).toBe(true)
    expect(existsSync(u.pendingFile)).toBe(false)
  })

  it('标记版本 != 当前版本 → 判定没换成，清掉 staging 并留痕', async () => {
    const { u } = await readyUpdater()
    u.apply(1)
    const logs: string[] = []
    const again = makeUpdater({ userDataDir: join(root, 'userData'), log: (_l, m) => logs.push(m) })
    const r = again.consumePending()
    expect(r.applied).toBe(false)
    expect(r.version).toBe('0.2.0')
    expect(existsSync(join(root, '.ckun-update-0.2.0'))).toBe(false)
    expect(logs.join(' ')).toContain('上次换包未完成')
  })

  it('pending.json 损坏时当没有，不抛异常', async () => {
    const u = makeUpdater()
    mkdirSync(u.updateDir, { recursive: true })
    writeFileSync(u.pendingFile, '{ not json')
    expect(u.readPending()).toBeNull()
    expect(u.consumePending()).toEqual({ applied: false, version: null })
  })
})

describe('cleanupStale()', () => {
  it('清掉非当前版本的 staging 残留，保留当前版本的同名目录', async () => {
    mkdirSync(join(root, '.ckun-update-0.0.9'), { recursive: true })
    mkdirSync(join(root, '.ckun-update-0.1.0'), { recursive: true })
    const u = makeUpdater()
    u.cleanupStale()
    expect(existsSync(join(root, '.ckun-update-0.0.9'))).toBe(false)
    expect(existsSync(join(root, '.ckun-update-0.1.0'))).toBe(true)
  })
})

describe('setFeed() / loadConfig()', () => {
  it('非 https 地址直接拒绝，不覆盖已有通道', () => {
    const u = makeUpdater()
    const s = u.setFeed('file:///C:/x/latest.json')
    expect(s.phase).toBe('error')
    expect(s.error).toContain('https')
    expect(s.feedUrl).toBe('https://feed.example/latest.json')
    // 明文公网在旧口径里算"合法 http(s)"，现在不是：清单没签名，期望哈希也来自它
    expect(u.setFeed('http://mirror.example/latest.json').error).toContain('https')
    expect(u.setFeed('http://mirror.example/latest.json').feedUrl).toBe('https://feed.example/latest.json')
    // 本地假清单验收走回环明文，这条不能一起挡掉（设计里写死的无网络验收路径）
    expect(u.setFeed('http://127.0.0.1:8000/latest.json').feedUrl).toBe('http://127.0.0.1:8000/latest.json')
  })

  it('保存合法地址会落盘 config.json；空串 = 回到「不检查」', () => {
    const u = makeUpdater()
    const s = u.setFeed('https://mirror.example/latest.json')
    expect(s.feedUrl).toBe('https://mirror.example/latest.json')
    expect(s.phase).toBe('idle')
    expect(JSON.parse(readFileSync(u.configFile, 'utf8')).feedUrl).toBe('https://mirror.example/latest.json')
    expect(u.setFeed('').feedUrl).toBe('')
  })

  it('loadConfig 用落盘值覆盖环境变量给的默认值；非法值忽略', () => {
    const a = makeUpdater()
    a.setFeed('https://mirror.example/latest.json')
    const b = makeUpdater({ userDataDir: join(root, 'userData') })
    b.loadConfig()
    expect(b.getState().feedUrl).toBe('https://mirror.example/latest.json')

    const c = makeUpdater()
    mkdirSync(c.updateDir, { recursive: true })
    writeFileSync(c.configFile, JSON.stringify({ feedUrl: 'javascript:alert(1)' }))
    c.loadConfig()
    expect(c.getState().feedUrl).toBe('https://feed.example/latest.json')
  })
})

describe('supported / 目录可写预检', () => {
  it('appDir 为空（开发运行）→ supported=false，check 不联网、download 直接拒', async () => {
    const fetchJson = vi.fn()
    const u = makeUpdater({ appDir: '', fetchJson })
    expect(u.getState().supported).toBe(false)
    expect((await u.check()).phase).toBe('idle')
    expect(fetchJson).not.toHaveBeenCalled()
    const s = await u.download()
    expect(s.phase).toBe('error')
    expect(s.error).toContain('开发运行')
  })

  it('appDir 不可写时在下载之前就报错（不白下一份 171MB）', async () => {
    const u = makeUpdater({ appDir: join(root, 'missing-dir') })
    expect((await u.check()).phase).toBe('available')
    const s = await u.download()
    expect(s.phase).toBe('error')
    expect(s.error).toContain('不可写')
  })
})
