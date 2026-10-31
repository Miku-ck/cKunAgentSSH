/**
 * tar 可执行文件候选（electron/utils/tar-exe.ts）。
 *
 * 守卫的是「Windows 上拿到的是 bsdtar 而不是 GNU tar」这条既有假设：裸 `tar.exe`
 * 会被 Git for Windows 的 usr/bin 抢走（从 Git Bash 启动应用/脚本时），而 GNU tar
 * 把 `C:\...` 的盘符当成远端主机，直接 `Cannot connect to C: resolve failed`。
 */
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { resolveTarExecutable, tarCandidatePaths } from './tar-exe'

describe('tar 候选路径', () => {
  it('win32 首选 System32 的 bsdtar，裸名只作兜底', () => {
    expect(tarCandidatePaths({ SystemRoot: 'C:\\Windows' }, 'win32')).toEqual([
      join('C:\\Windows', 'System32', 'tar.exe'),
      'tar.exe',
    ])
  })

  it('SystemRoot 缺失时不拼出 undefined 路径，也认 windir/WINDIR 两种写法', () => {
    expect(tarCandidatePaths({}, 'win32')).toEqual(['tar.exe'])
    for (const key of ['windir', 'WINDIR']) {
      expect(tarCandidatePaths({ [key]: 'C:\\WINNT' }, 'win32')[0]).toBe(
        join('C:\\WINNT', 'System32', 'tar.exe'),
      )
    }
  })

  it('非 Windows 只有系统 tar —— 不引入 Windows 方言', () => {
    expect(tarCandidatePaths({ SystemRoot: 'C:\\Windows' }, 'linux')).toEqual(['tar'])
    expect(tarCandidatePaths({}, 'darwin')).toEqual(['tar'])
  })

  it('本机上解析结果真的是 Windows 自带 bsdtar（PATH 抢跑时这里就会红）', () => {
    const exe = resolveTarExecutable()
    if (process.platform === 'win32') {
      expect(exe).toContain(join('System32', 'tar.exe'))
    } else {
      expect(exe).toBe('tar')
    }
  })
})
