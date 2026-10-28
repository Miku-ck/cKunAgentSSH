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

  it('预览即上传入口，格式说明不占位，重置可交给外层', () => {
    // 「PNG / JPG / WebP，自动裁成方形 256px」读了解决不了任何事：能选的文件都能用
    expect(src).not.toContain('自动裁成方形')
    expect(src).toMatch(/class="avatar-preview"[\s\S]*?fileInput\?\.click\(\)/)
    // #fields 插槽让外层把相邻字段并进头像那一栏；clearable=false 给已有整体重置的调用方
    expect(src).toContain('<slot name="fields" />')
    expect(src).toMatch(/v-if="clearable && modelValue"/)
  })
})
