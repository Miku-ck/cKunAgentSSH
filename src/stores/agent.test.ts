/**
 * agent store SSE 事件回填测试
 *
 * 覆盖 _handleEvent 的两处历史缺陷（本轮修复，防回归）：
 * - tool_call_result 用 `!t.result` 判"未回填"：shell_exec 执行无输出命令时
 *   result 就是空串，空串条目被当成未回填 → 后来的结果盖到它头上（张冠李戴）。
 * - clarification_done 的 800ms 延时清理无条件置 null：期间若已开新 run 并弹出
 *   新向导，新向导会被一起抹掉，用户再也答不了题。
 * 另验证 tool_call_id 优先配对（同名多调用时唯一可靠的归属依据）。
 *
 * 只驱动事件入口（streamAgentRun 被 mock），不触碰网络与真实终端。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

interface RunHandlers {
  onEvent: (e: unknown) => void
  onError: (e: string) => void
  onDone: () => void
}

const runs: RunHandlers[] = []

vi.mock('@/api/client', () => ({
  streamAgentRun: (opts: RunHandlers) => {
    runs.push(opts)
    return () => {}
  },
  agentApi: {
    denyCommand: vi.fn(async () => ({ ok: true })),
    grantPermission: vi.fn(async () => ({ ok: true })),
    submitClarification: vi.fn(async () => ({ run_id: '', status: 'ok', steps: 0 })),
    skipClarification: vi.fn(async () => ({ run_id: '', status: 'ok' })),
  },
  postToolResult: vi.fn(async () => ({})),
}))

vi.mock('@/stores/ssh', () => ({
  useSSHStore: () => ({ sessions: [], currentSessionId: '', activeSession: null }),
}))

const { useAgentStore } = await import('@/stores/agent')
const { agentApi } = await import('@/api/client')

/** 只暴露测试需要的 mock 面（denyCommand 在工厂里已是 vi.fn） */
const denyMock = agentApi.denyCommand as unknown as ReturnType<typeof vi.fn>

/** 起一次 run，返回该 run 的事件注入器（句柄即时捕获，不受后续 run 影响） */
function startRun() {
  const store = useAgentStore()
  store.runAgent({ task: 't', conversationId: 'c1', modelId: 'm1' })
  const handlers = runs[runs.length - 1]
  expect(handlers).toBeTruthy()
  return { store, push: (e: unknown) => handlers.onEvent(e) }
}

describe('tool_call_result 回填匹配', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  it('已回填的空串条目不会被后续结果改写', () => {
    const { store, push } = startRun()
    push({ type: 'state', state: 'acting', iteration: 0, run_id: 'r1' })
    push({ type: 'tool_call_start', tool: 'shell_exec', args: { command: 'touch /tmp/a' }, iteration: 0, tool_call_id: 'c1' })
    // 无输出命令：result 就是空串 —— 曾经被当成"未回填"
    push({ type: 'tool_call_result', tool: 'shell_exec', result: '', success: true, iteration: 0, tool_call_id: 'c1' })
    // 无配对条目的游离结果（如同迭代被拦截器补发的结果）：只能丢弃，不能盖到 c1 上
    push({ type: 'tool_call_result', tool: 'shell_exec', result: 'stray', success: false, iteration: 0 })

    expect(store.toolCalls).toHaveLength(1)
    expect(store.toolCalls[0].result).toBe('')
    expect(store.toolCalls[0].success).toBe(true)
  })

  it('事件缺 result 字段时也记为已回填（空串），不招来后来的结果', () => {
    const { store, push } = startRun()
    push({ type: 'state', state: 'acting', iteration: 0, run_id: 'r3' })
    push({ type: 'tool_call_start', tool: 'shell_exec', args: {}, iteration: 0, tool_call_id: 'c1' })
    push({ type: 'tool_call_result', tool: 'shell_exec', success: true, iteration: 0, tool_call_id: 'c1' })
    expect(store.toolCalls[0].result).toBe('')

    push({ type: 'tool_call_result', tool: 'shell_exec', result: 'stray', success: false, iteration: 0 })
    expect(store.toolCalls[0].result).toBe('')
  })

  it('优先按 tool_call_id 配对（同名多调用的归属契约）', () => {
    const { store, push } = startRun()
    push({ type: 'state', state: 'acting', iteration: 0, run_id: 'r2' })
    push({ type: 'tool_call_start', tool: 'file_write', args: { path: '/a' }, iteration: 0, tool_call_id: 'call-a' })
    push({ type: 'tool_call_start', tool: 'file_write', args: { path: '/b' }, iteration: 0, tool_call_id: 'call-b' })
    // 结果到达顺序与 start 顺序相反：只能靠 id 对齐，不能依赖名称 + 反向搜索的运气
    push({ type: 'tool_call_result', tool: 'file_write', result: 'ok-b', success: true, tool_call_id: 'call-b' })
    push({ type: 'tool_call_result', tool: 'file_write', result: 'ok-a', success: true, tool_call_id: 'call-a' })

    expect(store.toolCalls.map(t => [t.args.path, t.result])).toEqual([
      ['/a', 'ok-a'],
      ['/b', 'ok-b'],
    ])
  })
})

