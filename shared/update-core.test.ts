/**
 * tar 形态自动更新的纯逻辑守卫（shared/update-core.ts）。
 *
 * 这一层不碰 IO，所以可以穷举边界：版本比较、平台筛选、换包路径、助手脚本顺序。
 * 真正的坑都在"看起来一样但结论相反"的地方 —— 缺 sha256 的产物必须被拒、
 * staging 必须在 appDir 外面、error 之后必须还能回到 checking。
 */
import { describe, expect, it } from 'vitest'
import {
  buildApplierScript,
  canTransition,
  compareVersions,
  formatBytes,
  initialState,
  isNewerVersion,
  normalizeSha256,
  progressPercent,
  selectUpdate,
  stagingPaths,
  type PendingUpdate,
} from './update-core'

const SHA = 'a'.repeat(64)

const manifest = {
  version: '0.2.0',
  notes: '修复若干',
  releases: [
    { platform: 'darwin', arch: 'arm64', url: 'https://x/y.tar.gz', sha256: SHA },
    { platform: 'win32', arch: 'arm64', url: 'https://x/y-arm.tar.gz', sha256: SHA },
    { platform: 'win32', arch: 'x64', url: 'https://x/y.tar.gz', sha256: SHA, size: 1024 },
  ],
}

describe('compareVersions / isNewerVersion', () => {
  it('按段比较数字而不是字符串（0.10.0 > 0.9.0）', () => {
    expect(compareVersions('0.10.0', '0.9.0')).toBe(1)
    expect(compareVersions('1.0.0', '1.0.0')).toBe(0)
    expect(compareVersions('1.0.1', '1.1.0')).toBe(-1)
  })

  it('预发布版本低于同号正式版', () => {
    expect(compareVersions('1.0.0-beta', '1.0.0')).toBe(-1)
    expect(compareVersions('1.0.0', '1.0.0-beta')).toBe(1)
    expect(isNewerVersion('1.0.0-rc.2', '1.0.0-rc.1')).toBe(true)
  })

  it('容忍前导 v，畸形版本按"最旧"处理（不抛异常）', () => {
    expect(compareVersions('v1.2.3', '1.2.3')).toBe(0)
    expect(compareVersions('0.1', '0.1.0')).toBe(-1)
    expect(isNewerVersion('1.0', '0.9.9')).toBe(false)
  })
})

describe('selectUpdate', () => {
  const target = { platform: 'win32', arch: 'x64' }

  it('挑出本平台本架构的产物，并归一哈希', () => {
    const picked = selectUpdate(manifest, '0.1.0', target)
    expect(picked?.version).toBe('0.2.0')
    expect(picked?.release.url).toBe('https://x/y.tar.gz')
    expect(picked?.release.sha256).toBe(SHA)
    expect(picked?.notes).toBe('修复若干')
  })

  it('版本不更新时返回 null（同版本、更旧版本都算）', () => {
    expect(selectUpdate(manifest, '0.2.0', target)).toBeNull()
    expect(selectUpdate(manifest, '0.3.0', target)).toBeNull()
  })

  it('没有本平台产物时返回 null，不会退而求其次拿别的架构', () => {
    expect(selectUpdate(manifest, '0.1.0', { platform: 'linux', arch: 'x64' })).toBeNull()
    expect(selectUpdate(manifest, '0.1.0', { platform: 'win32', arch: 'ia32' })).toBeNull()
  })

  it('缺 sha256 或哈希不是 64 位十六进制的产物一律拒绝', () => {
    const bad = {
      version: '0.2.0',
      releases: [{ platform: 'win32', arch: 'x64', url: 'https://x/y.tar.gz', sha256: 'deadbeef' }],
    }
    expect(selectUpdate(bad, '0.1.0', target)).toBeNull()
    expect(normalizeSha256(undefined)).toBeNull()
    expect(normalizeSha256('A'.repeat(64))).toBe('a'.repeat(64))
  })

  it('非 http(s) 的 URL 拒绝（file://、裸路径都不认）', () => {
    const bad = {
      version: '0.2.0',
      releases: [{ platform: 'win32', arch: 'x64', url: 'C:\\tmp\\a.tar.gz', sha256: SHA }],
    }
    expect(selectUpdate(bad, '0.1.0', target)).toBeNull()
  })

  it('清单畸形（null / 缺字段 / releases 不是数组）不抛异常', () => {
    expect(selectUpdate(null, '0.1.0', target)).toBeNull()
    expect(selectUpdate({ version: '0.2.0' }, '0.1.0', target)).toBeNull()
    expect(selectUpdate({ version: 'x', releases: [] }, '0.1.0', target)).toBeNull()
  })
})

