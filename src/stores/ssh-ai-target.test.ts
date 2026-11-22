/**
 * AI 发送装配的落点归属（与用户终端隔离）。
 *
 * 核心契约：AI 操作的是**它在输入框选中的那条专属会话**，不是用户此刻盯着的终端 tab。
 *   - 选了 SSH 连接 → sshSessionId / 上下文都来自 AI 专属会话；
 *   - 本地 / 工作树 → 仍读用户活动本地会话（既有行为不变）；
 *   - 用户终端页开着远程 tab 但 AI 没选连接 → **不借用**那条远程会话（sshSessionId 为空）。
 * 观测点：runAgent 收到的 sshSessionId，以及落进用户消息的 context。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const lsMap = new Map<string, string>()
const g = globalThis as unknown as Record<string, unknown>
g.localStorage = {
  getItem: (k: string) => lsMap.get(k) ?? null,
  setItem: (k: string, v: string) => { lsMap.set(k, v) },
  removeItem: (k: string) => { lsMap.delete(k) },
}
g.window = {
  setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimeout: (id: unknown) => clearTimeout(id as number),
}

vi.mock('@/api/client', () => ({
  api: {
    listConversations: vi.fn(async () => []),
    getDefaultModel: vi.fn(async () => undefined),
    getModelConfigs: vi.fn(async () => []),
    getMessages: vi.fn(async () => []),
    createConversation: vi.fn(async () => ({ id: 'b1' })),
    uploadChatFile: vi.fn(async () => ({ file_id: 'x', filename: 'x', content: '', truncated: false })),
  },
  agentApi: { grantPermission: vi.fn(async () => ({})), denyCommand: vi.fn(async () => ({})) },
  streamAgentRun: vi.fn(() => () => {}),
}))

const { useSshAiStore } = await import('@/stores/ssh-ai')
const { useAgentStore } = await import('@/stores/agent')
const { useSSHStore } = await import('@/stores/ssh')
const { useExecTargetStore } = await import('@/stores/exec-target')
const { useAppStore } = await import('@/stores/app')

function conn(id: string, name: string) {
  return { id, name, host: '10.0.0.1', port: 22, username: 'root', environment: 'prod', authType: 'password', tags: [], createdAt: 0, updatedAt: 0 }
}

beforeEach(() => {
  setActivePinia(createPinia())
  lsMap.clear()
  useAppStore().backendStatus = 'ready'
})

/** 发送一条消息，返回 runAgent 收到的请求体与落进用户消息的 context。 */
async function send() {
  const store = useSshAiStore()
  store.setSelectedModel('m1')
  const agent = useAgentStore()
  const runAgent = vi.spyOn(agent, 'runAgent').mockImplementation(() => {})
  const ok = await store.sendMessage('看一下磁盘')
  expect(ok).toBe(true)
  expect(runAgent).toHaveBeenCalledTimes(1)
  const req = runAgent.mock.calls[0][0] as Record<string, unknown>
  const ctx = store.conversations[0].messages[0].context as Record<string, unknown> | undefined
  return { req, ctx }
}

describe('AI 发送装配只认专属落点', () => {
  it('选了 SSH 连接：sshSessionId 与上下文都来自 AI 专属会话，不读用户活动终端', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.connections = [conn('c1', '生产库')] as never
    // 用户正停在一条本地终端上：AI 不该把它当成执行落点
    ssh.sessions = [
      { id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' },
      { id: 'ai-1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1', connectionId: 'c1', ai: true },
    ]
    ssh.activeSessionId = 'l1'
    landing.aiSessionId = 'ai-1'

    const { req, ctx } = await send()

    expect(req.sshSessionId).toBe('ai-1')
    expect(ctx?.sessionType).toBe('remote')
    expect(ctx?.sessionId).toBe('ai-1')
    expect(ctx?.connectionName).toBe('生产库')
  })

  it('用户终端页开着远程 tab 但 AI 没选连接：不借用那条远程会话', async () => {
    const ssh = useSSHStore()
    ssh.connections = [conn('c1', '生产库')] as never
    ssh.sessions = [{ id: 'r1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1', connectionId: 'c1' }]
    ssh.activeSessionId = 'r1'

    const { req, ctx } = await send()

    expect(req.sshSessionId).toBeUndefined()
    expect(ctx?.sessionType).not.toBe('remote')
  })

  it('本地落点：仍读用户活动本地会话（既有行为不变）', async () => {
    const ssh = useSSHStore()
    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'

    const { req, ctx } = await send()

    expect(req.sshSessionId).toBe('l1')
    expect(ctx?.sessionType).toBe('local')
    expect(ctx?.sessionId).toBeUndefined()
  })
})