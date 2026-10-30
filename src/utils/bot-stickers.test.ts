import { describe, expect, it, vi } from 'vitest'

const baseUrl = vi.hoisted(() => ({ value: 'http://127.0.0.1:7777/api' }))
vi.mock('@/api/base', () => ({
  getBaseUrl: () => {
    if (!baseUrl.value) throw new Error('Backend not ready')
    return baseUrl.value
  },
}))

import { stickerAssetUrl, stickerKey, stripStickerTokens } from './bot-stickers'

describe('表情包标记 stickerKey', () => {
  it('整条是一个标记时取出键（全角/半角冒号都认）', () => {
    expect(stickerKey('[表情:开心]')).toBe('开心')
    expect(stickerKey('  [表情：害羞]  ')).toBe('害羞')
  })

  it('标记混在句子里不算「另发一张图」', () => {
    expect(stickerKey('笑死[表情:开心]')).toBeNull()
    expect(stickerKey('[表情:开心]\n还有一句')).toBeNull()
    expect(stickerKey('')).toBeNull()
  })
})

describe('显示层清理 stripStickerTokens', () => {
  it('去掉行内标记，正文留下', () => {
    expect(stripStickerTokens('笑死[表情:开心]')).toBe('笑死')
    expect(stripStickerTokens('[表情:开心]')).toBe('')
  })

  it('流式吐出的半截标记也藏掉，不让用户看到原文', () => {
    expect(stripStickerTokens('好的\n\n[表')).toBe('好的')
    expect(stripStickerTokens('好的\n\n[表情:开')).toBe('好的')
    expect(stripStickerTokens('好的 [表情]')).toBe('好的 [表情]')
  })
})

describe('表情图片直链 stickerAssetUrl', () => {
  it('/assets 不带 /api 前缀，键按 URL 编码', () => {
    expect(stickerAssetUrl('开心')).toBe('http://127.0.0.1:7777/assets/stickers/%E5%BC%80%E5%BF%83')
  })

  it('后端未就绪时给空串，不向上抛错', () => {
    baseUrl.value = ''
    expect(stickerAssetUrl('开心')).toBe('')
    baseUrl.value = 'http://127.0.0.1:7777/api'
  })
})
