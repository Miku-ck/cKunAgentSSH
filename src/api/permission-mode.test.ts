/**
 * 权限模式（permission_mode）下发契约测试 —— 审批轴版（M1 结构化重构）。
 *
 * 背景一（历史「亮的空开关」）：后端的 PlanModeGuard 只读强制挂在
 * `permission_mode` 上，而「计划模式」还带另一个字段 `workflow_mode`
 * （只注入姿态提示词）。前端历史上只发了后者，于是 UI 亮着、约束不存在。
 * M1 后姿态字段整个退役：plan 只读档改由服务端推导（会话存在待批准 /
 * 已批准 / 执行中的结构化 plan 时强制），前端请求体里不得再出现它。
 *
 * 背景二（审批轴）：permission_mode 承载三种语义 ——
 *   'normal' = 手动审批（危险卡用户逐条点头）
 *   'auto'   = 自动审批（模型代判一次，fail-closed 回弹卡，后端 auto_approve）
 *   'plan'   = 只读强制（写操作在弹卡前直接拒绝，PlanModeGuard；服务端推导，非前端下发）
 *
 * 断言必须能失败：把 client.ts 那行改成硬编码 'normal'、或让 auto 分支
 * 漏掉对 approval 的判定，本文件都要变红。
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

function runAgent(opts: { permissionMode?: 'normal' | 'auto' | 'plan' }) {
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
    // 旧姿态字段整个缺席（M1 结构化重构退役）：字段复活即旧双通路回归。
    expect('workflow_mode' in body).toBe(false)
  })

  it('显式 auto 时真的进了请求体（审批轴不是前端摆设）', async () => {
    runAgent({ permissionMode: 'auto' })
    const body = await firstBody()
    expect(body.permission_mode).toBe('auto')
    expect('workflow_mode' in body).toBe(false)
  })

  it('字段名必须是后端认的 snake_case —— 泄漏驼峰键会让后端静默当成 normal', async () => {
    runAgent({ permissionMode: 'auto' })
    const body = await firstBody()
    expect('permission_mode' in body).toBe(true)
    expect('permissionMode' in body).toBe(false)
  })
})

describe('审批档 → permission_mode 的唯一口径', () => {
  it('审批档仅认 auto，其余（含脏值）一律手动 normal', () => {
    expect(resolveRunModes(undefined, 'auto')).toEqual({ permissionMode: 'auto' })
    expect(resolveRunModes(undefined, 'normal')).toEqual({ permissionMode: 'normal' })
    // 未表态 → normal（下发时恒有 permission_mode 键，后端不猜）
    expect(resolveRunModes(undefined, undefined)).toEqual({ permissionMode: 'normal' })
    for (const junk of [undefined, '', 'AUTO', 'aut0', ' plan', 'Always']) {
      expect(resolveRunModes(undefined, junk).permissionMode).toBe('normal')
    }
  })

  it('agentMode 旧参数已无效：传什么都不影响档位（姿态退役的防回归钉）', () => {
    // 旧调用方若还传姿态，绝不能再让 'plan' 从前端产出 —— plan 只读档
    // 是服务端推导面，前端产出即绕过批准门。
    for (const staleMode of [undefined, '', 'goal', 'plan', 'PLAN', 'junk']) {
      expect(resolveRunModes(staleMode, 'auto').permissionMode).toBe('auto')
      expect(resolveRunModes(staleMode, 'normal').permissionMode).toBe('normal')
      expect(resolveRunModes(staleMode, undefined).permissionMode).toBe('normal')
    }
  })

  it('任何输入组合都只产出 normal / auto，绝不产出 plan', () => {
    for (const mode of [undefined, '', 'goal', 'plan', 'GOAL', 'Plan', 'PLAN', 'normal', 'spec', 'auto']) {
      for (const approval of [undefined, 'normal', 'auto', 'Auto', 'plan']) {
        const { permissionMode } = resolveRunModes(mode, approval)
        expect(['normal', 'auto']).toContain(permissionMode)
      }
    }
  })
})