describe('clarification_done 延时清理', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  function wizard(runId: string) {
    return {
      type: 'clarification_needed',
      run_id: runId,
      tool: 'shell_exec',
      tool_call_id: `tc-${runId}`,
      args: {},
      reason: '命令含通配符',
      question: '确认目标文件？',
      options: [{ id: 'a', label: 'A' }],
      current_step: 0,
      total_steps: 1,
    }
  }

  it('旧 run 的延时清理不会抹掉新 run 的向导', async () => {
    const { store, push } = startRun()
    push(wizard('r-first'))
    expect(store.clarificationWizard?.runId).toBe('r-first')

    push({ type: 'clarification_done', run_id: 'r-first' })
    expect(store.clarificationWizard?.submitted).toBe(true)

    // done 的 800ms 关闭定时器还在跑时，用户已经开新 run 并再次弹卡
    const second = startRun()
    second.push(wizard('r-second'))
    vi.advanceTimersByTime(1000)
    await nextTick()

    expect(store.clarificationWizard?.runId).toBe('r-second')
  })

  it('没有新向导时 done 正常关闭', () => {
    const { store, push } = startRun()
    push(wizard('r1'))
    push({ type: 'clarification_done', run_id: 'r1' })
    vi.advanceTimersByTime(900)
    expect(store.clarificationWizard).toBeNull()
  })

  it('run 收尾（done）时未作答的向导一起关掉', () => {
    const { store, push } = startRun()
    push(wizard('r1'))
    expect(store.clarificationWizard?.submitted).toBe(false)
    // 用户离席期间模型放弃重试、迭代数耗尽 → 流以 done 收尾。
    // error 分支早就清了向导，done 分支漏清：卡片继续挂着，答完 POST 到
    // 一个后端不再等待的 run_id，clarification_done 永不回来。
    push({ type: 'done', state: 'max_reached', iterations: 3, run_id: 'r1' })
    expect(store.clarificationWizard).toBeNull()
  })

  it('用户中止（abort）时未作答的向导一起关掉', () => {
    const { store, push } = startRun()
    push(wizard('r1'))
    expect(store.clarificationWizard?.submitted).toBe(false)
    // 同 done 分支的坑：中止后 run 已终止，向导继续挂着就是让用户答一张没人收
    // 的题——答完 POST 到已中止的 run_id 永远等不到 clarification_done。
    store.abort()
    expect(store.clarificationWizard).toBeNull()
  })
})

