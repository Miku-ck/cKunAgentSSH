/**
 * 远端目录项名的安全判定（S5-13）。
 *
 * SFTP 列表里的 `name` 完全由服务器书写，而下载侧把它当 relPath 一路拼进**本地**
 * 写路径（`sftp.ts` expandRemoteDir → `fs:pathJoin`）。`fs:pathJoin` 会归一化掉
 * `..`，`assertSafePath` 是敏感目录黑名单而不是 jail —— 于是 `../../x` 这类名字
 * 能把落点挪出用户选定的目录，且不会撞到黑名单（家目录里绝大多数位置都不在名单上）。
 * 判据放在这里而不是主进程：只有渲染层知道「用户选的下载目录」这个 jail 的边界，
 * 而畸形名字本来就没有可安全落地的形态。
 */

/** 单个目录项名是否不能安全拼进本地路径 */
export function isUnsafeRemoteName(name: string): boolean {
  if (!name || name === '.' || name === '..') return true
  if (name.includes('\0')) return true
  // POSIX 文件名里 `\\` 合法，但拼进本地路径后 Windows 侧会把它当分隔符重新解释，
  // 两侧的语义分歧不值得留给用户踩 —— 保守一起拒掉，代价是极罕见的怪名字要手动改名
  return /[/\\]/.test(name)
}
