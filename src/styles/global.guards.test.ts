/**
 * global.css 守卫 —— 焦点可见性契约
 * 钉：:focus-visible 只加描边，不改元素圆角。早先那句 `border-radius: 2px`
 * 会把按钮/输入框自己的圆角覆盖成 2px（焦点一到，方形按钮变圆角），
 * 手记主题下尤其突兀。
 *
 * 另钉一条底线：全仓 30 余处 `outline: none`里有一部分长在 input/range/switch
 * 上，它们的权重高于全局伪类 —— 没有这条 !important 底线，Tab 过去就是没焦点。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { SRC } from './fs-walk'

const src = readFileSync(
  fileURLToPath(new URL('./global.css', import.meta.url)),
  'utf8',
).replace(/\r\n/g, '\n')

const focusBlock = src.slice(src.indexOf(':focus-visible {'), src.indexOf('}', src.indexOf(':focus-visible {')))

describe('global.css 焦点可见', () => {
  it(':focus-visible 有 2px 主色描边', () => {
    expect(focusBlock).toContain('outline: 2px solid var(--accent-primary)')
  })
  it('不覆写元素自身的圆角', () => {
    expect(focusBlock).not.toContain('border-radius')
  })
})

const controlBlock = src.slice(
  src.indexOf('input:focus-visible'),
  src.indexOf('}', src.indexOf('input:focus-visible')),
)

describe('表单控件焦点底线', () => {
  it('圈住四类控件，且带 !important（要压住组件里的 outline: none）', () => {
    for (const sel of ['input:focus-visible', 'select:focus-visible', 'textarea:focus-visible', '[role="switch"]:focus-visible']) {
      expect(src, `缺少 ${sel}`).toContain(sel)
    }
    expect(controlBlock).toContain('outline: 2px solid var(--accent-primary) !important')
  })

  it('开关与滑块不再自己摘掉焦点环', () => {
    for (const name of ['components/settings/fields/ToggleSwitch.vue', 'components/settings/fields/SettingSlider.vue']) {
      expect(readFileSync(join(SRC, name), 'utf8'), `${name} 仍写着 outline: none`).not.toMatch(/outline:\s*none/)
    }
  })
})