describe('stagingPaths', () => {
  it('staging 与 backup 都在 appDir 的兄弟位置（换包第一步会改名 appDir）', () => {
    const p = stagingPaths('D:\\apps\\cKunAgentSSH', '0.2.0')
    expect(p.stagingDir).toBe('D:/apps/.ckun-update-0.2.0')
    expect(p.backupDir).toBe('D:/apps/.cKunAgentSSH-backup')
    // 关键不变量：staging 不能落在 appDir 里面，否则会被一起改名搬走
    expect(p.stagingDir.startsWith('D:/apps/cKunAgentSSH/')).toBe(false)
  })

  it('容忍尾部分隔符', () => {
    const p = stagingPaths('D:\\apps\\cKunAgentSSH\\', '0.2.0')
    expect(p.backupDir).toBe('D:/apps/.cKunAgentSSH-backup')
  })
})

describe('buildApplierScript', () => {
  const pending: PendingUpdate = {
    version: '0.2.0',
    appDir: 'D:\\apps\\cKunAgentSSH',
    stagingDir: 'D:\\apps\\.ckun-update-0.2.0',
    backupDir: 'D:\\apps\\.cKunAgentSSH-backup',
    exeName: 'cKunAgentSSH.exe',
    pid: 4321,
    preparedAt: '2026-10-30T00:00:00.000Z',
  }
  const script = buildApplierScript(pending, 'C:\\Users\\u\\AppData\\Roaming\\x\\update\\applier.log')

  it('先等宿主 PID 退出，再动目录（运行中的 exe/asar 不可替换）', () => {
    const waitAt = script.indexOf(`Get-Process -Id 4321`)
    const firstMove = script.indexOf('Move-Item')
    expect(waitAt).toBeGreaterThan(-1)
    expect(firstMove).toBeGreaterThan(waitAt)
  })

  it('顺序是 appDir→backup 再 staging→appDir，等价于原子换包', () => {
    const toBackup = script.indexOf(`Move-Item -LiteralPath 'D:\\apps\\cKunAgentSSH' -Destination 'D:\\apps\\.cKunAgentSSH-backup'`)
    const promote = script.indexOf(`Move-Item -LiteralPath 'D:\\apps\\.ckun-update-0.2.0' -Destination 'D:\\apps\\cKunAgentSSH'`)
    expect(toBackup).toBeGreaterThan(-1)
    expect(promote).toBeGreaterThan(toBackup)
  })

  it('提升失败必须把备份改回原名（回滚），不留下半新半旧的目录', () => {
    const rollback = script.indexOf(`Move-Item -LiteralPath 'D:\\apps\\.cKunAgentSSH-backup' -Destination 'D:\\apps\\cKunAgentSSH'`)
    expect(rollback).toBeGreaterThan(script.indexOf('promote failed'))
  })

  it('换包完成后拉起新 exe，重试清备份并写终态日志', () => {
    expect(script).toContain(`Start-Process -FilePath 'D:\\apps\\cKunAgentSSH\\cKunAgentSSH.exe'`)
    // 杀软/索引服务可能短暂锁住备份：重试而不是一次 SilentlyContinue 了事
    expect(script).toContain(`for ($i = 0; $i -lt 3; $i++)`)
    expect(script).toContain(`Remove-Item -LiteralPath 'D:\\apps\\.cKunAgentSSH-backup' -Recurse -Force -ErrorAction Stop`)
    // 'swap done' 之后备份还没清完，必须有终态标记才算真的结束
    expect(script.lastIndexOf(`Write-Log 'cleanup done'`)).toBeGreaterThan(script.indexOf(`Write-Log 'swap done'`))
  })

  it('路径里的单引号被转义，不会把脚本拼断', () => {
    const s = buildApplierScript({ ...pending, appDir: "D:\\it's\\app" }, 'C:\\l.log')
    expect(s).toContain(`'D:\\it''s\\app'`)
  })
})

describe('状态机迁移', () => {
  it('error 不是死胡同：能回到 checking / downloading', () => {
    expect(canTransition('error', 'checking')).toBe(true)
    expect(canTransition('error', 'downloading')).toBe(true)
  })

  it('applying 是终态入口（写完标记就退出），不允许跳回 downloading', () => {
    expect(canTransition('applying', 'ready')).toBe(true)
    expect(canTransition('applying', 'downloading')).toBe(false)
  })

  it('idle 不能直接跳到 downloading（必须先检查出 available）', () => {
    expect(canTransition('idle', 'downloading')).toBe(false)
    expect(canTransition('checking', 'available')).toBe(true)
    expect(canTransition('available', 'downloading')).toBe(true)
  })

  it('initialState 把 feedUrl 原样带出（空串 = 未配置，UI 据此隐藏检查按钮）', () => {
    expect(initialState('0.1.0', '')).toMatchObject({ phase: 'idle', currentVersion: '0.1.0', feedUrl: '' })
  })
})

describe('展示辅助', () => {
  it('formatBytes 四舍五入到一位小数，大于 100 不带小数', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(171 * 1024 * 1024)).toBe('171 MB')
    expect(formatBytes(0.4)).toBe('0 B')
  })

  it('progressPercent 夹在 0-100，total 未知时给 0', () => {
    expect(progressPercent(50, 200)).toBe(25)
    expect(progressPercent(500, 200)).toBe(100)
    expect(progressPercent(1, 0)).toBe(0)
  })
})
