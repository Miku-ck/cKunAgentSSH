/**
 * 诊断包导出（本地落盘，不联网）。
 *
 * 现场排障最缺的不是"再问用户一句"，而是一份能直接看的快照：哪个版本、什么平台、
 * 主日志最后几 MB、崩溃转储、环境变量白名单。这里把这些打成一个 tar.gz 落到
 * userData/diagnostics/，用户点一下就能拿到并（自行决定）发出来。
 *
 * 边界：脱敏/尾截断/文件名/system.json 的字段都在 shared/diagnostics-core.ts（可单测），
 * 本文件只做文件读写与 tar，靠注入点测。
 *
 * 三条约束：
 *   1. 日志只认白名单文件名 —— 不按目录扫，避免把用户随手放进 userData 的东西打包；
 *   2. 任何失败都不许留半成品假象：失败时 archive 不出现、staging 保留（现场还能手工取走）；
 *   3. 体积可控：日志各留尾部 4MB，转储最多 5 份。
 */
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import type { Dirent } from 'node:fs'
import { join } from 'node:path'
import {
  DIAG_MAX_CRASH_DUMPS,
  DIAG_MAX_LOG_BYTES,
  buildSystemInfo,
  diagnosticsFileName,
  redactSecrets,
  tailText,
  type DiagnosticsResult,
  type LogFileStat,
} from '../shared/diagnostics-core'

export type { DiagnosticsResult }

/**
 * 允许进包的日志。与 main-logger.ts / electron-bridge.ts 的落盘命名一一对应：
 *   main.log(+.1/.2)        installMainLogger 的三代轮转（含后端 stdout 转发）
 *   electron-bridge.log(+.old) bridgeLog 的一代轮转
 * 白名单而非"扫 *.log"：userData 里还有用户可能放的任何文件。
 */
const LOG_WHITELIST = ['main.log', 'main.log.1', 'main.log.2', 'electron-bridge.log', 'electron-bridge.log.old'] as const

/** 崩溃转储目录最多向下找几层（Crashpad 实际是 completed/ new/ 两层） */
const CRASH_DUMP_MAX_DEPTH = 2

export interface DiagnosticsOptions {
  userDataDir: string
  appDir: string
  currentVersion: string
  electronVersion: string
  chromeVersion: string
  nodeVersion: string
  platform: string
  arch: string
  locale: string
  startedAt: Date
  /** 内嵌 CPython 运行时的版本；拿不到就是 null（不猜） */
  runtimeVersion?: string | null
  runtimePath?: string | null
  updateFeedUrl?: string
  updatePhase?: string
  env?: Record<string, string | undefined>
  /** 崩溃转储目录（与 crash-reporter 同源，默认 userData/Crashpad） */
  crashDumpsDir?: string
  /** 产物目录，默认 userData/diagnostics */
  outDir?: string
  /** 每份日志最多留多少字节，默认 DIAG_MAX_LOG_BYTES（单测调小） */
  maxLogBytes?: number
  now?: () => Date
  log?: (level: 'info' | 'warn' | 'error', message: string) => void
  /** 注入点：单测替换 tar（真实实现走系统 tar.exe） */
  tarCreate?: (archive: string, cwd: string) => Promise<void>
}

export async function exportDiagnostics(opts: DiagnosticsOptions): Promise<DiagnosticsResult> {
  const now = opts.now?.() ?? new Date()
  const outDir = opts.outDir ?? join(opts.userDataDir, 'diagnostics')
  const dumpsDir = opts.crashDumpsDir ?? join(opts.userDataDir, 'Crashpad')
  const maxLogBytes = opts.maxLogBytes ?? DIAG_MAX_LOG_BYTES
  // 名字里带 pid 与时间戳：并发/连续两次导出不会互相踩（导出是同步阻塞的，但失败残留会）
  const staging = join(outDir, `.staging-${process.pid}-${now.getTime()}`)
  const archive = join(outDir, diagnosticsFileName(opts.currentVersion, now))
  let logs: LogFileStat[] = []
  let crashes = 0

  try {
    mkdirSync(join(staging, 'logs'), { recursive: true })
    mkdirSync(join(staging, 'crashes'), { recursive: true })

    logs = collectLogs(opts.userDataDir, join(staging, 'logs'), maxLogBytes, opts.log)
    crashes = collectCrashDumps(dumpsDir, join(staging, 'crashes'), opts.log)

    const info = buildSystemInfo({
      appVersion: opts.currentVersion,
      electronVersion: opts.electronVersion,
      chromeVersion: opts.chromeVersion,
      nodeVersion: opts.nodeVersion,
      platform: opts.platform,
      arch: opts.arch,
      locale: opts.locale,
      pid: process.pid,
      startedAt: opts.startedAt.toISOString(),
      appDir: opts.appDir,
      userDataDir: opts.userDataDir,
      logs,
      runtimeVersion: opts.runtimeVersion ?? null,
      runtimePath: opts.runtimePath ?? null,
      updateFeedUrl: opts.updateFeedUrl ?? '',
      updatePhase: opts.updatePhase ?? 'unknown',
      crashDumps: crashes,
      env: opts.env ?? process.env,
    })
    writeFileSync(join(staging, 'system.json'), JSON.stringify(info, null, 2), 'utf8')
    writeFileSync(join(staging, 'README.txt'), bundleReadme(now, logs.length, crashes), 'utf8')

    const tarCreate = opts.tarCreate ?? defaultTarCreate
    await tarCreate(archive, staging)
    const bytes = statSync(archive).size
    rmSync(staging, { recursive: true, force: true })
    // 成功之后顺手清掉历史失败残留：现场已经进了本次的包，旧 staging 没有价值
    cleanupStaleStaging(outDir)
    opts.log?.('info', `诊断包已导出：${archive}（${bytes} 字节，${logs.length} 份日志，${crashes} 份转储）`)
    return { ok: true, path: archive, bytes, logs, crashDumps: crashes, error: null }
  } catch (e) {
    // 失败时故意保留 staging：tar 挂了但日志已经脱敏写好，用户/支持人员还能手工取走
    const msg = e instanceof Error ? e.message : String(e)
    opts.log?.('error', `诊断包导出失败：${msg}（临时目录 ${staging}）`)
    return {
      ok: false,
      path: null,
      bytes: 0,
      logs,
      crashDumps: crashes,
      error: `${msg}（未完成的内容留在 ${staging}）`,
    }
  }
}

