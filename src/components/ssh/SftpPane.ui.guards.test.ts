/**
 * SftpPane 界面行为的形状守卫（S5-30 / S5-31）。
 *
 * 没有组件测试环境（未装 @vue/test-utils/jsdom），这里钉代码形状。两条都是
 * 「用户可感知的界面决策」，改回去很自然（输入框常驻更省事、v-if 换掉列表更
 * 简单），所以用测试把决策本身固定住：
 *
 *  S5-30 筛选入口收进标题栏图标，放在路径收藏左边。常驻输入框占掉一整个
 *        水平条 × 两栏 = 近 60px；收进图标后默认只多一个 22px 按钮。
 *  S5-31 导航中保留旧列表并压暗，而不是整批清空换成「加载中...」。v-if 换掉
 *        v-for 会让目录越大闪得越明显。
 */
import paneRaw from './SftpPane.vue?raw'
import { describe, expect, it } from 'vitest'

const src = paneRaw.replace(/\r\n/g, '\n')

describe('S5-30 筛选入口位置', () => {
  it('筛选按钮在路径收藏之前（pane-actions 内的相对顺序）', () => {
    const actionsAt = src.indexOf('class="pane-actions"')
    expect(actionsAt).toBeGreaterThanOrEqual(0)
    const filterAt = src.indexOf('@click="toggleFilter"', actionsAt)
    const bookmarkAt = src.indexOf('<SftpBookmarkMenu', actionsAt)
    expect(filterAt, '筛选按钮不在 pane-actions 里').toBeGreaterThanOrEqual(0)
    expect(bookmarkAt, '路径收藏不在 pane-actions 里').toBeGreaterThanOrEqual(0)
    expect(filterAt, '筛选按钮应在路径收藏左边').toBeLessThan(bookmarkAt)
  })

  it('筛选输入框与按钮同排（filter-inline 在 pane-actions 内）', () => {
    const actionsAt = src.indexOf('class="pane-actions"')
    const inlineAt = src.indexOf('class="filter-inline"')
    expect(inlineAt, '找不到 filter-inline').toBeGreaterThanOrEqual(0)
    expect(inlineAt, 'filter-inline 应在 pane-actions 内（与按钮同排）').toBeGreaterThan(actionsAt)
    // 旧的独立横条形状不得复现
    expect(src).not.toContain('class="filter-bar"')
  })

  it('有筛选关键字时按钮常亮（收起后仍看得出这一栏在筛）', () => {
    expect(src).toContain(':class="{ active: filterText }"')
  })

  it('Esc 清空关键字后连输入框一起收起', () => {
    const clearAt = src.indexOf('function clearFilter(')
    expect(clearAt).toBeGreaterThanOrEqual(0)
    const body = src.slice(clearAt, src.indexOf('\n}', clearAt) + 2)
    expect(body).toContain('filterOpen.value = false')
  })
})

describe('S5-31 导航中的加载反馈', () => {
  it('加载不再用 v-if 换掉整个列表', () => {
    // 旧形状：v-else-if="loading" 的 pane-loading 与 v-for 互斥
    expect(src).not.toContain('v-else-if="loading" class="pane-loading"')
  })

  it('压暗态挂在 file-list 上，进度条在列表顶部', () => {
    const line = src.split('\n').find(l => l.includes('file-list') && l.includes(':class='))
    expect(line, '找不到 file-list 的 :class 绑定行').toBeTruthy()
    expect(line).toContain('is-loading')
    expect(src).toContain('class="pane-loading-bar"')
  })

  it('错误态仍独占列表区（与压暗的旧列表并排会又挤又矛盾）', () => {
    const errAt = src.indexOf('v-if="error" class="pane-error"')
    expect(errAt).toBeGreaterThanOrEqual(0)
    const tplAt = src.indexOf('<template v-else>', errAt)
    expect(tplAt, '错误提示后应紧跟 template v-else 包住列表').toBeGreaterThan(errAt)
  })
})
