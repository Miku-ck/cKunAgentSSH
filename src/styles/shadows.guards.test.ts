/// <reference types="node" />
/**
 * 浮层投影守卫 —— DESIGN.md「浮层投影一律 var(--shadow-lg)」
 *
 * 弹窗家族里投影深浅不一是上一轮审查挑出来的毛病：BaseModal 与 docker 弹窗读
 * --shadow-lg（0 8px 24px / 40%），设置/模型/快速命令三个弹窗却各写一份
 * `0 16px 48px rgba(0,0,0,.5)`，确认框又是第三档 `0 12px 40px`。同一屏里两个
 * 弹窗投影差两档，说不出理由。这里把「一律」钉住：新增弹窗照抄令牌，别再抄 px。
 *
 * 控件级微投影（开关、滑块的 `0 1px 2px`）不在此列 —— 它们不是浮层。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { SRC, rel, walk } from './fs-walk'

/** 弹窗家族：有遮罩、有自己的 dialog 面。 */
const DIALOGS = [
  'components/common/BaseModal.vue',
  'components/common/CommandPalette.vue',
  'components/common/ConfirmDialog.vue',
  'components/common/SettingsDialog.vue',
  'components/settings/panels/ModelsPanel.vue',
  'components/ssh/AccessModal.vue',
  'components/ssh/QuickCommandEditor.vue',
  'components/ssh/SftpConflictDialog.vue',
  'components/ssh/SftpTransferDialog.vue',
  'components/ssh/TunnelModal.vue',
  'styles/docker-modal.css',
]

describe('浮层投影走令牌', () => {
  it('弹窗家族用的都是 var(--shadow-lg)', () => {
    for (const name of DIALOGS) {
      const text = readFileSync(join(SRC, name), 'utf8')
      expect(text, `${name} 未使用 --shadow-lg`).toContain('box-shadow: var(--shadow-lg)')
      expect(text, `${name} 仍有字面 px 投影`).not.toMatch(/box-shadow:\s*0 \d+px \d+px rgba\(/)
    }
  })

  it('「大弹窗专用」的两份字面投影已下线', () => {
    const magic = /box-shadow:\s*0 (12px 40px|16px 48px)/
    const offenders = walk()
      .filter((file) => magic.test(readFileSync(file, 'utf8')))
      .map((file) => rel(file))
    expect(offenders, `这些文件仍写着字面弹窗投影：${offenders.join(', ')}`).toEqual([])
  })
})