/**
 * 收集白名单日志：按字节留尾部 → 抹凭据 → 写进 staging/logs。
 * 返回的是**原始**文件清单（体积要反映盘上真实大小，而不是截断后的）。
 */
function collectLogs(
  userDataDir: string,
  destDir: string,
  maxBytes: number,
  log?: DiagnosticsOptions['log'],
): LogFileStat[] {
  const out: LogFileStat[] = []
  for (const name of LOG_WHITELIST) {
    const src = join(userDataDir, name)
    let bytes = 0
    try {
      bytes = statSync(src).size
    } catch {
      continue  // 该代日志不存在（首次运行没有 .1/.2，未用过 bridge 没有 electron-bridge.log）
    }
    if (bytes === 0) continue
    try {
      const kept = tailText(readFileSync(src, 'utf8'), maxBytes)
      writeFileSync(join(destDir, name), redactSecrets(kept), 'utf8')
      out.push({ name, bytes })
    } catch (e) {
      // 单个日志读失败（被占用/编码异常）不该拖垮整个诊断包
      log?.('warn', `诊断包跳过日志 ${name}：${e instanceof Error ? e.message : String(e)}`)
    }
  }
  return out
}

/** 最近的几份崩溃转储。按 mtime 倒序：转储目录里会累积很久以前的历史。 */
function collectCrashDumps(
  dumpsDir: string,
  destDir: string,
  log?: DiagnosticsOptions['log'],
): number {
  const found = findDumpFiles(dumpsDir)
  if (found.length === 0) return 0
  found.sort((a, b) => b.mtime - a.mtime)
  const picked = found.slice(0, DIAG_MAX_CRASH_DUMPS)
  let copied = 0
  for (const [i, dump] of picked.entries()) {
    const name = `${String(i + 1).padStart(2, '0')}-${dump.path.split(/[\\/]/).pop() ?? 'crash.dmp'}`
    try {
      copyFileSync(dump.path, join(destDir, name))
      copied++
    } catch (e) {
      log?.('warn', `诊断包跳过转储 ${dump.path}：${e instanceof Error ? e.message : String(e)}`)
    }
  }
  return copied
}

function findDumpFiles(dir: string, depth = 0, acc: Array<{ path: string; mtime: number }> = []): Array<{ path: string; mtime: number }> {
  if (depth > CRASH_DUMP_MAX_DEPTH) return acc
  let entries: Dirent[]
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return acc  // 目录不存在 = 从未崩溃过，属正常
  }
  for (const entry of entries) {
    const p = join(dir, entry.name)
    if (entry.isDirectory()) {
      findDumpFiles(p, depth + 1, acc)
    } else if (entry.name.endsWith('.dmp')) {
      try {
        acc.push({ path: p, mtime: statSync(p).mtimeMs })
      } catch { /* 读不到 stat 的转储直接跳过 */ }
    }
  }
  return acc
}

/** 清掉历史失败留下的 staging（只在本次导出成功后调用） */
function cleanupStaleStaging(outDir: string): void {
  try {
    for (const name of readdirSync(outDir)) {
      if (!name.startsWith('.staging-')) continue
      rmSync(join(outDir, name), { recursive: true, force: true })
    }
  } catch { /* 清不掉不影响本次结果 */ }
}

function bundleReadme(now: Date, logCount: number, crashCount: number): string {
  return [
    'cKunAgentSSH 诊断包',
    `导出时间：${now.toISOString()}`,
    '',
    '内容：',
    '  system.json   版本/平台/路径/运行时/更新状态/环境变量白名单（凭据已抹）',
    '  logs/         主进程日志，每份只留尾部、凭据已抹',
    `  crashes/      最近 ${DIAG_MAX_CRASH_DUMPS} 份崩溃转储（本次 ${crashCount} 份）`,
    '',
    `本次日志 ${logCount} 份。包内所有文本都经过脱敏（Bearer / sk- / ghp_ / URL 口令 / key=value），`,
    '但不排除仍有主机名、用户名、路径等敏感信息 —— 发出前请自行确认。',
    '',
  ].join('\n')
}

/** 系统自带 bsdtar（Windows 10+ 的 System32\\tar.exe），与打包/更新链路同一个假设 */
function defaultTarCreate(archive: string, cwd: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.platform === 'win32' ? 'tar.exe' : 'tar', ['-czf', archive, '-C', cwd, '.'], {
      stdio: 'ignore',
      windowsHide: true,
    })
    child.on('error', reject)
    child.on('exit', (code) => {
      if (code === 0) resolve()
      else reject(new Error(`tar 退出码 ${code}`))
    })
  })
}

/** 供 IPC 侧判断"上次导出的包还在不在"（用户可能已经删了/移动了） */
export function diagnosticsFileExists(p: string | null): boolean {
  return !!p && existsSync(p)
}
