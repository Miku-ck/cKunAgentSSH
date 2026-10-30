/**
 * git 可执行文件定位候选（electron/utils/git-exe.ts）。
 * Electron 从资源管理器启动时 PATH 可能不含 Git for Windows，必须有绝对路径兜底。
 */
import { join } from 'path'
import { describe, expect, it } from 'vitest'
import { gitCandidatePaths } from './git-exe'

describe('git 候选路径', () => {
  it('永远先试 PATH 里的 git（用户自己改过 PATH 时优先尊重）', () => {
    expect(gitCandidatePaths({}, 'win32')[0]).toBe('git')
    expect(gitCandidatePaths({}, 'linux')[0]).toBe('git')
  })

  it('win32 覆盖三档 Program Files 与用户级安装目录，且去重', () => {
    const programFiles = join('C:', 'Program Files')
    const out = gitCandidatePaths(
      {
        ProgramW6432: programFiles,
        ProgramFiles: programFiles,
        'ProgramFiles(x86)': join('C:', 'Program Files (x86)'),
        LOCALAPPDATA: join('C:', 'Users', 'u', 'AppData', 'Local'),
      },
      'win32',
    )
    const expected = join(programFiles, 'Git', 'cmd', 'git.exe')
    expect(out).toContain(expected)
    expect(out).toContain(join('C:', 'Program Files (x86)', 'Git', 'cmd', 'git.exe'))
    expect(out).toContain(join('C:', 'Users', 'u', 'AppData', 'Local', 'Programs', 'Git', 'cmd', 'git.exe'))
    // ProgramW6432 与 ProgramFiles 常常同值：不能出现两条一样的候选
    expect(out.filter(p => p === expected)).toHaveLength(1)
    expect(new Set(out).size).toBe(out.length)
  })

  it('环境变量缺失时只留能用的候选（不拼出 undefined 路径）', () => {
    expect(gitCandidatePaths({}, 'win32')).toEqual(['git'])
  })

  it('非 Windows 平台走常见安装目录', () => {
    const out = gitCandidatePaths({}, 'linux')
    expect(out).toEqual(['git', '/usr/bin/git', '/usr/local/bin/git', '/opt/homebrew/bin/git'])
  })
})
