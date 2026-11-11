/**
 * 诊断包导出（electron/diagnostics.ts）的 IO 层守卫。
 *
 * 纯逻辑（脱敏/尾截断/清单）在 shared/diagnostics-core.test.ts 里已钉过；这里只钉
 * IO 决策：**哪些文件进包**、截断与脱敏是否真的落到了包内内容、失败时留什么。
 * tar 走注入点（真实实现是系统 tar.exe），其余全部是真文件系统 —— 这套逻辑的
 * 错误几乎全部来自"路径与真实目录不一致"，用假 fs 反而测不出来。
 */
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, utimesSync, writeFileSync } from 'node:fs'
import type { Dirent } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { exportDiagnostics, type DiagnosticsOptions } from './diagnostics'

let root = ''
let userData = ''

/** 注入的 tar：先把 staging 里的文本全读下来（staging 成功后会被删），再落一个占位包 */
function fakeTar(sink: { cwd: string; files: Record<string, string> }) {
  return async (archive: string, cwd: string): Promise<void> => {
    sink.cwd = cwd
    sink.files = readAllText(cwd)
    writeFileSync(archive, 'fake-tar')
  }
}

function readAllText(dir: string, depth = 0): Record<string, string> {
  const out: Record<string, string> = {}
  const entries: Dirent[] = readdirSync(dir, { withFileTypes: true })
  for (const e of entries) {
    const p = join(dir, e.name)
    if (e.isDirectory()) Object.assign(out, readAllText(p, depth + 1))
    else out[e.name] = readFileSync(p, 'utf8')
  }
  return out
}

function baseOpts(patch: Partial<DiagnosticsOptions> = {}): DiagnosticsOptions {
  return {
    userDataDir: userData,
    appDir: join(root, 'app'),
    currentVersion: '0.1.0',
    electronVersion: '35.7.5',
    chromeVersion: '134.0.0.0',
    nodeVersion: '22.0.0',
    platform: 'win32',
    arch: 'x64',
    locale: 'zh-CN',
    startedAt: new Date('2026-10-30T00:00:00.000Z'),
    env: {},
    now: () => new Date(2026, 9, 30, 12, 5, 9),
    ...patch,
  }
}

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'ckun-diag-'))
  userData = join(root, 'userData')
  mkdirSync(userData, { recursive: true })
})

afterEach(() => {
  rmSync(root, { recursive: true, force: true })
})

