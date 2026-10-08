/**
 * S9-18 回归：从 localStorage 复原会话时必须丢掉 `temp-agent-` 占位气泡。
 *
 * 占位消息只在「那一次 run 还活着」期间有意义 —— 它的 id 由 `ssh-ai` 在发起运行时
 * 生成（`temp-agent-…`），终态 watch 负责把它换成真结果或移除。页面重载后那场 run
 * 必然已经没了，于是复原回来的占位**永远等不到回填**：界面上多一只永久空白的助手
 * 气泡，而且它还占着消息计数（「+」新建/去重逻辑都按消息数判断）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useChatPersistence } from './useChatPersistence'

const KEY = 'ckun-test-chat-conversations'

/** node 环境没有 localStorage（vitest 配置 environment:'node'），自己搭一个 */
function stubStorage(initial: Record<string, string> = {}) {
  const map = new Map<string, string>(Object.entries(initial))
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => { map.set(k, v) },
    removeItem: (k: string) => { map.delete(k) },
    clear: () => map.clear(),
  })
  return map
}

function boot() {
  return useChatPersistence({
    config: { storageKey: KEY, modelStorageKey: KEY + '.model' } as never,
    getDefaultModelId: async () => 'm1',
  })
}

const msg = (id: string, role: string, content = 'x') => ({ id, role, content })

beforeEach(() => {
  setActivePinia(createPinia())
  vi.unstubAllGlobals()
})

describe('loadConversations 的占位清扫', () => {
  it('重载后不再留下永久空白的助手气泡', () => {
    stubStorage({
      [KEY]: JSON.stringify([{
        id: 'c1', title: 'T', updated_at: 1,
        messages: [msg('u1', 'user'), msg('temp-agent-abc', 'assistant'), msg('a2', 'assistant', '真结果')],
      }]),
    })
    const { conversations } = boot()
    const ids = conversations.value[0].messages.map((m: any) => m.id)
    expect(ids).toEqual(['u1', 'a2'])
  })

  it('只按 role+前缀丢助手占位，不误伤同名前缀的用户消息', () => {
    stubStorage({
      [KEY]: JSON.stringify([{
        id: 'c1', title: 'T', updated_at: 1,
        messages: [msg('temp-agent-keep', 'user'), msg('temp-agent-drop', 'assistant')],
      }]),
    })
    const { conversations } = boot()
    expect(conversations.value[0].messages.map((m: any) => m.id)).toEqual(['temp-agent-keep'])
  })

  it('会话本身不因为「只剩占位」而被删掉', () => {
    stubStorage({
      [KEY]: JSON.stringify([
        { id: 'c1', title: '空壳', updated_at: 1, messages: [msg('temp-agent-z', 'assistant')] },
        { id: 'c2', title: '正常', updated_at: 2, messages: [msg('u2', 'user')] },
      ]),
    })
    const { conversations } = boot()
    expect(conversations.value.map((c: any) => c.id)).toEqual(['c1', 'c2'])
    expect(conversations.value[0].messages).toEqual([])
  })

  it('既有的坏数据降级路径不受影响（非数组 ⇒ 空列表）', () => {
    stubStorage({ [KEY]: JSON.stringify({ not: 'an array' }) })
    const { conversations } = boot()
    expect(conversations.value).toEqual([])
  })
})
