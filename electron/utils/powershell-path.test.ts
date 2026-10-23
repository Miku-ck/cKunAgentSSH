/**
 * PowerShell 路径解析 util：注入 exists 桩断言三级回退口径
 * （System32 优先 → Sysnative 兜底 → 全缺回退裸名）。
 */
import { join } from 'path'
import { describe, expect, it } from 'vitest'
import { resolvePowerShell } from './powershell-path'

describe('resolvePowerShell', () => {
  // 候选路径用与实现同一套 join 拼装：POSIX CI 上分隔符不同，硬编码反斜杠会误报
  const root = process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows'
  const sys32 = join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
  const sysnative = join(root, 'Sysnative', 'WindowsPowerShell', 'v1.0', 'powershell.exe')

  it('System32 命中优先', () => {
    expect(resolvePowerShell((p) => p === sys32)).toBe(sys32)
  })
  it('System32 缺失 → Sysnative 兜底（32 位宿主看 64 位系统卷）', () => {
    expect(resolvePowerShell((p) => p === sysnative)).toBe(sysnative)
  })
  it('全缺 → 裸名 powershell（兼容 PATH 正常场景），exists 抛错也不炸', () => {
    expect(resolvePowerShell(() => false)).toBe('powershell')
    expect(resolvePowerShell(() => { throw new Error('boom') })).toBe('powershell')
  })
})