describe('exportDiagnostics', () => {
  it('只收白名单日志：抹掉凭据、写进 logs/，包外文件一个都不进去', async () => {
    writeFileSync(
      join(userData, 'main.log'),
      '[12:00:00.000] [INFO] 启动 port=8080\n[12:00:00.100] [WARN] ELECTRON_BRIDGE_TOKEN=abcdef123456\n',
      'utf8',
    )
    writeFileSync(join(userData, 'electron-bridge.log'), 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9\n', 'utf8')
    // 不在白名单：用户的私人文件绝不能被"顺手打包"
    writeFileSync(join(userData, 'notes.log'), 'private', 'utf8')

    const sink = { cwd: '', files: {} as Record<string, string> }
    const res = await exportDiagnostics(baseOpts({ tarCreate: fakeTar(sink) }))

    expect(res.ok).toBe(true)
    expect(res.path).toContain('ckunagentssh-diag-0.1.0-20261030-120509.tar.gz')
    expect(res.logs.map((l) => l.name)).toEqual(['main.log', 'electron-bridge.log'])
    expect(res.crashDumps).toBe(0)

    // 脱敏必须落到包内内容，而不只是在纯函数里"测过"
    expect(sink.files['main.log']).toContain('port=8080')
    expect(sink.files['main.log']).not.toContain('abcdef123456')
    expect(sink.files['electron-bridge.log']).not.toContain('eyJhbGciOiJIUzI1NiJ9')
    expect(sink.files['notes.log']).toBeUndefined()

    // system.json 与说明在位，且不含凭据
    const info = JSON.parse(sink.files['system.json']!)
    expect(info.app.version).toBe('0.1.0')
    expect(info.logs).toHaveLength(2)
    expect(sink.files['README.txt']).toContain('诊断包')

    // 成功后 staging 必须清掉，只留产物
    expect(readdirSync(join(userData, 'diagnostics')).sort()).toEqual([
      'ckunagentssh-diag-0.1.0-20261030-120509.tar.gz',
    ])
    expect(statSync(res.path!).size).toBe(8)
  })

  it('超限日志只留尾部（排障看最近发生了什么）', async () => {
    const head = `${'HEAD-MARKER '.repeat(200)}\n`
    const tail = 'TAIL-MARKER: 后端起不来\n'
    writeFileSync(join(userData, 'main.log'), head + tail, 'utf8')

    const sink = { cwd: '', files: {} as Record<string, string> }
    // 上限取尾部自身的字节数：截断点逐字落在字符边界上，结果应当恰好等于尾部
    const res = await exportDiagnostics(baseOpts({ tarCreate: fakeTar(sink), maxLogBytes: Buffer.byteLength(tail, 'utf8') }))

    expect(res.ok).toBe(true)
    expect(sink.files['main.log']).toBe(tail)
    // 清单里报的是盘上真实大小，不是截断后的字节数
    expect(res.logs[0]!.bytes).toBe(Buffer.byteLength(head + tail, 'utf8'))
  })

  it('只带最近的 5 份崩溃转储，并给出序号前缀', async () => {
    const dumpsDir = join(userData, 'Crashpad')
    const completed = join(dumpsDir, 'completed')
    mkdirSync(completed, { recursive: true })
    for (let i = 0; i < 7; i++) {
      const p = join(completed, `crash-${i}.dmp`)
      writeFileSync(p, `dump-${i}`)
      // mtime 递增：i 越大越新
      const t = new Date(2026, 9, 1, 0, 0, i)
      utimesSync(p, t, t)
    }

    const sink = { cwd: '', files: {} as Record<string, string> }
    const res = await exportDiagnostics(baseOpts({ tarCreate: fakeTar(sink) }))

    expect(res.ok).toBe(true)
    expect(res.crashDumps).toBe(5)
    const inBundle = Object.keys(sink.files).filter((n) => n.endsWith('.dmp')).sort()
    expect(inBundle).toHaveLength(5)
    // 最近的 5 份 = crash-6..crash-2
    expect(inBundle).toEqual([
      '01-crash-6.dmp',
      '02-crash-5.dmp',
      '03-crash-4.dmp',
      '04-crash-3.dmp',
      '05-crash-2.dmp',
    ])
    // 二进制内容原样带过去（不脱敏：.dmp 是二进制，改写等于毁证）
    expect(sink.files['01-crash-6.dmp']).toBe('dump-6')
  })

  it('从未崩溃过 / 从未写过日志也能出包（空包不是错误）', async () => {
    const sink = { cwd: '', files: {} as Record<string, string> }
    const res = await exportDiagnostics(baseOpts({ tarCreate: fakeTar(sink) }))

    expect(res.ok).toBe(true)
    expect(res.logs).toEqual([])
    expect(res.crashDumps).toBe(0)
    expect(sink.files['system.json']).toBeDefined()
    expect(sink.files['README.txt']).toContain('本次日志 0 份')
  })

  it('tar 失败：不产包、临时目录保留（现场还能手工取走）', async () => {
    writeFileSync(join(userData, 'main.log'), 'boom\n', 'utf8')
    const res = await exportDiagnostics(
      baseOpts({
        tarCreate: async () => {
          throw new Error('tar 退出码 1')
        },
      }),
    )

    expect(res.ok).toBe(false)
    expect(res.path).toBeNull()
    expect(res.error).toContain('tar 退出码 1')
    expect(res.error).toContain('.staging-')
    const outDir = join(userData, 'diagnostics')
    const left = readdirSync(outDir)
    expect(left).toHaveLength(1)
    expect(left[0]!.startsWith('.staging-')).toBe(true)
    // staging 里的日志已经写好并脱敏过：失败也没有白干
    expect(readFileSync(join(outDir, left[0]!, 'logs', 'main.log'), 'utf8')).toBe('boom\n')
  })

  it('下一次成功导出会清掉历史失败残留的 staging', async () => {
    const outDir = join(userData, 'diagnostics')
    mkdirSync(join(outDir, '.staging-999-1'), { recursive: true })

    const sink = { cwd: '', files: {} as Record<string, string> }
    const res = await exportDiagnostics(baseOpts({ tarCreate: fakeTar(sink) }))

    expect(res.ok).toBe(true)
    expect(readdirSync(outDir)).toEqual(['ckunagentssh-diag-0.1.0-20261030-120509.tar.gz'])
  })

  it('更新通道地址里的口令不会跟着 system.json 出包', async () => {
    const sink = { cwd: '', files: {} as Record<string, string> }
    await exportDiagnostics(
      baseOpts({
        tarCreate: fakeTar(sink),
        updateFeedUrl: 'https://u:secretpw@feed.example/latest.json',
        updatePhase: 'available',
        env: { CKUN_UPDATE_FEED: 'https://u:secretpw@feed.example/latest.json' },
      }),
    )

    expect(sink.files['system.json']).not.toContain('secretpw')
    expect(JSON.parse(sink.files['system.json']!).update.phase).toBe('available')
  })
})
