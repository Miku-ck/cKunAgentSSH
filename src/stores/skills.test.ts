/**
 * 对话侧技能清单的数据源守卫。
 *
 * 首页 ＋ 菜单读的是后端 /skills?mode=ssh 那份（带设置页的 per-mode 开关），
 * 本地 ~/.agents/skills 扫出来的副本一律 enabled=false，只用于同步入库。
 * 这里钉住「谁会真的出现在菜单里」，避免再退回按全局 enabled 过滤导致
 * 选中后被 agent.py 按 mode 静默丢掉。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSkillStore } from './skills'

const g = globalThis as unknown as Record<string, unknown>

function skill(over: Record<string, unknown> = {}) {
  return {
    id: 's1',
    name: '示例技能',
    slug: 'example',
    description: '',
    category: '开发工具',
    system_prompt: '正文',
    trigger_words: '[]',
    source: 'builtin',
    priority: 0,
    created_at: '',
    updated_at: '',
    enabled: 1,
    mode_enabled: true,
    ...over,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('loadRemoteSkills — 首页菜单的权威清单', () => {
  it('按 ssh 模式拉取，只让 mode_enabled 的技能进菜单', async () => {
    const listSkills = vi.fn().mockResolvedValue([
      skill({ id: 'on', name: '已启用' }),
      skill({ id: 'off', name: '本模式停用', mode_enabled: false }),
    ])
    g.window = { electronAPI: { listSkills } }

    const store = useSkillStore()
    await store.loadRemoteSkills()

    expect(listSkills).toHaveBeenCalledWith('ssh')
    expect(store.enabledSkills.map(s => s.id)).toEqual(['on'])
  })

  it('旧后端不带 mode_enabled 时退回全局 enabled，不把整个清单判空', async () => {
    const { mode_enabled: _unused, ...legacyOn } = skill({ id: 'legacy' })
    void _unused
    g.window = {
      electronAPI: {
        listSkills: vi.fn().mockResolvedValue([
          legacyOn,
          skill({ id: 'disabled', enabled: 0, mode_enabled: undefined }),
        ]),
      },
    }

    const store = useSkillStore()
    await store.loadRemoteSkills()

    expect(store.enabledSkills.map(s => s.id)).toEqual(['legacy'])
  })

  it('拉取失败保留上一次的清单：打开 ＋ 时的补拉才有意义', async () => {
    const listSkills = vi.fn()
      .mockResolvedValueOnce([skill({ id: 'kept' })])
      .mockRejectedValueOnce(new Error('后端未就绪'))
    g.window = { electronAPI: { listSkills } }
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const store = useSkillStore()
    await store.loadRemoteSkills()
    expect(store.enabledSkills.map(s => s.id)).toEqual(['kept'])
    await store.loadRemoteSkills()
    expect(store.enabledSkills.map(s => s.id)).toEqual(['kept'])

    errSpy.mockRestore()
  })
})
