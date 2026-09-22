/**
 * 权限模式（permission_mode）下发契约测试 —— 双轴版。
 *
 * 背景一（历史「亮的空开关」）：后端的 PlanModeGuard 只读强制挂在
 * `permission_mode` 上，而「计划模式」还带另一个字段 `workflow_mode`
 * （只注入姿态提示词）。前端历史上只发了后者，于是 UI 亮着、约束不存在。
 *
 * 背景二（新增审批轴）：permission_mode 现在是三条独立语义共用一字段 ——
 *   'normal' = 手动审批（危险卡用户逐条点头）
 *   'auto'   = 自动审批（模型代判一次，fail-closed 回弹卡，后端 auto_approve）
 *   'plan'   = 只读强制（写操作在弹卡前直接拒绝，PlanModeGuard）
 * 姿态（plan）优先于审批档（auto）：plan 姿态下审批轴休眠，
 * 任何输入都不许产出「plan 姿态 + auto 权限」这种约束更弱的组合。
 *
 * 断言必须能失败：把 client.ts 那行改成硬编码 'normal'、把 resolveRunModes
 * 两字段拆成不同条件、或让 plan 分支漏掉对 approval 的遮蔽，本文件都要变红。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { streamAgentRun } from './client'
import { resolveRunModes } from '@/utils/run-modes'

interface Captured {
  url: string
  body: Record<string, unknown>
}

let captured: Captured[] = []

/** 最小 Response 替身：只需 ok / body.getReader()，reader 立刻 done（半截流由上层处理） */
function okResponse(): Response {
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () => ({ done: true, value: undefined }),
        cancel: async () => {},
        releaseLock: () => {},
      }),
    },
  } as unknown as Response
}

function runAgent(opts: { workflowMode?: 'goal' | 'plan'; permissionMode?: 'normal' | 'auto' | 'plan' }) {
  streamAgentRun({
    task: 't',
    conversationId: 'c1',
    modelId: 'm1',
    ...opts,
    onEvent: () => {},
    onError: () => {},
    onDone: () => {},
  })
}

/** 等首个请求落到 fetch（ensureApiToken 是 async，至少要让出几个 tick） */
async function firstBody(): Promise<Record<string, unknown>> {
  for (let i = 0; i < 50 && captured.length === 0; i++) {
    await new Promise(r => setTimeout(r, 0))
  }
  expect(captured.length).toBe(1)
  return captured[0]!.body
}

beforeEach(() => {
  captured = []
  vi.stubGlobal('fetch', vi.fn(async (url: unknown, init?: RequestInit) => {
    captured.push({ url: String(url), body: JSON.parse(String(init?.body ?? '{}')) })
    return okResponse()
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('请求体里的 permission_mode', () => {
  it('缺省不表态时下发 normal（默认零行为变化）', async () => {
    runAgent({})
    const body = await firstBody()
    expect(body.permission_mode).toBe('normal')
    expect(body.workflow_mode).toBe('goal')
  })

  it('显式 plan 时真的进了请求体（不是只改了 UI）', async () => {
    runAgent({ workflowMode: 'plan', permissionMode: 'plan' })
    const body = await firstBody()
    expect(body.permission_mode).toBe('plan')
    expect(body.workflow_mode).toBe('plan')
  })

  it('显式 auto 时真的进了请求体（审批轴不是前端摆设）', async () => {
    runAgent({ workflowMode: 'goal', permissionMode: 'auto' })
    const body = await firstBody()
    expect(body.permission_mode).toBe('auto')
    expect(body.workflow_mode).toBe('goal')
  })

  it('字段名必须是后端认的 snake_case —— 泄漏驼峰键会让后端静默当成 normal', async () => {
    runAgent({ permissionMode: 'auto' })
    const body = await firstBody()
    expect('permission_mode' in body).toBe(true)
    expect('permissionMode' in body).toBe(false)
  })
})

describe('姿态 × 审批档 → permission_mode 的唯一口径', () => {
  it('非 plan 姿态：审批档仅认 auto，其余（含脏值）一律手动 normal', () => {
    expect(resolveRunModes('goal', 'auto')).toEqual({ workflowMode: 'goal', permissionMode: 'auto' })
    expect(resolveRunModes('goal', 'normal')).toEqual({ workflowMode: 'goal', permissionMode: 'normal' })
    expect(resolveRunModes(undefined, undefined)).toEqual({ workflowMode: 'goal', permissionMode: 'normal' })
    for (const junk of [undefined, '', 'AUTO', 'aut0', ' plan', 'Always']) {
      expect(resolveRunModes('goal', junk).permissionMode).toBe('normal')
    }
  })

  it('plan 姿态下审批轴休眠：哪怕 approval=auto 也必须是 plan', () => {
    for (const approval of ['auto', 'normal', undefined, 'junk']) {
      expect(resolveRunModes('plan', approval)).toEqual({ workflowMode: 'plan', permissionMode: 'plan' })
    }
  })

  it('任何输入组合都不产出「plan 姿态 + 比 plan 更弱的权限」', () => {
    for (const mode of [undefined, '', 'goal', 'plan', 'GOAL', 'Plan', 'PLAN', 'normal', 'spec', 'auto']) {
      for (const approval of [undefined, 'normal', 'auto', 'Auto', 'plan']) {
        const { workflowMode, permissionMode } = resolveRunModes(mode, approval)
        if (workflowMode === 'plan') {
          expect(permissionMode).toBe('plan')
        } else {
          expect(['normal', 'auto']).toContain(permissionMode)
        }
        expect(['normal', 'auto', 'plan']).toContain(permissionMode)
      }
    }
  })
})
