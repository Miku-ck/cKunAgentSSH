import { describe, expect, it } from 'vitest'
import { mruInitialIndex, mruStep } from './useGlobalShortcuts'

describe('MRU 切换器的纯函数', () => {
  it('打开时高亮落在「上一个会话」（栈顶下一位）', () => {
    expect(mruInitialIndex(2)).toBe(1)
    expect(mruInitialIndex(9)).toBe(1)
  })

  it('连点 Tab 在栈内循环移动', () => {
    expect(mruStep(3, 1, 1)).toBe(2)
    expect(mruStep(3, 2, 1)).toBe(0)
    expect(mruStep(3, 0, -1)).toBe(2)
  })
})