describe('权限卡片决策在途互斥', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
    denyMock.mockReset()
    denyMock.mockResolvedValue({ ok: true })
  })

  /** 弹一张命令绑定的授权卡片，返回卡片对象 */
  function addCard(store: ReturnType<typeof useAgentStore>, push: (e: unknown) => void) {
    push({
      type: 'permission_needed',
      tool: 'shell_exec',
      command: 'rm -rf /tmp/x',
      run_id: 'r1',
      risk: 'dangerous',
    })
    const req = store.permissionRequests[0]
    expect(req).toBeTruthy()
    return req
  }

  it('拒绝请求在途期间卡片置为 settling', async () => {
    const { store, push } = startRun()
    const req = addCard(store, push)
    let release: () => void = () => {}
    denyMock.mockImplementation(
      () => new Promise(r => { release = () => r({ ok: true }) }),
    )

    const pending = store.denyPermission(req.id)
    expect(req.settling).toBe(true)

    release()
    expect(await pending).toBe(true)
    expect(store.permissionRequests).toHaveLength(0)
  })

  it('已有决策在途时拒绝调用被忽略', async () => {
    const { store, push } = startRun()
    const req = addCard(store, push)
    req.settling = true   // 授权请求已发出、尚未返回

    expect(await store.denyPermission(req.id)).toBe(false)
    expect(denyMock).not.toHaveBeenCalled()
  })

  it('拒绝失败后 settling 复位，卡片保留可重试', async () => {
    const { store, push } = startRun()
    const req = addCard(store, push)
    denyMock.mockRejectedValueOnce(new Error('network down'))

    expect(await store.denyPermission(req.id)).toBe(false)
    expect(req.settling).toBe(false)
    expect(store.permissionRequests).toHaveLength(1)

    expect(await store.denyPermission(req.id)).toBe(true)
    expect(denyMock).toHaveBeenCalledTimes(2)
  })

  it('拒绝登记的命令与卡片授权命令同源', async () => {
    const { store, push } = startRun()
    const req = addCard(store, push)
    await store.denyPermission(req.id)
    expect(denyMock).toHaveBeenCalledWith('shell_exec', 'rm -rf /tmp/x', 'r1')
  })
})

describe('新 run 清空上一轮可见终端交付', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  it('上一轮的交付徽章不会串进新一轮面板', () => {
    const { store, push } = startRun()
    push({
      type: 'tool_deliver',
      tool_id: 'td-1',
      command: 'rm -rf /tmp/x',
      session_id: 's1',
      requires_confirm: true,
      run_id: 'r1',
    })
    expect(store.deliveries).toHaveLength(1)

    startRun()
    expect(store.deliveries).toHaveLength(0)
  })
})

describe('运行结束时的快照读数', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  it('中止的运行固化为 failed，而不是「已完成」', () => {
    const { store, push } = startRun()
    push({ type: 'tool_call_start', tool: 'shell_exec', args: {}, iteration: 0, tool_call_id: 'c1' })
    store.abort()
    // 原实现只认 status==='failed'，中止后固化的轨迹卡片写「已完成」，
    // 而消息正文是「(已中止)」——同一条消息自相矛盾
    expect(store.snapshot().status).toBe('failed')
  })

  it('未回填的调用在快照里保持双 undefined（判据留给固化侧收敛）', () => {
    const { store, push } = startRun()
    push({ type: 'tool_call_start', tool: 'shell_exec', args: {}, iteration: 0, tool_call_id: 'c1' })
    const tc = store.snapshot().toolCalls[0]
    // finalizeAgentMessage 按「result/success 都缺失」识别未回写；这里若提前落成
    // 空串或 false，运行中的实时轨迹就会把在途调用显示成失败
    expect(tc.result).toBeUndefined()
    expect(tc.success).toBeUndefined()
  })
})

