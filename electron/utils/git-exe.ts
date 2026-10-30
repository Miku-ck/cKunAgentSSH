/**
 * git 可执行文件定位。
 *
 * Electron 从资源管理器启动时拿到的 PATH 可能不含 Git for Windows（安装器写的是
 * 用户级 PATH，explorer.exe 的进程环境不会刷新），此时 execFile('git') 直接
 * ENOENT，界面把「找不到 git」报成「未安装 git」。这里按 PATH → 常见安装路径
 * 逐个探测，成功结果进程内缓存（装完 git 无需重启，失败不缓存）。
 */
import { execFile } from 'child_process'
import { existsSync } from 'fs'
import { join } from 'path'

/** 候选路径：'git' 表示走 PATH；其余为绝对路径。纯函数，便于测试。 */
export function gitCandidatePaths(
  env: NodeJS.ProcessEnv,
  platform: NodeJS.Platform = process.platform,
): string[] {
  const out: string[] = ['git']
  if (platform === 'win32') {
    const roots = [
      env.ProgramW6432,
      env.ProgramFiles,
      env['ProgramFiles(x86)'],
      env.LOCALAPPDATA ? join(env.LOCALAPPDATA, 'Programs') : undefined,
    ]
    for (const root of roots) {
      if (!root) continue
      out.push(join(root, 'Git', 'cmd', 'git.exe'))
    }
  } else {
    out.push('/usr/bin/git', '/usr/local/bin/git', '/opt/homebrew/bin/git')
  }
  return [...new Set(out)]
}

function probe(exe: string): Promise<string | null> {
  return new Promise((resolvePromise) => {
    execFile(exe, ['--version'], { timeout: 4000, windowsHide: true }, (err) => {
      resolvePromise(err ? null : exe)
    })
  })
}

let cachedExe: string | null = null

/** 解析可用的 git 可执行文件；找不到返回 null */
export async function resolveGitExecutable(): Promise<string | null> {
  if (cachedExe) return cachedExe
  for (const exe of gitCandidatePaths(process.env)) {
    // 绝对路径先看存在性，省一次进程创建
    if (exe !== 'git' && !existsSync(exe)) continue
    const found = await probe(exe)
    if (found) {
      cachedExe = found
      return found
    }
  }
  return null
}

/** 用户手动装了 git 之后强制重探 */
export function resetGitExecutableCache(): void {
  cachedExe = null
}
