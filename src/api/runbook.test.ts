/**
 * runbookApi 六方法契约（URL / method / body 形状与 limit 夹取）。
 *
 * 剧本 API 的键名/路径拼错任何一处，请求照样发出、筛选/触发整条静默失效
 * —— 同族先例见 run-list-query.test.ts 的注释。这里钉死六方法的形状。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clampRunsLimit, runbookApi } from './runbook'

let calls: { url: string; init: RequestInit | undefined }[] = []

beforeEach(() => {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (url: unknown, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

async function oneCall(): Promise<{ url: string; init: RequestInit | undefined }> {
  for (let i = 0; i < 50 && calls.length === 0; i++) await new Promise(r => setTimeout(r, 0))
  expect(calls).toHaveLength(1)
  return calls[0]!
}

describe('runbookApi 六方法形状', () => {
  it('list → GET /runbooks，无 body', async () => {
    void runbookApi.list()
    const c = await oneCall()
    expect(c.url.endsWith('/api/runbooks')).toBe(true)
    expect(c.init?.method).toBeUndefined()
    expect(c.init?.body).toBeUndefined()
  })

  it('create → POST /runbooks，body 带步骤数组', async () => {
    void runbookApi.create({
      name: 'nginx 恢复',
      steps: [{ command: 'systemctl restart nginx', assertCommand: 'is-active nginx', note: '' }],
    })
    const c = await oneCall()
    expect(c.url.endsWith('/api/runbooks')).toBe(true)
    expect(c.init?.method).toBe('POST')
    const body = JSON.parse(String(c.init?.body))
    expect(body.name).toBe('nginx 恢复')
    expect(body.steps[0].command).toBe('systemctl restart nginx')
    expect(body.steps[0].assertCommand).toBe('is-active nginx')
  })

  it('update → PATCH /runbooks/{id}，只发改过的键', async () => {
    void runbookApi.update('rb1', { enabled: false })
    const c = await oneCall()
    expect(c.url.endsWith('/api/runbooks/rb1')).toBe(true)
    expect(c.init?.method).toBe('PATCH')
    expect(JSON.parse(String(c.init?.body))).toEqual({ enabled: false })
  })

  it('remove → DELETE /runbooks/{id}', async () => {
    void runbookApi.remove('rb2')
    const c = await oneCall()
    expect(c.url.endsWith('/api/runbooks/rb2')).toBe(true)
    expect(c.init?.method).toBe('DELETE')
  })

  it('runNow → POST /runbooks/{id}/run', async () => {
    void runbookApi.runNow('rb3')
    const c = await oneCall()
    expect(c.url.endsWith('/api/runbooks/rb3/run')).toBe(true)
    expect(c.init?.method).toBe('POST')
  })

  it('runs → GET /runbooks/{id}/runs?limit=…，limit 缺省 50', async () => {
    void runbookApi.runs('rb4')
    const c = await oneCall()
    expect(c.url.endsWith('/api/runbooks/rb4/runs?limit=50')).toBe(true)
    expect(c.init?.method).toBeUndefined()
  })
})

describe('limit 夹取（与后端 max(1, min(limit, 200)) 同口径）', () => {
  it('超上限夹到 200，下限夹到 1', () => {
    expect(clampRunsLimit(9999)).toBe(200)
    expect(clampRunsLimit(200)).toBe(200)
    expect(clampRunsLimit(0)).toBe(1)
    expect(clampRunsLimit(-5)).toBe(1)
    expect(clampRunsLimit(3)).toBe(3)
  })

  it('非法/缺省输入落 50', () => {
    expect(clampRunsLimit(undefined)).toBe(50)
    expect(clampRunsLimit(Number.NaN)).toBe(50)
  })

  it('runs() 把夹取后的值放上查询串', async () => {
    void runbookApi.runs('rb5', 9999)
    const c = await oneCall()
    expect(c.url.endsWith('/api/runbooks/rb5/runs?limit=200')).toBe(true)
  })
})
