import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('人格编辑器 · 记忆不进编辑页', () => {
  it('没有「它记得你什么」管理区：记忆由系统自己抽取与召回', () => {
    const editor = read('src/components/bot/PersonaEditor.vue')
    expect(editor).not.toContain('它记得你什么')
    for (const gone of ['listMemories', 'addMemory', 'deleteMemory', 'clearMemories', 'BotMemory', 'memoryBusy', 'memory-list', 'memory-add']) {
      expect(editor, gone).not.toContain(gone)
    }
    // 留下的只有「记忆设定」：那是这个人格对记忆的分寸要求，不是一份可增删的清单
    expect(editor).toContain('记忆设定')
    expect(editor).toContain('memory_directive')
  })

  it('api 层不再留无人消费的记忆封装：编辑页一走，这四个方法就是死代码', () => {
    const api = read('src/api/bot.ts')
    for (const gone of ['listMemories', 'addMemory', 'deleteMemory', 'clearMemories', 'BotMemory', '/memories']) {
      expect(api, gone).not.toContain(gone)
    }
  })

  it('记忆只有一个看的地方：Agent 那个面板，不再开第二个', () => {
    expect(read('src/components/settings/panels/MemoryPanel.vue')).toContain('记忆管理')
    expect(read('src/components/agent/AgentMemoryView.vue')).toContain('listMemories')
  })

  it('聊天界面不暴露记忆控件：记忆是后台事实，不该变成聊天里的功能按钮', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).not.toContain('listMemories')
    expect(view).not.toContain('clearMemories')
  })
})
