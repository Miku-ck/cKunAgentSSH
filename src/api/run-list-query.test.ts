/**
 * GET /agent/runs 的查询串契约（spec 2026-09-30-retire-ai-side-panel R2）
 *
 * 前端传 `runId`、后端收 `run_id`：中间那层拼接写错任何一个键名，请求照样 200、
 * 照样返回一堆 run，筛选却整条静默失效 —— 后端 db 函数全绿的形状（审计表 count
 * 漏条件那次是同一家族）。这里只钉键名与"缺席即不发"，分页边界由后端测试负责。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { agentAdminApi } from './client'

let urls: string[] = []

beforeEach(() => {
  urls = []
  vi.stubGlobal('fetch', vi.fn(async (url: unknown) => {
    urls.push(String(url))
    return { ok: true, status: 200, json: async () => ({ items: [], total: 0 }) } as unknown as Response
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

async function oneQuery(): Promise<URLSearchParams> {
  for (let i = 0; i < 50 && urls.length === 0; i++) await new Promise(r => setTimeout(r, 0))
  expect(urls).toHaveLength(1)
  return new URL(urls[0]!).searchParams
}

describe('/agent/runs 的查询串', () => {
  it('参数名用后端的形式：run_id / conversation_id / status / limit / offset', async () => {
    void agentAdminApi.listRuns({
      status: 'completed', runId: 'abcdef123456', conversationId: 'c1', limit: 50, offset: 100,
    })
    const qs = await oneQuery()
    expect(qs.get('status')).toBe('completed')
    expect(qs.get('run_id')).toBe('abcdef123456')
    expect(qs.get('conversation_id')).toBe('c1')
    expect(qs.get('limit')).toBe('50')
    expect(qs.get('offset')).toBe('100')
  })

  it('空筛选根本不出现在串上：`run_id=` 会被后端的 if val 当假值，但也别多发明令牌', async () => {
    void agentAdminApi.listRuns({ status: '', runId: '   '.trim(), conversationId: undefined })
    const qs = await oneQuery()
    expect([...qs.keys()]).toEqual([])
  })
})
