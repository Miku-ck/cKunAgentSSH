import { describe, expect, it } from 'vitest'
import { MAX_CHAT_IMAGES, isChatImage, readChatImage } from '@/utils/chat-image'

describe('chat-image', () => {
  it('最多 4 张', () => {
    expect(MAX_CHAT_IMAGES).toBe(4)
  })

  it('isChatImage 只认图片 data URL', () => {
    expect(isChatImage('data:image/webp;base64,AAAA')).toBe(true)
    expect(isChatImage('data:text/plain;base64,AAAA')).toBe(false)
  })

  it('非图片文件直接报错，不触达 canvas', async () => {
    await expect(readChatImage(new File(['x'], 'a.txt', { type: 'text/plain' }))).rejects.toThrow('请选择图片文件')
  })
})
