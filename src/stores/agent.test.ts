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
    releaseRunPermissions: vi.fn(async () => ({ ok: true })),
  },
  postToolResult: vi.fn(async () => ({})),
}))

vi.mock('@/stores/ssh', () => ({
  useSSHStore: () => ({ sessions: [], currentSessionId: '', activeSession: null }),
}))

const { useAgentStore } = await import('@/stores/agent')
const { agentApi } = await import('@/api/client')
// 档位表是纯函数（无 store 依赖），可以直接静态导入
import { grantScopesForRisk } from '@/utils/approval-scopes'

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

  it('新一轮 run 把上一轮的待放行整批清掉 —— 队列里不会同时躺着两条 run 的卡', () => {
    const { store, push } = startRun()
    addCard(store, push)
    expect(store.pendingPermissions).toHaveLength(1)
    // 故事 14 写的"看出各条来自哪个 run"在单 run 模型下是**恒等**的：_reset() 连着
    // 清 permissionRequests 与 lastRunId。所以归属只在队头说一次，不每张卡印一遍
    // —— 印了就是把同一条 run 号重复 N 次，而 run 号一变这句才会开始承载信息。
    store.runAgent({ task: '换一条', conversationId: 'c1', modelId: 'm1' })
    expect(store.pendingPermissions).toHaveLength(0)
    expect(store.lastRunId).toBe('')
  })

  it('只剩待放行卡片时不挂运行通知区 —— 决策位只有消息流里的内联审批卡', () => {
    const { store, push } = startRun()
    addCard(store, push)
    expect(store.permissionRequests).toHaveLength(1)
    // 审批卡内联在那一轮回答的消息气泡里，首页任务台本身就把消息流摊在眼前；
    // 通知区再为它挂一块容器等于同一件事说两遍。顶栏徽章与首页状态点才是
    // 「有东西在等」的告警信道（useAgentPresence 读 pendingPermissions）。
    expect(store.hasRunNotice).toBe(false)
  })

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

  it('交付事件携带的后果说明透传到交付条目，LLM 补发按 tool_id 回填', () => {
    const { store, push } = startRun()
    push({
      type: 'tool_deliver',
      tool_id: 'td-2',
      command: 'rm /etc/nginx/nginx.conf',
      session_id: 's1',
      requires_confirm: true,
      run_id: 'r1',
      explain: '删除系统关键路径下的文件，可能造成软件损坏。',
      explain_source: 'static',
    })
    expect(store.deliveries[0].explain).toContain('删除系统关键路径')

    // 未知命令族：LLM 解释后补，前端按 tool_id 精确找回交付条目
    push({
      type: 'permission_explain',
      tool: 'shell_exec',
      command: 'mycohortool --run',
      explanation: '运行 mycohortool 工具并输出结果。',
      tool_id: 'td-2',
    })
    expect(store.deliveries[0].explain).toBe('运行 mycohortool 工具并输出结果。')
    expect(store.deliveries[0].explain_source).toBe('llm')
  })

  it('permission_explain 不带 tool_id 时走审批卡回填，不误伤交付条目', () => {
    const { store, push } = startRun()
    push({
      type: 'tool_deliver',
      tool_id: 'td-3',
      command: 'systemctl restart nginx',
      session_id: 's1',
      requires_confirm: true,
      run_id: 'r1',
      explain: '重启 nginx 服务。',
      explain_source: 'static',
    })
    push({
      type: 'permission_needed',
      tool: 'shell_exec',
      command: 'systemctl restart nginx',
      risk: 'dangerous',
      scopes: [{ id: 'once', label: '仅本次' }],
      run_id: 'r1',
    })
    push({
      type: 'permission_explain',
      tool: 'shell_exec',
      command: 'systemctl restart nginx',
      explanation: '重启 nginx 进程，Web 服务会短暂中断。',
    })
    // 审批卡拿到回填，交付条目保留 tool_deliver 自带的静态说明
    expect(store.permissionRequests[0].explain).toBe('重启 nginx 进程，Web 服务会短暂中断。')
    expect(store.deliveries[0].explain).toBe('重启 nginx 服务。')
    expect(store.deliveries[0].explain_source).toBe('static')
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

  it('迭代耗尽（max_reached）不报「完成」，快照里保留原始终态', () => {
    const { store, push } = startRun()
    // S9-13：done 事件带的是后端原始终态，原分支只列了 failed / human_help，
    // max_reached 落到 else ⇒ 面板与固化记录都说「完成」。
    push({ type: 'done', state: 'max_reached', iterations: 12, run_id: 'r1' })
    expect(store.status).toBe('failed')
    expect(store.snapshot().status).toBe('max_reached')
  })

  it('需要人工介入（human_help）在快照里不再被折成 failed', () => {
    const { store, push } = startRun()
    push({ type: 'done', state: 'human_help', iterations: 4, run_id: 'r1' })
    expect(store.status).toBe('failed')
    // 折叠是「为了兼容两值类型」的产物；类型补全后原样落库，
    // 卡片才分得清「请求人工介入」与「失败」
    expect(store.snapshot().status).toBe('human_help')
  })

  it('human_help_needed 的 suggestion（失败诊断建议）透传到求助卡，缺省存空串', () => {
    const { store, push } = startRun()
    push({ type: 'human_help_needed', reason: '执行通道不可用', suggestion: '检查 SSH 会话后重试' })
    expect(store.humanHelpRequests[0].suggestion).toBe('检查 SSH 会话后重试')
    const h2 = startRun()
    h2.push({ type: 'human_help_needed', reason: '仅低置信度，无诊断' })
    expect(h2.store.humanHelpRequests[0].suggestion).toBe('')
  })

  it('正常完成仍记 completed', () => {
    const { store, push } = startRun()
    push({ type: 'done', state: 'completed', iterations: 2, run_id: 'r1' })
    expect(store.status).toBe('completed')
    expect(store.snapshot().status).toBe('completed')
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

describe('风险等级缺失时不得替用户判成宽松档', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
    denyMock.mockResolvedValue({ ok: true })
  })

  it('事件没带 risk → 卡片按「后端没说过」处理，档位回落到 fail-closed', () => {
    const { store, push } = startRun()
    // telemetry.to_sse_dict 对缺字段发的是**空串**（`payload.get("risk", "")`），
    // 所以线上真实形态就是 risk:''。曾写成 `event.risk || 'moderate'`：空串被换成
    // 中风险档、卡片多出「本会话允许」—— 正是 approval-scopes 头部写明的"假承诺"
    // （后端对 dangerous 刻意忽略 session 缓存，按钮点了也不兑现）。
    push({ type: 'permission_needed', tool: 'shell_exec', command: 'kill -9 1234', risk: '', run_id: 'r1' })
    const req = store.permissionRequests[0]
    expect(req).toBeTruthy()
    expect(req.risk).toBe('')
    expect(grantScopesForRisk(req.risk)).toEqual(['once', 'always'])
  })

  it('显式 moderate 仍给 session 档（上一条不是一刀切掉档位）', () => {
    const { store, push } = startRun()
    push({ type: 'permission_needed', tool: 'service_manage', command: '', risk: 'moderate', run_id: 'r1' })
    const req = store.permissionRequests[0]
    expect(req.risk).toBe('moderate')
    expect(grantScopesForRisk(req.risk)).toEqual(['once', 'session', 'always'])
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

  it('lastTask/lastSshSessionId 记的是本次 run 的入参，新一轮覆写', () => {
    const store = useAgentStore()
    store.runAgent({ task: '看看 web-01 内存', conversationId: 'c1', modelId: 'm1', sshSessionId: 's1' })
    expect(store.lastTask).toBe('看看 web-01 内存')
    expect(store.lastSshSessionId).toBe('s1')
    // run-head 的指令原文与目标主机都读这两格，不去捞"当前会话最后一条消息 /
    // 此刻的活动会话"—— 那两种捞法在运行中切了会话就指向另一个 run（M-19 同因）
    store.runAgent({ task: '换一条：清磁盘', conversationId: 'c1', modelId: 'm1' })
    expect(store.lastTask).toBe('换一条：清磁盘')
    expect(store.lastSshSessionId).toBe('')
  })

  it('模型与审批档在发起那一刻记下，运行中改设置不改写这一轮', () => {
    const store = useAgentStore()
    store.runAgent({ task: 't', conversationId: 'c1', modelId: 'm-a', permissionMode: 'auto' })
    expect(store.lastModelId).toBe('m-a')
    expect(store.lastPermissionMode).toBe('auto')
    // 未表态时记的是请求体实际发出去的那一档（PERMISSION_MODE_DEFAULT），不是空：
    // run-head 因此能诚实说「手动审批」，而不是这一格干脆不显示
    store.runAgent({ task: 't2', conversationId: 'c1', modelId: 'm-b' })
    expect(store.lastModelId).toBe('m-b')
    expect(store.lastPermissionMode).toBe('normal')
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

describe('run 终态的四处清理必须一致（S1-02）', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  const releaseMock = (agentApi as any).releaseRunPermissions as ReturnType<typeof vi.fn>

  /** 铺好「澄清向导开着 + 一条待确认交付」的现场 */
  function armedRun() {
    const h = startRun()
    h.push({ type: 'state', state: 'thinking', run_id: 'r1' })
    h.push({
      type: 'clarification_needed',
      run_id: 'r1',
      tool: 'shell_exec',
      tool_call_id: 'tc-r1',
      args: {},
      reason: '命令含通配符',
      question: '确认目标文件？',
      options: [{ id: 'a', label: 'A' }],
      current_step: 0,
      total_steps: 1,
    })
    h.push({
      type: 'tool_deliver',
      tool_id: 'td-1',
      command: 'rm -rf /tmp/x',
      session_id: 's1',
      requires_confirm: true,
      run_id: 'r1',
    })
    expect(h.store.clarificationWizard).not.toBeNull()
    expect(h.store.deliveries[0].status).toBe('pending_confirm')
    return h
  }

  it('流被截断（onError）也要关向导、并让待确认交付失去派发能力', () => {
    const h = armedRun()
    releaseMock.mockClear()

    runs[runs.length - 1].onError('stream cut')

    expect(h.store.clarificationWizard).toBeNull()
    expect(h.store.deliveries[0].status).toBe('aborted')
    // 流是被截断的：服务端未必走过终态清理，这一次释放要带上 run_id 试一次
    expect(releaseMock).toHaveBeenCalledWith('r1')
  })

  it('done 终态事件关向导、夺走派发能力，但不再 POST 释放', () => {
    const h = armedRun()
    releaseMock.mockClear()

    h.push({ type: 'done', iterations: 2, run_id: 'r1', state: 'completed' })

    expect(h.store.clarificationWizard).toBeNull()
    expect(h.store.deliveries[0].status).toBe('aborted')
    expect(releaseMock).not.toHaveBeenCalled()
  })

  it('error 终态事件同口径', () => {
    const h = armedRun()
    releaseMock.mockClear()

    h.push({ type: 'error', message: 'boom', run_id: 'r1' })

    expect(h.store.clarificationWizard).toBeNull()
    expect(h.store.deliveries[0].status).toBe('aborted')
    expect(releaseMock).not.toHaveBeenCalled()
  })
})

/**
 * 上一轮 run 的黄色告警不能跟到空白任务上：离开会话（新建/切换/删除）时
 * 必须整块清掉运行通知，否则首页「今天想让助手替你做什么？」下面还挂着
 * 「[生产强化] …拒绝执行: sudo command」，看起来像这一轮刚发生的事。
 */
describe('clearRunNotice', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  /** 铺好一堆运行通知，再把 run 收尾成非运行态 */
  function endedRunWithNotice() {
    const h = startRun()
    h.push({ type: 'warning', message: '[生产强化] 危险命令在 prod 环境仅接受预授权 allowlist 放行。拒绝执行: sudo command' })
    h.push({
      type: 'intention',
      intention: {
        level: 'simple', task_type: 'query', reasoning_budget: null,
        allow_planning_phase: false, confidence: 0.9,
      },
    })
    h.push({ type: 'human_help_needed', reason: '请人工确认目标机' })
    h.store.status = 'completed'
    return h.store
  }

  it('清空警告/错误/规划/求助/审批卡/向导', () => {
    const store = endedRunWithNotice()
    store.errorMessage = 'boom'
    store.permissionRequests.push({
      id: 'p1', tool: 'shell_exec', args: {}, risk: 'dangerous',
      reversible: false, message: '需要授权', timestamp: 1,
    })
    expect(store.hasRunNotice).toBe(true)

    store.clearRunNotice()

    expect(store.warnings).toEqual([])
    expect(store.errorMessage).toBe('')
    expect(store.intentionInfo).toBeNull()
    expect(store.humanHelpRequests).toEqual([])
    expect(store.permissionRequests).toEqual([])
    expect(store.clarificationWizard).toBeNull()
    expect(store.hasRunNotice).toBe(false)
  })

  it('运行中不清 —— 那是正在跑的这一轮要看的通知', () => {
    const h = startRun()
    h.push({ type: 'warning', message: '正在进行中的告警' })

    h.store.clearRunNotice()

    expect(h.store.warnings).toEqual(['正在进行中的告警'])
  })
})
