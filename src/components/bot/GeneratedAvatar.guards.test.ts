import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('QQ 对端头像 · 无图即透明（不自造图案）', () => {
  it('GeneratedAvatar 已随调用点清零删除（头像只活在 PersonaAvatar）', () => {
    expect(() => read('src/components/bot/GeneratedAvatar.vue')).toThrow()
    expect(read('src/components/bot/CkunBotView.vue')).not.toContain('GeneratedAvatar')
  })

  it('identicon 与首字占位两套自造图案都已退役', () => {
    const decor = read('src/utils/bot-chat-decor.ts')
    for (const gone of ['identiconSvg', 'prng', 'GRID', 'avatarPlaceholderStyle', 'placeholderInitial']) {
      expect(decor).not.toContain(gone)
    }
  })

  it('PersonaAvatar 无图时只留透明空位，不写首字、不画底', () => {
    const persona = read('src/components/bot/PersonaAvatar.vue')
    expect(persona).toContain('avatar-empty')
    for (const gone of ['placeholderInitial', 'avatarPlaceholderStyle', 'identiconSvg', 'v-html']) {
      expect(persona).not.toContain(gone)
    }
  })

  it('头像调用点只喂 avatar，不再传 seed/label', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const tags = view.match(/<PersonaAvatar[^>]*>/g) || []
    expect(tags.length).toBeGreaterThan(0)
    for (const tag of tags) {
      expect(tag).not.toContain(':seed=')
      expect(tag).not.toContain(':label=')
    }
    // 匿名代号仍以文本呈现，不靠头像区分
    expect(view).toContain('peerLabel(')
  })
})
