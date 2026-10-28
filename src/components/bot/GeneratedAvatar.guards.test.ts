import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('QQ 对端头像 · 去马赛克（Phase 4 §4.1）', () => {
  it('GeneratedAvatar 已随调用点清零删除（占位逻辑只活在 PersonaAvatar）', () => {
    expect(() => read('src/components/bot/GeneratedAvatar.vue')).toThrow()
    expect(read('src/components/bot/CkunBotView.vue')).not.toContain('GeneratedAvatar')
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

  it('QQ 调用点由 PersonaAvatar 两态承接，匿名代号 label 仍在', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const tags = view.match(/<PersonaAvatar[^>]*>/g) || []
    expect(tags.length).toBeGreaterThan(0)
    expect(view).toContain('peerSeed(')
    expect(view).toContain('peerLabel(')
  })
})
