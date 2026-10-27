/// <reference types="node" />
/**
 * 浮层层级守卫 —— DESIGN.md「toast 必须最顶层」
 *
 * toast 长期停在 2000，和 BaseModal 同层：于是被确认框（3000）、模型弹窗（2100）、
 * 页面里几处 9999 浮层和命令面板（10000）盖住。那些场景下「保存失败」「已复制」
 * 这类提示等于没提示，用户只会觉得操作没生效 —— 这类回归在 code review 里看不出来，
 * 只能靠断言钉住「toast 严格高于每一个已知浮层」。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { SRC } from './fs-walk'

/** 一个文件里出现的最高 z-index（同一组件可能分好几层）。 */
function maxZ(name: string): number {
  const found = [...readFileSync(join(SRC, name), 'utf8').matchAll(/z-index:\s*(\d+)/g)].map((m) =>
    Number(m[1]),
  )
  expect(found.length, `${name} 里没有 z-index`).toBeGreaterThan(0)
  return Math.max(...found)
}

/** 已知浮层：任何一个高过 toast 都会吞掉提示。 */
const LAYERS = [
  'components/common/BaseModal.vue',
  'components/common/ConfirmDialog.vue',
  'components/common/SettingsDialog.vue',
  'components/common/CommandPalette.vue',
  'components/settings/panels/ModelsPanel.vue',
  'components/ssh/QuickCommandBar.vue',
  'components/ssh/RemoteManager.vue',
  'components/ssh/SftpView.vue',
  'views/ssh/SshView.vue',
  'styles/docker-modal.css',
]

describe('浮层层级', () => {
  it('toast 严格高于每个已知浮层', () => {
    const toast = maxZ('components/common/ToastContainer.vue')
    for (const name of LAYERS) {
      expect(toast, `${name} 会盖住 toast`).toBeGreaterThan(maxZ(name))
    }
  })

  it('DESIGN.md 记着这一层', () => {
    const design = readFileSync(join(SRC, '..', 'DESIGN.md'), 'utf8')
    expect(design).toContain('11000')
  })
})
