/**
 * 诊断包"真 tar"端到端守卫（零注入）。
 *
 * electron/diagnostics.test.ts 里 tar 走的是注入点，于是剩下一整类错误没有被覆盖：
 * **真实 tar 的参数与相对路径**。Windows 自带的是 bsdtar，`-czf <绝对路径> -C <目录> .`
 * 这种"输出路径写在切换目录之前"的顺序一旦写错，得到的不是报错而是空包或错位目录 ——
 * 单测里注入的 fakeTar 永远不会发现。这里用真 tar.exe 打一次包，再把包解开核对内容。
 *
 * 只在 Windows 上跑：`tar.exe` 存在于 System32 是本项目的既有假设（打包与自动更新
 * 两条链路都依赖它），POSIX 下 tar 的方言差异不值得在这里再养一套。
 */
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { exportDiagnostics } from './diagnostics'

const isWindows = process.platform === 'win32'

let root = ''
let userData = ''

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'ckun-diag-e2e-'))
  userData = join(root, 'userData')
  mkdirSync(join(userData, 'Crashpad', 'completed'), { recursive: true })
})

afterEach(() => {
  rmSync(root, { recursive: true, force: true })
})

describe.skipIf(!isWindows)('exportDiagnostics（真实 tar.exe）', () => {
  it('包里能解开出 system.json / 脱敏日志 / 崩溃转储，且不残留 staging', async () => {
    writeFileSync(
      join(userData, 'main.log'),
      '[12:00:00.000] [WARN] ELECTRON_BRIDGE_TOKEN=abcdef123456\n[12:00:01.000] [INFO] 后端启动完成\n',
      'utf8',
    )
    writeFileSync(join(userData, 'Crashpad', 'completed', 'crash-1.dmp'), 'MDMP-fake-bytes')

    const res = await exportDiagnostics({
      userDataDir: userData,
      appDir: join(root, 'app'),
      currentVersion: '0.1.0',
      electronVersion: '35.7.5',
      chromeVersion: '134.0.0.0',
      nodeVersion: '22.0.0',
      platform: process.platform,
      arch: process.arch,
      locale: 'zh-CN',
      startedAt: new Date('2026-10-30T00:00:00.000Z'),
      env: { CKUN_UPDATE_FEED: 'https://feed.example/latest.json' },
    })

    expect(res.ok).toBe(true)
    expect(res.path).toBeTruthy()

    // 1) 归档里真的有三个部分（目录前缀可能是 ./，按后缀判）
    const listing = execFileSync('tar.exe', ['-tzf', res.path!], { encoding: 'utf8' })
    const entries = listing.split(/\r?\n/).map((l) => l.replace(/^\.\//, '')).filter(Boolean)
    expect(entries).toContain('system.json')
    expect(entries).toContain('logs/main.log')
    expect(entries).toContain('crashes/01-crash-1.dmp')

    // 2) 解出来的内容与盘上一致：日志脱敏、system.json 可解析
    const unpacked = join(root, 'unpacked')
    mkdirSync(unpacked, { recursive: true })
    execFileSync('tar.exe', ['-xzf', res.path!, '-C', unpacked])
    const log = readFileSync(join(unpacked, 'logs', 'main.log'), 'utf8')
    expect(log).toContain('后端启动完成')
    expect(log).not.toContain('abcdef123456')
    expect(log).toContain('ELECTRON_BRIDGE_TOKEN=***')

    const info = JSON.parse(readFileSync(join(unpacked, 'system.json'), 'utf8'))
    expect(info.app.version).toBe('0.1.0')
    expect(info.crashDumps).toBe(1)
    expect(info.env.CKUN_UPDATE_FEED).toBe('https://feed.example/latest.json')

    // 3) 崩溃转储按原字节带过去（二进制不脱敏）
    expect(readFileSync(join(unpacked, 'crashes', '01-crash-1.dmp'), 'utf8')).toBe('MDMP-fake-bytes')

    // 4) staging 已清，产物目录里只剩这一个包
    const dir = join(userData, 'diagnostics')
    const left = execFileSync('cmd.exe', ['/c', 'dir', '/b', dir], { encoding: 'utf8' })
      .split(/\r?\n/).filter(Boolean)
    expect(left).toEqual([res.path!.split(/[\\/]/).pop()])
  })
})
