import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('人格编辑器 · 长期记忆区', () => {
  it('编辑器给出「它记得你什么」这一区', () => {
    const editor = read('src/components/bot/PersonaEditor.vue')
    expect(editor).toContain('它记得你什么')
    for (const needle of ['listMemories', 'addMemory', 'deleteMemory', 'clearMemories']) {
      expect(editor).toContain(needle)
    }
  })

  it('记忆区只对已有人格开放：新人格没有记忆可看', () => {
    const editor = read('src/components/bot/PersonaEditor.vue')
    expect(editor).toContain('v-if="persona"')
  })

  it('API 层有四个记忆端点', () => {
    const api = read('src/api/bot.ts')
    expect(api).toContain('listMemories')
    expect(api).toContain('addMemory')
    expect(api).toContain('deleteMemory')
    expect(api).toContain('clearMemories')
    expect(api).toContain('/memories')
  })

  it('聊天界面不暴露记忆控件：记忆是后台事实，不该变成聊天里的功能按钮', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).not.toContain('listMemories')
    expect(view).not.toContain('clearMemories')
  })
})