describe('授权卡片去重只认未处理的卡片', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  const CARD = {
    type: 'permission_needed',
    tool: 'shell_exec',
    command: 'systemctl restart nginx',
    run_id: 'r1',
    risk: 'dangerous',
  }

  it('同一请求在处理中重发时不产生第二张卡（去重仍然生效）', () => {
    const { store, push } = startRun()
    push(CARD)
    push(CARD)
    expect(store.permissionRequests).toHaveLength(1)
  })

  it('已授权后同一命令再次请求会出新卡片 —— 后端执行成功即撤下等待标记', () => {
    const { store, push } = startRun()
    push(CARD)
    store.resolvePermission(store.permissionRequests[0].id, true)

    push(CARD)
    expect(store.permissionRequests).toHaveLength(2)
    const latest = store.permissionRequests[1]
    expect(latest.resolved).toBeFalsy()
    // 老卡片必须留在列表里（面板靠它显示「已授权」），但它不再吞掉新请求
    expect(store.permissionRequests[0].granted).toBe(true)
  })

  it('审批超时后保留在队列打 expired 标，重发复用同一张卡（不再建新卡）', () => {
    const { store, push } = startRun()
    push(CARD)
    push({ type: 'permission_expired', tool: 'shell_exec', command: 'systemctl restart nginx' })
    // 裁决A：到期不删卡、不置 resolved，仅打 expired 标——卡片留在可操作队列，
    // 后端签名仍在等待集，用户点「允许」即可恢复执行
    expect(store.permissionRequests[0].expired).toBe(true)
    expect(store.permissionRequests[0].resolved).toBeFalsy()
    expect(store.pendingPermissions).toHaveLength(1)
    // 超时提示走面板角标，不再往 message 里追加
    expect(store.permissionRequests[0].message).not.toContain('审批超时')

    // 后端到期仍将签名保留在等待集 → 同一命令重发复用该卡，不再重复建卡
    push(CARD)
    expect(store.permissionRequests).toHaveLength(1)

    // expired 卡仍可结算：授权后出队
    store.resolvePermission(store.permissionRequests[0].id, true)
    expect(store.pendingPermissions).toHaveLength(0)
  })

  it('不同命令各自一张卡（批了 A 不放行 B）', () => {
    const { store, push } = startRun()
    push(CARD)
    push({ ...CARD, command: 'systemctl restart sshd' })
    expect(store.permissionRequests).toHaveLength(2)
  })

  it('后果解释只回填到未处理的那张卡', () => {
    const { store, push } = startRun()
    push(CARD)
    store.resolvePermission(store.permissionRequests[0].id, true)
    push(CARD)
    push({
      type: 'permission_explain',
      tool: 'shell_exec',
      command: 'systemctl restart nginx',
      explanation: '重启 nginx 进程',
    })
    expect(store.permissionRequests[0].explain).toBeUndefined()
    expect(store.permissionRequests[1].explain).toBe('重启 nginx 进程')
  })
})

// ── 审批结算下沉到 store（spec 2026-09-29-home-command-panel · 接缝 S3）──
// 授权/拒绝原先写在 AgentRunPanel.vue:77-102 里，首页队列要复用就得整段抄一份：
// 而那段里每一条都是踩出来的口径（命令取 payload 原文、settling 守卫、失败留卡
// 可重试）。抄一份就是第二条会各自漂移的通路，故提进 store 供两处调用。
describe('审批结算 approvePermission', () => {
  const grantMock = agentApi.grantPermission as unknown as ReturnType<typeof vi.fn>

  function pendingCard() {
    const h = startRun()
    h.push({
      type: 'permission_needed',
      tool: 'shell_exec',
      command: 'nginx -s reload',
      args: { command: 'nginx -s reload' },
      risk: 'dangerous',
      reversible: false,
      run_id: 'r1',
    })
    const req = h.store.permissionRequests[0]
    expect(req).toBeTruthy()
    return { ...h, req }
  }

  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
    grantMock.mockClear()
  })

  it('回传 run_id 与会话 id，命令取 payload 原文而非 args.command', async () => {
    const { store, req } = pendingCard()
    await store.approvePermission(req.id, 'once')
    expect(grantMock).toHaveBeenCalledWith(
      'shell_exec', 'once', 'nginx -s reload', 'r1', req.conversationId)
  })

  it('成功后卡片保留在数组里（面板要显示「已授权」），但离开待审批队列', async () => {
    const { store, req } = pendingCard()
    await store.approvePermission(req.id, 'once')
    expect(store.permissionRequests).toHaveLength(1)
    expect(req.resolved).toBe(true)
    expect(req.granted).toBe(true)
    expect(store.pendingPermissions).toHaveLength(0)
  })

  it('结算中或已结算的卡片不再重复发授权请求', async () => {
    const { store, req } = pendingCard()
    req.settling = true
    await store.approvePermission(req.id, 'once')
    expect(grantMock).not.toHaveBeenCalled()

    req.settling = false
    req.resolved = true
    await store.approvePermission(req.id, 'once')
    expect(grantMock).not.toHaveBeenCalled()
  })

  it('后端授权失败时卡片留在待审批队列、settling 复位以便重试', async () => {
    const { store, req } = pendingCard()
    grantMock.mockRejectedValueOnce(new Error('500'))
    await store.approvePermission(req.id, 'once')
    expect(req.resolved).toBeFalsy()
    expect(req.settling).toBe(false)
    expect(store.pendingPermissions).toHaveLength(1)
  })

  it('队列里多张卡时只结算指定的那一张', async () => {
    const { store, push, req } = pendingCard()
    push({ type: 'permission_needed', tool: 'file_write', command: '/etc/hosts', risk: 'dangerous', run_id: 'r1' })
    expect(store.pendingPermissions).toHaveLength(2)

    await store.approvePermission(req.id, 'once')
    const other = store.permissionRequests.find(r => r.id !== req.id)
    expect(req.resolved).toBe(true)
    expect(other?.resolved).toBeFalsy()
    expect(store.pendingPermissions).toHaveLength(1)
  })

  it('重复点击是 skipped、后端失败才是 failed —— 调用方据此决定要不要弹错', async () => {
    const { store, req, push } = pendingCard()
    expect(await store.approvePermission(req.id, 'once')).toBe('granted')
    // 同一张卡再点一次：已结算，静默跳过而非报「授权失败」
    expect(await store.approvePermission(req.id, 'once')).toBe('skipped')

    push({ type: 'permission_needed', tool: 'shell_exec', command: 'systemctl restart nginx', risk: 'dangerous', run_id: 'r1' })
    grantMock.mockRejectedValueOnce(new Error('500'))
    expect(await store.approvePermission(store.pendingPermissions[0].id, 'once')).toBe('failed')
  })
})

