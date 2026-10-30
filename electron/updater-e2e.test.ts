/**
 * 自动更新的**真实 IO** 集成测试：本地 HTTP + 真 tar + 真 PowerShell 助手。
 *
 * 为什么必须有这一层：`electron/updater.test.ts` 把 fetchJson/downloadFile/extractTar/
 * spawnApplier 全部注入了替身，于是「助手到底起没起来」从来没被验证过。2026-10-30
 * 用本用例第一次跑生产路径时暴露了真实缺陷：Windows 上 `spawn(powershell.exe,
 * { detached: true })` 对应 DETACHED_PROCESS（无控制台），powershell.exe 拿不到控制台
 * 会**立即 exit 0 且什么都不做** —— 所有单元测试全绿，线上点「重启并更新」却永远只是
 * 应用退出、什么都没换。修复后本用例才能通过（见 updater.ts 的 defaultSpawnApplier）。
 *
 * 只跑 Windows：依赖 tar.exe / cmd.exe / Windows PowerShell 5.1，且要真的搬目录。
 * 用已退出的 PID 当宿主 PID，等价于「应用已经退出」——不必真的去等一个进程。
 */
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { Updater } from './updater'

const isWindows = process.platform === 'win32'

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}

describe.skipIf(!isWindows)('自更新真实 IO（check → download → apply → 换包）', () => {
  it('走完生产路径：新版就位、旧目录改名后被清掉、staging 不再残留', async () => {
    const root = mkdtempSync(join(tmpdir(), 'ckun-e2e-'))
    const appDir = join(root, 'cKunAgentSSH')
    mkdirSync(appDir, { recursive: true })
    writeFileSync(join(appDir, 'old.txt'), 'old')

    // 造一份"新版本"目录并打成 tar.gz（归档根层就是 appDir 的内容 —— 与发布产物的形态一致）
    const srcDir = join(root, 'src')
    mkdirSync(srcDir, { recursive: true })
    writeFileSync(join(srcDir, 'new.txt'), 'new')
    const archive = join(root, 'v020.tar.gz')
    const tar = spawnSync('tar.exe', ['-czf', archive, '-C', srcDir, '.'], { encoding: 'utf8' })
    expect(tar.status, tar.stderr).toBe(0)
    const body = readFileSync(archive)
    const sha256 = createHash('sha256').update(body).digest('hex')

    let port = 0
    const manifest = {
      version: '0.2.0',
      notes: 'e2e',
      releases: [{ platform: process.platform, arch: process.arch, url: '', sha256, size: body.length }],
    }
    const server = createServer((req, res) => {
      if (req.url?.startsWith('/latest.json')) {
        manifest.releases[0].url = `http://127.0.0.1:${port}/v020.tar.gz`
        res.setHeader('content-type', 'application/json')
        res.end(JSON.stringify(manifest))
        return
      }
      res.setHeader('content-length', String(body.length))
      res.end(body)
    })
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
    port = (server.address() as { port: number }).port

    try {
      const u = new Updater({
        appDir,
        userDataDir: join(root, 'userData'),
        currentVersion: '0.1.0',
        // 故意指向不存在的 exe：只验换包，不真拉起进程（助手会把失败写进 applier.log）
        exeName: 'cKunAgentSSH.exe',
        platform: process.platform,
        arch: process.arch,
        feedUrl: `http://127.0.0.1:${port}/latest.json`,
      })

      const checked = await u.check()
      expect(checked.phase, checked.error ?? '').toBe('available')
      expect(checked.targetVersion).toBe('0.2.0')

      const downloaded = await u.download()
      expect(downloaded.phase, downloaded.error ?? '').toBe('ready')
      expect(readdirSync(join(root, '.ckun-update-0.2.0'))).toContain('new.txt')
      // 下载/解压阶段旧版本一个字节没动
      expect(existsSync(join(appDir, 'old.txt'))).toBe(true)

      const dead = spawnSync(process.execPath, ['-e', '0'])
      expect(u.apply(dead.pid ?? 0)).toBe(true)

      const logFile = join(u.updateDir, 'applier.log')
      let log = ''
      for (let i = 0; i < 160; i++) {
        await sleep(250)
        if (existsSync(logFile)) log = readFileSync(logFile, 'utf8')
        if (log.includes('cleanup done')) break
      }

      // 用 expect 的 message 带出日志：助手静默失败时（正是那个 detached 缺陷）能直接看到现场
      expect(log, `applier.log 内容：${log}`).toContain('swap done')
      expect(log, `applier.log 内容：${log}`).toContain('cleanup done')
      // 换包失败也不该假装成功：拉起这一步会失败并被记录，但不能影响目录已经换好
      expect(log).toContain('relaunch failed')
      expect(existsSync(join(appDir, 'new.txt')), `appDir=${readdirSync(appDir).join(',')}`).toBe(true)
      expect(existsSync(join(appDir, 'old.txt'))).toBe(false)
      expect(existsSync(join(root, '.cKunAgentSSH-backup'))).toBe(false)
      expect(existsSync(join(root, '.ckun-update-0.2.0'))).toBe(false)
    } finally {
      server.close()
    }
  }, 120_000)
})
