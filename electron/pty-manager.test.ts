/**
 * S4-01 回归：PTY shell 白名单的判据。
 *
 * 旧判据按 basename 后缀放行，任何目录下改名放着的 cmd.exe 都能起一个带完整
 * preload API 面的 PTY 会话。抽成纯函数后这里可以脱离 Electron/ node-pty 跑真断言
 * （本仓 pty 侧此前零测试）。
 */
import { describe, expect, it } from 'vitest'

import { buildShellTrustContext, isAllowedShell } from './pty-manager'

const WIN_SHELLS = ['cmd.exe', 'powershell.exe', 'pwsh.exe', 'bash.exe', 'git-bash.exe', 'wsl.exe']
const POSIX_SHELLS = ['/bin/zsh', '/bin/bash', '/bin/sh', '/usr/bin/zsh', '/usr/bin/bash']

const winCtx = {
  caseInsensitive: true,
  pathDirs: ['C:\\Git\\bin', 'C:\\Windows\\System32'],
  systemRoots: ['C:\\Windows', 'C:\\Program Files'],
}

describe('isAllowedShell', () => {
  it('bare 名与标准绝对路径照常放行', () => {
    expect(isAllowedShell('cmd.exe', WIN_SHELLS, winCtx)).toBe(true)
    expect(isAllowedShell('C:\\Windows\\System32\\cmd.exe', WIN_SHELLS, winCtx)).toBe(true)
    expect(isAllowedShell('C:\\Program Files\\PowerShell\\7\\pwsh.exe', WIN_SHELLS, winCtx)).toBe(true)
  })

  it('PATH 上的非标准安装位置不被打掉（Git 装在 C:\\Git 的真实用户场景）', () => {
    expect(isAllowedShell('C:\\Git\\bin\\bash.exe', WIN_SHELLS, winCtx)).toBe(true)
  })

  it('任意目录下的同名可执行文件不再算命中白名单', () => {
    expect(isAllowedShell('C:\\Users\\me\\AppData\\Local\\Temp\\cmd.exe', WIN_SHELLS, winCtx)).toBe(false)
    expect(isAllowedShell('D:\\evil\\powershell.exe', WIN_SHELLS, winCtx)).toBe(false)
    // 旧判据正是靠 endsWith('\\cmd.exe') 放掉这类输入
  })

  it('basename 不在白名单里一律拒绝，哪怕目录受信任', () => {
    expect(isAllowedShell('C:\\Windows\\System32\\notepad.exe', WIN_SHELLS, winCtx)).toBe(false)
    expect(isAllowedShell('C:\\Git\\bin\\curl.exe', WIN_SHELLS, winCtx)).toBe(false)
  })

  it('既不是白名单项也不是路径的输入拒绝', () => {
    expect(isAllowedShell('rm -rf /', WIN_SHELLS, winCtx)).toBe(false)
    expect(isAllowedShell('', WIN_SHELLS, winCtx)).toBe(false)
  })

  it('POSIX：精确项与标准 bin 目录放行，用户缓存目录里的同名文件拒绝', () => {
    const ctx = {
      caseInsensitive: false,
      pathDirs: ['/opt/homebrew/bin'],
      systemRoots: ['/bin', '/usr/bin', '/usr/local/bin'],
    }
    expect(isAllowedShell('/bin/zsh', POSIX_SHELLS, ctx)).toBe(true)
    expect(isAllowedShell('/usr/local/bin/zsh', POSIX_SHELLS, ctx)).toBe(true)
    expect(isAllowedShell('/opt/homebrew/bin/bash', POSIX_SHELLS, ctx)).toBe(true)
    expect(isAllowedShell('/home/me/.cache/bin/bash', POSIX_SHELLS, ctx)).toBe(false)
    // 大小写敏感：/BIN/bash 不是放行路径
    expect(isAllowedShell('/Bin/bash', POSIX_SHELLS, ctx)).toBe(false)
  })
})

describe('buildShellTrustContext', () => {
  it('Windows 用分号切 PATH、取 SystemRoot 与 Program Files、大小写不敏感', () => {
    const ctx = buildShellTrustContext('win32', {
      SystemRoot: 'C:\\Windows',
      PATH: 'C:\\a;C:\\b',
      ProgramFiles: 'C:\\Program Files',
    })
    expect(ctx.caseInsensitive).toBe(true)
    expect(ctx.pathDirs).toEqual(['C:\\a', 'C:\\b'])
    expect(ctx.systemRoots).toContain('C:\\Windows')
    expect(ctx.systemRoots).toContain('C:\\Program Files')
  })

  it('POSIX 用冒号切 PATH，且不含 LOCALAPPDATA 之类的用户可写目录', () => {
    const ctx = buildShellTrustContext('linux', { PATH: '/usr/bin:/bin', SystemRoot: 'C:\\Windows' })
    expect(ctx.caseInsensitive).toBe(false)
    expect(ctx.pathDirs).toEqual(['/usr/bin', '/bin'])
    expect(ctx.systemRoots).toEqual(['/bin', '/usr/bin', '/usr/local/bin', '/sbin', '/usr/sbin'])
  })
})
