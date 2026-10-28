import { describe, expect, it } from 'vitest'
import { isImageAvatar } from './persona-avatar'

const IMAGE = 'data:image/webp;base64,AAAA'

describe('人格头像', () => {
  it('识别上传图片的 data URL，不把表情/文本当头像', () => {
    expect(isImageAvatar(IMAGE)).toBe(true)
    expect(isImageAvatar('data:image/png;base64,AAAA')).toBe(true)
    expect(isImageAvatar('🐟')).toBe(false)
    expect(isImageAvatar('data:text/plain;base64,AAAA')).toBe(false)
    expect(isImageAvatar('')).toBe(false)
    expect(isImageAvatar(null)).toBe(false)
  })
})
