import { describe, expect, it } from 'vitest'
import { imageMime, isImagePreviewable, isPreviewable } from '@/utils/language'

describe('图片预览白名单（png/jpg/jpeg/gif/webp）', () => {
  it('命中四类图片，扩展名大小写不敏感', () => {
    for (const n of ['a.png', 'b.jpg', 'c.JPEG', 'd.Gif', 'e.webp']) {
      expect(isImagePreviewable(n)).toBe(true)
    }
  })

  it('MIME 正确，jpeg/jpg 都归 image/jpeg', () => {
    expect(imageMime('x.PNG')).toBe('image/png')
    expect(imageMime('x.jpg')).toBe('image/jpeg')
    expect(imageMime('x.jpeg')).toBe('image/jpeg')
    expect(imageMime('x.gif')).toBe('image/gif')
    expect(imageMime('x.webp')).toBe('image/webp')
  })

  it('非白名单（含 svg/bmp/无扩展名）不预览、mime 为 null', () => {
    for (const n of ['a.svg', 'a.bmp', 'a.ico', 'a.tiff', 'README', 'a.pdf']) {
      expect(isImagePreviewable(n)).toBe(false)
      expect(imageMime(n)).toBeNull()
    }
  })

  it('图片与文本预览互斥：png 不是可编辑文本，ts 不是图片', () => {
    expect(isPreviewable('a.png')).toBe(false)
    expect(isImagePreviewable('a.png')).toBe(true)
    expect(isImagePreviewable('a.ts')).toBe(false)
    expect(isPreviewable('a.ts')).toBe(true)
  })
})
