/**
 * tar 可执行文件定位 —— Windows 上必须是 System32 的 bsdtar。
 *
 * 三处调用（updater 解包、diagnostics 打包、package.js 出件）都写着「Windows 10+ 自带
 * System32\tar.exe」，但传的是裸 `tar.exe`，命中谁实际由 PATH 顺序决定。从 Git Bash
 * 启动时 Git for Windows 的 usr/bin 排在 System32 之前，拿到的是 GNU tar：它把 `-czf`
 * 输出路径 `C:\...` 里的 `C:` 当成远端主机去解析，报
 * `tar (child): Cannot connect to C: resolve failed` 并退出 2 —— 自更新解包与诊断包
 * 导出在这类开发机上必炸（updater-e2e / diagnostics-e2e 实测复现，两条链路的单元测试
 * 全绿，因为它们的 tar 都走注入点）。这里把那句注释里的假设变成代码。
 *
 * 与 electron/utils/git-exe.ts 同一取舍：先绝对路径、探不到再退回 PATH。
 */
import { existsSync } from 'node:fs'
import { isAbsolute, join } from 'node:path'

/**
 * 候选路径，纯函数便于单测。
 * win32：System32\tar.exe（bsdtar）优先，其次裸 `tar.exe` 交给 PATH；
 * 其余平台：只有系统 `tar`，方言差异不在本项目的交付面上。
 */
export function tarCandidatePaths(
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): string[] {
  if (platform !== 'win32') return ['tar']
  const systemRoot = env.SystemRoot ?? env.windir ?? env.WINDIR
  return systemRoot ? [join(systemRoot, 'System32', 'tar.exe'), 'tar.exe'] : ['tar.exe']
}

let cached: string | null = null

/**
 * 可用的 tar 可执行文件路径。进程内只探一次：tar 属于系统组件，不会在运行中途被换。
 * 一个都不存在时仍返回裸名，让失败以 spawn ENOENT 的形式原样暴露，而不是被这里吞掉。
 */
export function resolveTarExecutable(): string {
  if (cached) return cached
  const candidates = tarCandidatePaths()
  cached = candidates.find((p) => !isAbsolute(p) || existsSync(p)) ?? candidates[0]
  return cached
}
