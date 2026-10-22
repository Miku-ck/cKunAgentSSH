import { describe, expect, it } from 'vitest'
import { nextFocusIndex } from './focus-trap'

describe('nextFocusIndex：模态内的 Tab 循环', () => {
  it('正向走到最后一项后回到第一项', () => {
    expect(nextFocusIndex(3, 2, 1)).toBe(0)
  })

  it('反向走到第一项后回到最后一项', () => {
    expect(nextFocusIndex(3, 0, -1)).toBe(2)
  })

  it('中间项按方向移动一格', () => {
    expect(nextFocusIndex(3, 1, 1)).toBe(2)
    expect(nextFocusIndex(3, 1, -1)).toBe(0)
  })

  it('焦点不在集合内时：正向落到第一项，反向落到最后一项', () => {
    expect(nextFocusIndex(3, -1, 1)).toBe(0)
    expect(nextFocusIndex(3, -1, -1)).toBe(2)
    expect(nextFocusIndex(3, 9, 1)).toBe(0)
    expect(nextFocusIndex(3, 9, -1)).toBe(2)
  })

  it('没有可聚焦项时返回 -1（调用方据此把焦点交还给容器）', () => {
    expect(nextFocusIndex(0, -1, 1)).toBe(-1)
  })
})
