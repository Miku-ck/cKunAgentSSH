/**
 * 主进程路径安全校验
 * 敏感路径拦截逻辑自 main.ts 迁入（行为保持不变），并新增私钥文件路径白名单校验。
 */
import { app } from 'electron'
import { homedir } from 'os'
import { join, dirname, basename, resolve as pathResolve } from 'path'
import { realpath } from 'fs/promises'
import { realpathSync } from 'fs'

/** 主目录顶层敏感文件/目录（仅在用户主目录顶层时拦截） */
export const HOME_TOPLEVEL_SENSITIVE = new Set([
  '.ssh', '.aws', '.gnupg', '.gpg', '.kube', '.docker',
  '.env', '.npmrc', '.yarnrc',
])

/**
 * 任意路径段都拦截的敏感目录名。
 * 这些目录几乎不会作为合法项目子目录出现，且包含凭证，因此任何位置都拒绝。
 * 注意：`.env` / `.npmrc` / `.yarnrc` / `.docker` / `.gpg` 不在此列——
 * 它们常作为项目内合法文件/目录存在，仅在主目录顶层拦截即可。
 */
export const ANYWHERE_SENSITIVE = new Set([
  '.ssh', '.aws', '.gnupg', '.kube',
])

/**
 * 检查路径是否访问敏感目录，是则抛异常。
 * M2 修复：先通过 realpath 解析符号链接，防止渲染进程利用 symlink 绕过检查。
 * M6 修复：`.env` / `.npmrc` 等仅在主目录顶层拦截，不再误伤项目内同名文件。
 */
export async function assertSafePath(targetPath: string): Promise<void> {
  let resolved = pathResolve(targetPath)
  // M2：解析符号链接——目标存在时直接 realpath；不存在时回退到父目录 realpath + basename
  try {
    resolved = await realpath(resolved)
  } catch {
    try {
      const parentReal = await realpath(dirname(resolved))
      resolved = join(parentReal, basename(resolved))
    } catch {
      // 父目录也不存在：使用原始 resolve 结果（后续检查仍基于字面路径）
    }
  }
  const home = homedir()
  const homeResolved = pathResolve(home)
  // 检查是否直接访问主目录下的敏感子目录
  // 审查修复：用 isInside（Windows 大小写归一 + 分隔符边界）替代裸 startsWith，
  // 否则 homedir() 大小写与 realpath 规范形式不一致时（C:\Users\CKUN vs ...\ckun）
  // 整个顶层敏感块被跳过，.env/.npmrc/.yarnrc/.docker/.gpg（仅在此列）绕过拦截
  if (isInside(resolved, homeResolved)) {
    const relative = resolved.slice(homeResolved.length).replace(/^[\\/]+/, '')
    let topLevel = relative.split(/[\\/]/)[0]
    // E19 修复：Windows 路径大小写不敏感 — 先归一化再比对，
    // 否则 `.SSH` / `.Ssh` 可绕过主目录顶层敏感目录拦截
    topLevel = normPath(topLevel)
    if (topLevel && HOME_TOPLEVEL_SENSITIVE.has(topLevel)) {
      throw new Error(`拒绝访问敏感路径: ${targetPath}`)
    }
  }
  // 检查绝对路径中的任意位置敏感目录名（仅限凭证类目录）
  // 审查修复：Windows 路径大小写不敏感 — 段比对须小写化，
  // 否则 `C:\Users\x\.SSH` / `.AWS` 可绕过任意位置拦截（顶层检查已有 E19，此处补齐）
  for (const seg of resolved.split(/[\\/]/)) {
    if (ANYWHERE_SENSITIVE.has(normPath(seg))) {
      throw new Error(`拒绝访问敏感路径: ${targetPath}`)
    }
  }
}

/**
 * 通过文件选择对话框选取后允许作为私钥读取的路径集合（存 realpath 后的绝对路径）。
 * 修复 1：私钥读取绕过路径校验的加固——只有本集合、~/.ssh 或 userData 之下的路径可读。
 */
const dialogAllowedKeyPaths = new Set<string>()

/** 把通过文件选择对话框选取的文件路径加入私钥白名单（入参应为 realpath 后的路径） */
export function allowDialogKeyPath(p: string): void {
  if (p) dialogAllowedKeyPaths.add(p)
}

/** 平台感知的路径归一化（Windows 忽略大小写，POSIX 保持精确匹配） */
function normPath(p: string): string {
  return process.platform === 'win32' ? p.toLowerCase() : p
}

/** 判断 resolved 是否位于 base 之下（含相等），边界按路径分隔符 */
function isInside(resolved: string, base: string): boolean {
  const sep = process.platform === 'win32' ? '\\' : '/'
  const nResolved = normPath(resolved)
  const nBase = normPath(base)
  return nResolved === nBase || nResolved.startsWith(nBase + sep)
}

/**
 * 私钥文件路径白名单校验（修复 1）：
 * realpath 解析后，仅当真实路径位于「用户主目录/.ssh 之下」或「app userData 之下」或
 * 「dialogAllowedKeyPaths（文件选择对话框选取）集合中」时放行，否则抛出中文提示 Error。
 */
export function assertKeyFileSafe(keyPath: string): void {
  let resolved: string
  try {
    resolved = realpathSync(keyPath)
  } catch {
    throw new Error(`私钥文件不存在或不可访问: ${keyPath}`)
  }
  const homeSsh = join(homedir(), '.ssh')
  const userData = app.getPath('userData')
  const allowedByDialog = [...dialogAllowedKeyPaths].some((p) => normPath(p) === normPath(resolved))
  if (isInside(resolved, homeSsh) || isInside(resolved, userData) || allowedByDialog) {
    return
  }
  throw new Error('私钥路径不受信任：请将私钥放入 ~/.ssh 目录，或通过文件选择对话框选取密钥文件后重试')
}
