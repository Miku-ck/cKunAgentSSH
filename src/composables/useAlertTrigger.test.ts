/**
 * useAlertTrigger — 接线契约守卫（docs/p0-ops-agent-enhancements.md §1.5）。
 *
 * ①agentStore.isRunning 时不发起且不重置冷却（恢复后下一 tick 立即触发 = 冷却未被
 *   错误设置的证明）；②建会话参数（mode='ssh'）与 runAgent 参数契约（专用
 *   conversationId / sshSessionId=current.sshId / environment='test' /
 *   permissionMode='normal' / maxCostUsd=设置默认）；③runAgent 抛错 / createConversation
 *   拒绝时静默放弃、in-flight 释放（冷却过期后可再次触发）；④polling=false 间隙样本冻结；
 * ⑤建会话窗口内用户 run 抢先 → 放弃发起（runAgent 会 abort 旧流，不能顶掉用户的 run）；
 * ⑥切会话 / stop（current=null、host 变化）清零计数与冷却。
 *
 * 依赖全部 mock（reactive 形态对齐 pinia store 的 ref 解包），核心逻辑走真实现。
 * 冷却推进用 Date.now spy 控制，不用假定时器（watch 走微任务，混用易碎）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, reactive } from 'vue'
import type { ServerMetrics } from '@/stores/server-monitor-core'
import {
  ALERT_CONFIG_STORAGE_KEY,
  ALERT_SYSTEM_PROMPT,
  ALERT_TASK_MAX_CHARS,
  alertMemoryStorage,
  defaultAlertConfig,
} from '@/utils/alert-trigger-core'

const toastMock = { info: vi.fn(), error: vi.fn(), warning: vi.fn(), success: vi.fn() }
const agentMock = { isRunning: false, runAgent: vi.fn() }
const appMock = { backendStatus: 'ready' as 'ready' | 'starting' | 'error' | 'restarting' }
const monitorMock = reactive({
  current: null as ServerMetrics | null,
  polling: true,
  cpuHistory: [] as number[],
  memHistory: [] as number[],
})
const modelsMock = {
  models: [{ id: 'm-list-0' }, { id: 'm-default' }] as Array<{ id: string }>,
  fetchModels: vi.fn(async () => {}),
}
const settingsMock = { settings: { agent: { maxCostUsd: 3.5, breakerDisabled: false } } }
const apiMock = {
  createConversation: vi.fn(),
  getDefaultModel: vi.fn(),
}

vi.mock('@/stores/server-monitor', () => ({ useServerMonitorStore: () => monitorMock }))
vi.mock('@/stores/agent', () => ({ useAgentStore: () => agentMock }))
vi.mock('@/stores/app', () => ({ useAppStore: () => appMock }))
vi.mock('@/stores/models', () => ({ useModelsStore: () => modelsMock }))
vi.mock('@/stores/settings', () => ({ useSettingsStore: () => settingsMock }))
vi.mock('@/stores/toast', () => ({ useToastStore: () => toastMock }))
vi.mock('@/api/client', () => ({ api: apiMock }))

const { useAlertTrigger } = await import('@/composables/useAlertTrigger')

let mockNow = 1_700_000_000_000
let dateSpy: ReturnType<typeof vi.spyOn>
const stops: Array<() => void> = []

function makeMetrics(over: Partial<ServerMetrics> = {}): ServerMetrics {
  mockNow += 1000
  return {
    ts: mockNow,
    host: 'web-01',
    sshId: 'ssh-1',
    cpu: { used: 96, iowait: 2 },
    load: [5, 4, 3],
    mem: { total: 8000, used: 4000, percent: 50 },
    swap: { total: 0, used: 0 },
    disks: [{ mount: '/', total: 51200, used: 10240, percent: 20 }],
    ports: [],
    gpus: [],
    gpuError: '',
    gpuDriver: '',
    gpuCuda: '',
    ...over,
  }
}

async function pushSample(over: Partial<ServerMetrics> = {}): Promise<void> {
  monitorMock.current = makeMetrics(over)
  await nextTick()
}

/** 冲刷 trigger 的异步链（createConversation → getDefaultModel → runAgent） */
async function flushAsync(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0))
}

