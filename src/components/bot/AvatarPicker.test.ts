import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const src = readFileSync(resolve(__dirname, '../../../src/components/bot/AvatarPicker.vue'), 'utf8')

describe('头像选择器 AvatarPicker', () => {
  it('预览 / 从图库选 / 上传 三来源齐备，压缩口径只沿用 readAvatarFile 一份', () => {
    for (const needle of ['PersonaAvatar', '从图库选', '上传图片', 'listGallery', 'readAvatarFile']) {
      expect(src).toContain(needle)
    }
    expect(src).not.toContain('createImageBitmap')
  })

  it('存库尽力而为：图库写入失败不阻断头像已设成功', () => {
    expect(src).toContain('addGalleryItem')
    expect(src).toMatch(/catch \{[^}]*\}/)
  })
})
