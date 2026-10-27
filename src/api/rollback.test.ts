/**
 * rollbackApi 三方法契约（URL / method / body 形状与非 200 错误路径）。
 *
 * 回滚 API 的键名/路径拼错任何一处，请求照样发出、回滚整条静默失效
 * —— 同族先例见 runbook.test.ts / run-list-query.test.ts。这里钉死三方法的形状。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { rollbackApi } from './rollback'

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

describe('rollbackApi 三方法形状', () => {
  it('getUndoActions → GET /rollback/runs/{id}/undo-actions，无 body', async () => {
    void rollbackApi.getUndoActions('run abc/1')
    const c = await oneCall()
    expect(c.url.endsWith('/api/rollback/runs/run%20abc%2F1/undo-actions')).toBe(true)
    expect(c.init?.method).toBeUndefined()
    expect(c.init?.body).toBeUndefined()
  })

  it('plan → POST /rollback/plan，body 带 source_run_id', async () => {
    void rollbackApi.plan('run-1')
    const c = await oneCall()
    expect(c.url.endsWith('/api/rollback/plan')).toBe(true)
    expect(c.init?.method).toBe('POST')
    expect(JSON.parse(String(c.init?.body))).toEqual({ source_run_id: 'run-1' })
  })

  it('register → POST /rollback/register，body 双 id', async () => {
    void rollbackApi.register('src-1', 'rbk-1')
    const c = await oneCall()
    expect(c.url.endsWith('/api/rollback/register')).toBe(true)
    expect(c.init?.method).toBe('POST')
    expect(JSON.parse(String(c.init?.body))).toEqual({
      source_run_id: 'src-1',
      rollback_run_id: 'rbk-1',
    })
  })

  it('非 200 → 抛错且 detail 透出（ FriendlyError 链路的输入形状）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'run 无 undo 记录: run-404' }),
    }) as unknown as Response))
    calls = []
    await expect(rollbackApi.getUndoActions('run-404')).rejects.toThrow('run 无 undo 记录: run-404')
  })
})
