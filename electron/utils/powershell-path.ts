/**
 * PowerShell 可执行文件路径解析（共享 util，backend-manager 孤儿检测与
 * shortcut-win 快捷方式生成同源使用，避免两处各写一份候选表漂移）。
 */
import { existsSync } from 'fs'
import { join } from 'path'

/**
 * 必须优先用绝对路径：本应用经常从 PATH 被裁剪的环境启动（实测 `npm run dev`
 * 的父 shell 连 ls / dirname 都找不到），此时 spawn 裸名 'powershell' 会直接
 * ENOENT，孤儿检测/快捷方式生成静默失效。返回值回退为裸名，以兼容 PATH 正常
 * 的场景。exists 可注入（默认 existsSync），便于单测断言三级回退口径。
 */
export function resolvePowerShell(exists: (p: string) => boolean = existsSync): string {
  const root = process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows'
  const candidates = [
    join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
    // 32 位宿主进程访问 64 位 System32 时的别名路径
    join(root, 'Sysnative', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
  ]
  for (const c of candidates) {
    try {
      if (exists(c)) return c
    } catch {
      /* 忽略，继续尝试下一个候选 */
    }
  }
  return 'powershell'
}
