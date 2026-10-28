import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('QQ 对端头像 · 去马赛克（Phase 4 §4.1）', () => {
  it('GeneratedAvatar 不含 identicon，渲染淡色首字占位', () => {
    const comp = read('src/components/bot/GeneratedAvatar.vue')
    // 注释里的「取代 identicon 马赛克」不算 —— 钉的是代码引用：不许再 import/渲染 SVG 拼块
    expect(comp).not.toContain('identiconSvg')
    expect(comp).not.toContain('v-html')
    expect(comp).toContain('avatarPlaceholderStyle')
    expect(comp).toContain('placeholderInitial')
  })

  it('bot-chat-decor 不再导出 identiconSvg，prng/GRID 一并退役', () => {
    const decor = read('src/utils/bot-chat-decor.ts')
    expect(decor).not.toContain('identiconSvg')
    expect(decor).not.toContain('prng')
    expect(decor).not.toContain('GRID')
    expect(decor).toContain('avatarPlaceholderStyle')
    expect(decor).toContain('placeholderInitial')
  })

  it('PersonaAvatar 无图时渲染淡色首字占位圈，不再留空', () => {
    const persona = read('src/components/bot/PersonaAvatar.vue')
    expect(persona).toContain('avatar-placeholder')
    expect(persona).toContain('placeholderInitial')
    expect(persona).not.toContain('identiconSvg')
    expect(persona).not.toContain('v-html')
  })

  it('QQ 调用点都带匿名代号 label，占位首字才有意义', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const tags = view.match(/<GeneratedAvatar[^>]*>/g) || []
    expect(tags.length).toBeGreaterThan(0)
    for (const tag of tags) {
      expect(tag).toContain(':label=')
      expect(tag).toContain('peerSeed(')
    }
  })
})