describe('useAlertTrigger 接线契约', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    dateSpy = vi.spyOn(Date, 'now').mockImplementation(() => mockNow)
    monitorMock.current = null
    monitorMock.polling = true
    monitorMock.cpuHistory = []
    monitorMock.memHistory = []
    agentMock.isRunning = false
    appMock.backendStatus = 'ready'
    modelsMock.models = [{ id: 'm-list-0' }, { id: 'm-default' }]
    settingsMock.settings.agent = { maxCostUsd: 3.5, breakerDisabled: false }
    apiMock.createConversation.mockResolvedValue({ id: 'conv-al-1' })
    apiMock.getDefaultModel.mockResolvedValue({ id: 'm-default' })
  })

  afterEach(() => {
    stops.splice(0).forEach(stop => stop())
    dateSpy.mockRestore()
    alertMemoryStorage.setItem(ALERT_CONFIG_STORAGE_KEY, '') // 复位规则存储（空值 → 预置配置）
  })

  it('连续 6 次越限：建专用会话（mode=ssh）并按契约发起 runAgent + toast', async () => {
    stops.push(useAlertTrigger())
    for (let i = 0; i < 6; i++) await pushSample()
    await flushAsync()

    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
    expect(apiMock.createConversation).toHaveBeenCalledWith('[告警诊断] web-01 cpu_high', 'ssh')
    expect(agentMock.runAgent).toHaveBeenCalledTimes(1)
    const arg = agentMock.runAgent.mock.calls[0]![0] as Record<string, unknown>
    expect(arg.conversationId).toBe('conv-al-1')
    expect(arg.modelId).toBe('m-default')
    expect(arg.systemPrompt).toBe(ALERT_SYSTEM_PROMPT)
    expect(arg.sshSessionId).toBe('ssh-1')
    expect(arg.environment).toBe('test')
    expect(arg.permissionMode).toBe('normal')
    expect(arg.maxCostUsd).toBe(3.5)
    expect(typeof arg.task).toBe('string')
    expect(arg.task as string).toContain('web-01')
    expect(arg.task as string).toContain('CPU 使用率 > 90%')
    expect((arg.task as string).length).toBeLessThanOrEqual(ALERT_TASK_MAX_CHARS)
    expect(toastMock.info).toHaveBeenCalledTimes(1)
    expect(toastMock.info.mock.calls[0]![0] as string).toContain('已在任务台自动发起诊断')
  })

  it('未达 N 不触发；夹入未越限样本重置连续链', async () => {
    stops.push(useAlertTrigger())
    for (let i = 0; i < 3; i++) await pushSample()
    await pushSample({ cpu: { used: 20, iowait: 2 } })   // 归零
    for (let i = 0; i < 3; i++) await pushSample()       // count=3
    await pushSample({ cpu: { used: 20, iowait: 2 } })   // 归零
    for (let i = 0; i < 5; i++) await pushSample()       // count=5，仍未达 6
    expect(apiMock.createConversation).not.toHaveBeenCalled()
    await pushSample()                                    // 第 6 个连续样本 → 触发
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
    expect(agentMock.runAgent).toHaveBeenCalledTimes(1)
  })

  it('冷却期内不重复触发（默认 30 分钟）', async () => {
    stops.push(useAlertTrigger())
    for (let i = 0; i < 6; i++) await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
    for (let i = 0; i < 12; i++) await pushSample()      // 冷却内持续越限
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
    expect(agentMock.runAgent).toHaveBeenCalledTimes(1)
  })

  it('isRunning 时不发起且不重置冷却：恢复后下一 tick 立即触发', async () => {
    stops.push(useAlertTrigger())
    agentMock.isRunning = true
    for (let i = 0; i < 8; i++) await pushSample()       // 计数累积越过 N 但不发起
    await flushAsync()
    expect(apiMock.createConversation).not.toHaveBeenCalled()
    agentMock.isRunning = false
    await pushSample()                                    // 若错误设置了冷却，这里不会再触发
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
    expect(agentMock.runAgent).toHaveBeenCalledTimes(1)
  })

  it('后端未就绪不发起；就绪后下一 tick 触发', async () => {
    stops.push(useAlertTrigger())
    appMock.backendStatus = 'starting'
    for (let i = 0; i < 8; i++) await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).not.toHaveBeenCalled()
    appMock.backendStatus = 'ready'
    await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
  })

  it('polling=false 间隙样本冻结：计数以最后一次成功样本为准', async () => {
    stops.push(useAlertTrigger())
    for (let i = 0; i < 5; i++) await pushSample()       // count=5
    monitorMock.polling = false
    for (let i = 0; i < 5; i++) await pushSample()       // 重试间隙：冻结
    await flushAsync()
    expect(apiMock.createConversation).not.toHaveBeenCalled()
    monitorMock.polling = true
    await pushSample()                                    // count=6 → 触发
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
  })

  it('runAgent 同步抛错：静默放弃（无 toast），in-flight 释放、冷却过期后可再次触发', async () => {
    stops.push(useAlertTrigger())
    agentMock.runAgent.mockImplementation(() => { throw new Error('boom') })
    for (let i = 0; i < 6; i++) await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
    expect(toastMock.info).not.toHaveBeenCalled()        // 静默：不提示、不重试
    mockNow += 31 * 60_000                                // 跳过冷却（以触发时刻起算，失败照常计时）
    agentMock.runAgent.mockImplementation(() => {})
    for (let i = 0; i < 6; i++) await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(2)
    expect(agentMock.runAgent).toHaveBeenCalledTimes(2)
    expect(toastMock.info).toHaveBeenCalledTimes(1)
  })

  it('createConversation 拒绝（如后端 5xx）：静默放弃，冷却过期后可再次触发', async () => {
    stops.push(useAlertTrigger())
    apiMock.createConversation.mockRejectedValue(new Error('503'))
    for (let i = 0; i < 6; i++) await pushSample()
    await flushAsync()
    expect(agentMock.runAgent).not.toHaveBeenCalled()
    expect(toastMock.error).not.toHaveBeenCalled()
    mockNow += 31 * 60_000
    apiMock.createConversation.mockResolvedValue({ id: 'conv-al-2' })
    for (let i = 0; i < 6; i++) await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(2)
    expect(agentMock.runAgent).toHaveBeenCalledTimes(1)
    expect(agentMock.runAgent.mock.calls[0]![0] as Record<string, unknown>).toMatchObject({ conversationId: 'conv-al-2' })
  })

  it('建会话窗口内用户 run 抢先：放弃发起，不顶掉用户的流', async () => {
    stops.push(useAlertTrigger())
    let resolveConv!: (v: { id: string }) => void
    apiMock.createConversation.mockImplementation(() => new Promise<{ id: string }>(res => { resolveConv = res }))
    for (let i = 0; i < 6; i++) await pushSample()       // 触发已开始，建会话挂起
    agentMock.isRunning = true                            // 用户此刻发起了自己的 run
    resolveConv({ id: 'conv-late' })
    await flushAsync()
    expect(agentMock.runAgent).not.toHaveBeenCalled()    // 绝不 abort 用户的流
    expect(toastMock.info).not.toHaveBeenCalled()
  })

  it('每主机开关关闭不触发；其他主机不受影响', async () => {
    const cfg = defaultAlertConfig()
    cfg.hostOverrides['web-01'] = false
    alertMemoryStorage.setItem(ALERT_CONFIG_STORAGE_KEY, JSON.stringify(cfg))
    stops.push(useAlertTrigger())
    for (let i = 0; i < 8; i++) await pushSample()       // web-01 已关闭
    await flushAsync()
    expect(apiMock.createConversation).not.toHaveBeenCalled()
    for (let i = 0; i < 6; i++) await pushSample({ host: 'db-01', sshId: 'ssh-2' })
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
    expect(apiMock.createConversation).toHaveBeenCalledWith('[告警诊断] db-01 cpu_high', 'ssh')
  })

  it('切会话（host 变化）清零计数：切回原主机需重新累积 N 次', async () => {
    stops.push(useAlertTrigger())
    for (let i = 0; i < 5; i++) await pushSample()                       // web-01 count=5
    await pushSample({ host: 'db-01', sshId: 'ssh-2' })                  // 切主机 → 清零
    for (let i = 0; i < 5; i++) await pushSample()                       // 切回 web-01：count 0→5
    await flushAsync()
    expect(apiMock.createConversation).not.toHaveBeenCalled()            // 若未清零这里早已触发
    await pushSample()                                                    // count=6 → 触发
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
  })

  it('stop（current=null，会话全关）清零计数与冷却', async () => {
    stops.push(useAlertTrigger())
    for (let i = 0; i < 5; i++) await pushSample()
    monitorMock.current = null
    await nextTick()                                                      // stop() → current=null → 清零
    for (let i = 0; i < 5; i++) await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).not.toHaveBeenCalled()
    await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
  })

  it('同 tick 双规则命中：只发起一条（不顶流），另一条下一 tick 补上且未落冷却', async () => {
    const cfg = defaultAlertConfig()
    cfg.rules = [
      { id: 'a', metric: 'cpu', op: '>', threshold: 90, consecutiveN: 2, enabled: true, diskScope: 'any' },
      { id: 'b', metric: 'mem', op: '>', threshold: 90, consecutiveN: 2, enabled: true, diskScope: 'any' },
    ]
    alertMemoryStorage.setItem(ALERT_CONFIG_STORAGE_KEY, JSON.stringify(cfg))
    stops.push(useAlertTrigger())
    const both = { mem: { total: 8000, used: 7600, percent: 95 } }  // cpu 与 mem 同时越限
    await pushSample(both)                                          // 双规则 count=1
    await pushSample(both)                                          // 双规则 count=2 → 双候选
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)     // 单 tick 单发起
    expect(apiMock.createConversation).toHaveBeenCalledWith('[告警诊断] web-01 a', 'ssh')
    expect(agentMock.runAgent).toHaveBeenCalledTimes(1)
    await pushSample(both)                                          // b 未落冷却 → 下一 tick 补上
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(2)
    expect(apiMock.createConversation).toHaveBeenCalledWith('[告警诊断] web-01 b', 'ssh')
  })

  it('enabled:false 的规则不触发（手改 localStorage 禁用必须生效）', async () => {
    const cfg = defaultAlertConfig()
    cfg.rules = [
      { id: 'a', metric: 'cpu', op: '>', threshold: 90, consecutiveN: 2, enabled: true, diskScope: 'any' },
      { id: 'b', metric: 'mem', op: '>', threshold: 90, consecutiveN: 2, enabled: false, diskScope: 'any' },
    ]
    alertMemoryStorage.setItem(ALERT_CONFIG_STORAGE_KEY, JSON.stringify(cfg))
    stops.push(useAlertTrigger())
    for (let i = 0; i < 4; i++) await pushSample({ mem: { total: 8000, used: 7600, percent: 95 } })
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)     // 只有 a 触发
    expect(apiMock.createConversation).toHaveBeenCalledWith('[告警诊断] web-01 a', 'ssh')
  })

  it('getDefaultModel 失败回落模型列表首个；列表为空则静默放弃', async () => {
    stops.push(useAlertTrigger())
    modelsMock.models = [{ id: 'm-list-0' }]
    apiMock.getDefaultModel.mockRejectedValue(new Error('x'))
    for (let i = 0; i < 6; i++) await pushSample()
    await flushAsync()
    expect(agentMock.runAgent).toHaveBeenCalledTimes(1)
    expect((agentMock.runAgent.mock.calls[0]![0] as Record<string, unknown>).modelId).toBe('m-list-0')

    // 无可用模型：静默放弃（冷却已按触发时刻计时）
    agentMock.runAgent.mockClear()
    modelsMock.models = []
    mockNow += 31 * 60_000
    for (let i = 0; i < 6; i++) await pushSample()
    await flushAsync()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(2)
    expect(agentMock.runAgent).not.toHaveBeenCalled()
  })
})