describe('整程计时与每步耗时', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  it('runAgent 记下整程起点，固化快照的 duration 口径不变', () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1_000)
    const { store } = startRun()
    nowSpy.mockReturnValue(4_600)
    expect(store.runStartedAt).toBe(1_000)
    // 运行视图按这一个数自己 tick；snapshot 仍走同一算式，两处不会量出两个时长
    expect(store.snapshot().duration).toBe(3.6)
    nowSpy.mockRestore()
  })

  it('新一轮 run 重置起点，整程计时不从上一轮接着走', () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1_000)
    startRun()
    nowSpy.mockReturnValue(9_000)
    const { store } = startRun()
    expect(store.runStartedAt).toBe(9_000)
    expect(store.snapshot().duration).toBe(0)
    nowSpy.mockRestore()
  })

  it('tool_call_result 带的 duration_ms 落到该次调用上', () => {
    const { store, push } = startRun()
    push({ type: 'state', state: 'acting', iteration: 0, run_id: 'r1' })
    push({ type: 'tool_call_start', tool: 'shell_exec', args: { command: 'df -h' }, iteration: 0, tool_call_id: 'c1' })
    push({ type: 'tool_call_result', tool: 'shell_exec', result: 'ok', success: true, iteration: 0, tool_call_id: 'c1', duration_ms: 420 })
    expect(store.toolCalls[0].durationMs).toBe(420)
  })

  it('事件里没有 duration_ms 时不写值 —— 不能拿 0 冒充「量过、量到 0」', () => {
    const { store, push } = startRun()
    push({ type: 'tool_call_start', tool: 'shell_exec', args: {}, iteration: 0, tool_call_id: 'c1' })
    push({ type: 'tool_call_result', tool: 'shell_exec', result: 'ok', success: true, iteration: 0, tool_call_id: 'c1' })
    expect(store.toolCalls[0].durationMs).toBeUndefined()
  })

  it('后端量到 0 就如实记 0，怎么标是显示侧的事', () => {
    const { store, push } = startRun()
    push({ type: 'tool_call_start', tool: 'shell_exec', args: {}, iteration: 0, tool_call_id: 'c1' })
    push({ type: 'tool_call_result', tool: 'shell_exec', result: '', success: false, iteration: 0, tool_call_id: 'c1', duration_ms: 0 })
    expect(store.toolCalls[0].durationMs).toBe(0)
  })

  it('时长按 tool_call_id 配对回填，不落进同名的另一条调用', () => {
    const { store, push } = startRun()
    push({ type: 'tool_call_start', tool: 'shell_exec', args: { command: 'a' }, iteration: 0, tool_call_id: 'c1' })
    push({ type: 'tool_call_start', tool: 'shell_exec', args: { command: 'b' }, iteration: 0, tool_call_id: 'c2' })
    push({ type: 'tool_call_result', tool: 'shell_exec', result: 'b 的输出', success: true, iteration: 0, tool_call_id: 'c2', duration_ms: 50 })
    expect(store.toolCalls[0].durationMs).toBeUndefined()
    expect(store.toolCalls[1].durationMs).toBe(50)
    expect(store.toolCalls[1].result).toBe('b 的输出')
  })
})
