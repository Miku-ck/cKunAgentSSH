/**
 * 过滤 ANSI 转义序列中的背景色代码，保留前景色、加粗、下划线等其他属性。
 * 直接在 xterm 写入前处理，不依赖 SSH 服务器是否接受 LS_COLORS 环境变量。
 *
 * 过滤范围：
 * - 标准背景色 40-49
 * - 高亮背景色 100-107
 * - 256 色背景 ESC[48;5;Nm
 * - True Color 背景 ESC[48;2;R;G;Bm
 *
 * 关键：48;5;N 和 48;2;R;G;B 是多参数序列，必须整体识别跳过，
 * 不能用 split(';') 拆成独立数字（会导致 5/2/R/G/B 被误当作闪烁/暗淡等属性）。
 */
export function stripAnsiBackground(data: string): string {
  return data.replace(/\x1b\[([0-9;]*)m/g, (match, codes: string) => {
    if (!codes) return match
    const parts = codes.split(';')
    const tokens: string[] = []
    let i = 0
    while (i < parts.length) {
      const n = parseInt(parts[i], 10)
      // 256 色背景 48;5;N — 整体跳过
      if (n === 48 && parts[i + 1] === '5') {
        i += 3
        continue
      }
      // True Color 背景 48;2;R;G;B — 整体跳过
      if (n === 48 && parts[i + 1] === '2') {
        i += 5
        continue
      }
      // 256 色前景 38;5;N — 保留
      if (n === 38 && parts[i + 1] === '5') {
        tokens.push(parts[i], parts[i + 1], parts[i + 2])
        i += 3
        continue
      }
      // True Color 前景 38;2;R;G;B — 保留
      if (n === 38 && parts[i + 1] === '2') {
        tokens.push(parts[i], parts[i + 1], parts[i + 2], parts[i + 3], parts[i + 4])
        i += 5
        continue
      }
      // 标准背景 40-49，高亮背景 100-107 — 移除
      if ((n >= 40 && n <= 49) || (n >= 100 && n <= 107)) {
        i++
        continue
      }
      // 其他属性（前景色、加粗、下划线等）— 保留
      tokens.push(parts[i])
      i++
    }
    return tokens.length ? `\x1b[${tokens.join(';')}m` : ''
  })
}
