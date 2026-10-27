/// <reference types="node" />
/**
 * 遮罩令牌守卫 —— DESIGN.md「浮层遮罩一律 var(--scrim)」
 *
 * 字面 rgba(0,0,0,.6) 在深色主题下看着没问题，切到浅色/手记主题就压出一层
 * 冷黑纱（同 BaseModal/HomeHistoryDialog 已统一到 --scrim）。这里扫全仓，
 * 防止新弹窗又把 0.6 写回来。
 *
 * 另：`--scrim` 单独用不够 —— 深色主题的 scrim 只有 34%，靠 2px 模糊补回层次
 * （浅色/手记主题的 scrim 更淡，只有 14%~16%，全靠模糊和投影分开）。
 * 所以每条 `background: var(--scrim)` 都必须在同一声明块附近带 `blur(2px)`。
 *
 * 两处例外，各有用意：
 *  - App.vue：应用背景叠层读 --app-bg-overlay，用户可调，不是弹窗遮罩；
 *  - ConnectionModal.vue：他人未提交改动占着该文件，合并后再统一（见台账）。
 */
import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, relative } from 'node:path'

const SRC = fileURLToPath(new URL('..', import.meta.url))
const ALLOWED = ['App.vue', 'ConnectionModal.vue']

function walk(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      if (name === 'node_modules') continue
      out.push(...walk(full))
    } else if (name.endsWith('.vue') || name.endsWith('.css')) {
      out.push(full)
    }
  }
  return out
}

const maskPattern = /background:\s*rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*\.?\d/

describe('浮层遮罩走令牌', () => {
  const offenders = walk(SRC)
    .filter((f) => maskPattern.test(readFileSync(f, 'utf8')))
    .map((f) => relative(SRC, f).replace(/\\/g, '/'))
    .filter((f) => !ALLOWED.some((a) => f.endsWith(a)))

  it('没有新的字面遮罩漏进来', () => {
    expect(offenders, `这些文件仍写着字面遮罩：${offenders.join(', ')}`).toEqual([])
  })

  it('已知弹窗里确实用上 var(--scrim)', () => {
    const known = [
      'components/common/BaseModal.vue',
      'components/common/ConfirmDialog.vue',
      'components/common/SettingsDialog.vue',
      'components/common/CommandPalette.vue',
      'components/ssh/AccessModal.vue',
      'components/ssh/BulkCommandModal.vue',
      'components/ssh/GatewayDialogs.vue',
      'components/ssh/NewConnectionDialog.vue',
      'components/ssh/SftpConflictDialog.vue',
      'components/ssh/SftpImagePreview.vue',
      'components/ssh/SftpTransferDialog.vue',
      'components/settings/panels/ModelsPanel.vue',
      'styles/docker-modal.css',
    ]
    for (const rel of known) {
      const text = readFileSync(join(SRC, rel), 'utf8')
      expect(text, `${rel} 未使用 --scrim`).toContain('var(--scrim)')
    }
  })

  it('每条遮罩都带 blur(2px)', () => {
    const missing: string[] = []
    for (const file of walk(SRC)) {
      const lines = readFileSync(file, 'utf8').replace(/\r\n/g, '\n').split('\n')
      lines.forEach((line, i) => {
        if (!line.includes('background: var(--scrim)')) return
        // 声明块可能写成一行，也可能换行展开：允许前后 3 行内出现模糊
        const block = lines.slice(Math.max(0, i - 3), i + 4).join('\n')
        if (!block.includes('backdrop-filter: blur(2px)')) {
          missing.push(`${relative(SRC, file).replace(/\\/g, '/')}:${i + 1}`)
        }
      })
    }
    expect(missing, `这些遮罩缺 blur(2px)：${missing.join(', ')}`).toEqual([])
  })

  it('DESIGN.md 写死了这条规则，不再留字面遮罩范例', () => {
    const design = readFileSync(join(SRC, '..', 'DESIGN.md'), 'utf8')
    expect(design).toContain('var(--scrim)')
    expect(design).not.toContain('rgba(0,0,0,.6)')
  })
})
