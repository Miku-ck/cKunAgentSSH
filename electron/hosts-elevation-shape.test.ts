/**
 * hosts 提权写入的进程链路守卫（读源码断言，不启动 Electron）。
 *
 * 两个真实故障都只会在"非管理员 + PATH 被裁剪"的机器上复现，单测跑不了 UAC，
 * 所以把实现形态钉死：
 * 1) 裸名 `powershell.exe`：本应用的 PATH 里可能只剩 PowerShell 7 的目录，裸名直接
 *    ENOENT，被上层映射成"退出码 1"，界面报「提权写入 hosts 失败: 退出码 1」却
 *    拿不到任何原因（项目里 backend-manager / shortcut-win 早就因此改用
 *    resolvePowerShell()，hosts 这里是最后一个漏网处）。
 * 2) `Start-Process ... | ForEach-Object { exit $_.ExitCode }`：实测管道里的 exit 会把
 *    整个管道吞掉 —— 退出码恒 0、子进程根本没跑，失败还会被当成"成功但没生效"。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('./gh-hosts.ts', import.meta.url), 'utf-8').replace(/\r\n/g, '\n')
/** 去掉注释行：反向断言只针对真正的代码，不能被解释性注释误伤 */
const code = src
  .split('\n')
  .filter(l => !l.trim().startsWith('//') && !l.trim().startsWith('*'))
  .join('\n')

describe('hosts 提权写入链路', () => {
  it('用 resolvePowerShell() 的绝对路径启动，不用裸名 powershell.exe', () => {
    expect(src).toContain("import { resolvePowerShell } from './utils/powershell-path'")
    expect(src).toContain('const ps = resolvePowerShell()')
    // execFile 的第一段参数里不能再出现裸名
    expect(code).not.toMatch(/execFile\(\s*'powershell/i)
    expect(code).not.toMatch(/Start-Process powershell\.exe/)
  })

  it('Start-Process 用 -PassThru 传退出码，不用会被吞掉的管道 exit 形式', () => {
    expect(src).toContain('-PassThru')
    expect(src).toContain('exit $proc.ExitCode')
    expect(code).not.toContain('| ForEach-Object { exit')
  })

  it('提权子进程把真实结果写回文件：失败原因不再丢成"退出码 1"', () => {
    expect(src).toContain("Set-Content -LiteralPath $result -Value 'OK'")
    expect(src).toContain("'FAIL: ' + $_.Exception.Message")
    expect(src).toContain("outcome.startsWith('FAIL:')")
    // 结果是 PS 5.1 写的 UTF-8（带 BOM），读回必须剥掉
    expect(src).toContain("replace(/^\\uFEFF/, '')")
  })

  it('ENOENT / UAC 超时都有中文原因，不再落到裸退出码', () => {
    expect(src).toContain('未找到 Windows PowerShell')
    expect(src).toContain('等待管理员授权超时')
  })
})
