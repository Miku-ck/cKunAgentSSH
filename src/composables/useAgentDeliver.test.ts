/**
 * useAgentDeliver 交付状态机测试（可见终端执行模式）
 *
 * 覆盖三处历史缺陷（本轮修复，防回归）：
 * - rejectDeliver 不校验状态：命令已回传终态后再点一次「拒绝」会对同一个
 *   tool_id 再 POST 一条结果（快速双击即命中），后端按重复结果处理。
 * - 拒绝回传缺 rejected 标志：后端一律走 resume()，模型侧看到的是
 *   「命令已写入可见终端」而非「用户拒绝执行」。
 * - deliveries 列表只增不清：store 起新 run 时 _reset() 清空了工具调用与授权
 *   卡片，唯独漏了它 —— 上一轮的"已完成/已拒绝"徽章串进新一轮面板。
 *
 * 另覆盖捕获链路集中化与哨兵容错（可见终端 60s 误判超时事故）：
 * - 全局 onSshData/onPtyData 订阅：捕获不再依赖终端组件实例喂数据；
 * - 哨兵行匹配对 ANSI 转义 / 提示符粘连 / \r\n / 跨 chunk 撕裂容错，
 *   回显的原始命令行不误触发 BEGIN；
 * - 终态细化：done（exit 0）/ failed（exit 非零）/ timeout（60s 兜底）；
 * - 超时结构化诊断（receivedAny / sawBegin / 订阅是否建立）；
 * - 捕获输出超限截断：达到 256KB 上限即停止累加、标记只追加一次，
 *   END 哨兵仍能正常收尾，回传内容同步受限。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => ({
  postToolResult: vi.fn(async () => ({})),
  agentApi: {
    denyCommand: vi.fn(async () => ({ ok: true })),
    releaseRunPermissions: vi.fn(async () => ({})),
  },
}))

// 逐测试可变的 ssh 会话状态（vi.mock 工厂被提升，须经 vi.hoisted 共享）
const sshState = vi.hoisted(() => ({
  sessions: [] as Array<{ id: string; type: string }>,
}))

vi.mock('@/stores/ssh', () => ({
  useSSHStore: () => ({ sessions: sshState.sessions, currentSessionId: '', activeSession: null }),
}))

const { useAgentDeliver, ingestOutput } = await import('@/composables/useAgentDeliver')
const { postToolResult, agentApi } = await import('@/api/client')

const postMock = postToolResult as unknown as ReturnType<typeof vi.fn>
const denyMock = agentApi.denyCommand as unknown as ReturnType<typeof vi.fn>

type DataCb = (data: { id: string; data: string }) => void

/** 安装 window.electronAPI mock（node 测试环境无 window，连带占位） */
function installElectronApi() {
  const sshListeners: DataCb[] = []
  const ptyListeners: DataCb[] = []
  const api = {
    onSshData: vi.fn((cb: DataCb) => {
      sshListeners.push(cb)
      return () => {}
    }),
    onPtyData: vi.fn((cb: DataCb) => {
      ptyListeners.push(cb)
      return () => {}
    }),
    sshWrite: vi.fn(async (_id: string, _data: string) => {}),
    ptyWrite: vi.fn(async (_id: string, _data: string) => {}),
  }
  ;(globalThis as any).window = { electronAPI: api }
  return { api, sshListeners, ptyListeners }
}

/** 等待 handleDeliver → deliverCommand 的微任务链走完（status 落到 capturing） */
async function flushAsync(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve()
}

/** 登记一条等待用户确认的交付 */
function addPending(toolId: string) {
  useAgentDeliver().handleDeliver({
    tool_id: toolId,
    command: `rm -rf /tmp/${toolId}`,
    session_id: 'sess-1',
    requires_confirm: true,
    runId: 'run-1',
  })
}

/** 自动交付（requires_confirm=false）写入终端并等捕获就绪，返回哨兵 marker */
async function deliverAuto(toolId: string, command = 'echo hi'): Promise<string> {
  useAgentDeliver().handleDeliver({
    tool_id: toolId,
    command,
    session_id: 'sess-1',
    requires_confirm: false,
    runId: 'run-1',
  })
  // deliverCommand 同步执行到 await writer(...)：调用参数（哨兵串）已记录在案
  const api = (globalThis as any).window.electronAPI as ReturnType<typeof installElectronApi>['api']
  const calls = api.sshWrite.mock.calls
  const written = calls[calls.length - 1]?.[1]
  const m = typeof written === 'string' ? /__AGENT_BEGIN__ ([a-z0-9]+)/.exec(written) : null
  if (!m) throw new Error(`sentinel not written: ${String(written)}`)
  await flushAsync()
  return m[1]
}

function deliveryOf(toolId: string) {
  return useAgentDeliver().deliveries.value.find(x => x.tool_id === toolId)
}

// node 环境无 window：占位让模块内 window.electronAPI 访问安全；会话供 deliverCommand 写入
beforeEach(() => {
  ;(globalThis as any).window = {}
  sshState.sessions = [{ id: 'sess-1', type: 'remote' }]
})

describe('可见终端交付', () => {
  beforeEach(() => {
    useAgentDeliver().resetDeliveries()
    postMock.mockClear()
    denyMock.mockClear()
  })

  it('同一 tool_id 只回传一次终态', async () => {
    const d = useAgentDeliver()
    addPending('t1')
    await d.rejectDeliver('t1')
    // 第二次点击（列表还没重绘）必须被状态门挡住
    await d.rejectDeliver('t1')

    expect(postMock).toHaveBeenCalledTimes(1)
    // 必须以 rejected=true 回传：后端据此走 reject()。漏掉时 Driver 写成
    // 「[已写入可见终端] rm -rf …」，模型以为破坏性命令已在真实终端执行过
    // 末两位（S1-03）：run_id 归属 + confirmed 主张。拒绝这条腿没经过 deliverCommand，
    // 所以 confirmed=false 是对的 —— 后端对 rejected 本来也不查确认。
    expect(postMock.mock.calls[0]).toEqual(['t1', '[用户拒绝执行]', false, null, true, 'run-1', false])
    expect(denyMock).toHaveBeenCalledTimes(1)
    expect(d.deliveries.value).toHaveLength(1)
    expect(d.deliveries.value[0].status).toBe('rejected')
  })

  it('新 run 起手清空上一轮徽章', () => {
    const d = useAgentDeliver()
    addPending('t1')
    expect(d.deliveries.value).toHaveLength(1)

    d.resetDeliveries()
    expect(d.deliveries.value).toHaveLength(0)
    // 清空后确认/拒绝都是空操作，不会凭空造出交付
    d.confirmDeliver('t1')
    expect(d.deliveries.value).toHaveLength(0)
  })

  it('重复的 tool_deliver 事件不会叠加卡片', () => {
    const d = useAgentDeliver()
    addPending('t1')
    addPending('t1')
    expect(d.deliveries.value).toHaveLength(1)
  })
})

// 注意顺序：本 describe 必须先于「哨兵行匹配」执行 —— 全局订阅是模块级一次性
// 懒初始化，首次在 electronAPI 就绪状态下的 handleDeliver 会消耗掉这次订阅。
describe('全局捕获订阅集中化', () => {
  beforeEach(() => {
    useAgentDeliver().resetDeliveries()
    postMock.mockClear()
  })

  it('首次交付自动全局订阅，回显经全局回调完成捕获且不重复订阅', async () => {
    const { api, sshListeners, ptyListeners } = installElectronApi()

    const marker = await deliverAuto('t-g1')

    // 首次 handleDeliver：onSshData/onPtyData 各订阅恰好一次
    expect(api.onSshData).toHaveBeenCalledTimes(1)
    expect(api.onPtyData).toHaveBeenCalledTimes(1)
    expect(sshListeners).toHaveLength(1)
    expect(ptyListeners).toHaveLength(1)

    // 终端组件未挂载也无妨：数据从全局订阅回调进入捕获（集中化路径）
    sshListeners[0]({
      id: 'sess-1',
      data: `__AGENT_BEGIN__ ${marker}\r\nhello-from-global\r\n__AGENT_END__ ${marker}:0\r\n`,
    })

    await flushAsync()
    const d = deliveryOf('t-g1')
    expect(d?.status).toBe('done')
    expect(d?.exit_code).toBe(0)
    expect(postMock).toHaveBeenCalledTimes(1)
    // 走过 deliverCommand（自动交付或用户确认）⇒ 回传带 run_id 且 confirmed=true（S1-03）
    expect(postMock.mock.calls[0]).toEqual(['t-g1', 'hello-from-global', true, 0, false, 'run-1', true])

    // 再次交付不重复订阅（模块级标志防重）
    useAgentDeliver().handleDeliver({
      tool_id: 't-g2',
      command: 'echo x',
      session_id: 'sess-1',
      requires_confirm: true,
      runId: 'run-1',
    })
    expect(api.onSshData).toHaveBeenCalledTimes(1)
    expect(api.onPtyData).toHaveBeenCalledTimes(1)
  })
})

describe('哨兵行匹配与捕获终态', () => {
  beforeEach(() => {
    useAgentDeliver().resetDeliveries()
    postMock.mockClear()
    installElectronApi()
  })

  it('回显的原始命令行（含哨兵原文）不触发 BEGIN', async () => {
    const marker = await deliverAuto('t-echo', 'du -sh /tmp')
    // 终端回显的原始命令行：提示符 + 完整 sentinel（BEGIN 后面还跟着 ; 命令 ; END）
    ingestOutput(
      'sess-1',
      `admin-pl@host:~$ echo __AGENT_BEGIN__ ${marker}; du -sh /tmp; echo __AGENT_END__ ${marker}:$?\r\n`,
    )
    let d = deliveryOf('t-echo')
    expect(d?.status).toBe('capturing') // 未被回显行误触发
    expect(postMock).not.toHaveBeenCalled()

    ingestOutput(
      'sess-1',
      `__AGENT_BEGIN__ ${marker}\r\n4.0K\t/tmp\r\n__AGENT_END__ ${marker}:0\r\n`,
    )
    await flushAsync()
    d = deliveryOf('t-echo')
    expect(d?.status).toBe('done')
    expect(d?.output).toBe('4.0K\t/tmp')
    expect(d?.exit_code).toBe(0)
  })

  it('\\r\\n 行尾正常命中', async () => {
    const marker = await deliverAuto('t-crlf')
    ingestOutput(
      'sess-1',
      `__AGENT_BEGIN__ ${marker}\r\nline-1\r\nline-2\r\n__AGENT_END__ ${marker}:0\r\n`,
    )
    await flushAsync()
    const d = deliveryOf('t-crlf')
    expect(d?.status).toBe('done')
    expect(d?.output).toBe('line-1\nline-2')
    expect(d?.exit_code).toBe(0)
  })

  it('跨 chunk 撕裂（BEGIN/输出/END 各拆两半）仍完整捕获', async () => {
    const marker = await deliverAuto('t-split')
    const begin = `__AGENT_BEGIN__ ${marker}`
    const end = `__AGENT_END__ ${marker}:0`
    const beginTornAt = Math.floor(begin.length / 2)
    const endTornAt = `__AGENT_END__ ${marker.slice(0, 3)}`.length // END 行在 marker 中间撕裂
    ingestOutput('sess-1', begin.slice(0, beginTornAt))
    ingestOutput('sess-1', begin.slice(beginTornAt) + '\r\nhello ')
    ingestOutput('sess-1', 'world\r\n' + end.slice(0, endTornAt))
    ingestOutput('sess-1', end.slice(endTornAt) + '\r\n')
    await flushAsync()
    const d = deliveryOf('t-split')
    expect(d?.status).toBe('done')
    expect(d?.output).toBe('hello world')
    expect(d?.exit_code).toBe(0)
  })

  it('BEGIN 前有提示符/空行等杂项行不影响捕获', async () => {
    const marker = await deliverAuto('t-noise')
    ingestOutput(
      'sess-1',
      'Last login: Mon Sep 21 10:00:00 2026 from 1.2.3.4\r\n\r\nuser@host:~$ \r\n',
    )
    ingestOutput(
      'sess-1',
      `__AGENT_BEGIN__ ${marker}\r\nok\r\n__AGENT_END__ ${marker}:0\r\n`,
    )
    await flushAsync()
    const d = deliveryOf('t-noise')
    expect(d?.status).toBe('done')
    expect(d?.output).toBe('ok')
  })

  it('退出码解析：0 → done、1 → failed、空 → 按 0', async () => {
    const m0 = await deliverAuto('t-exit0')
    ingestOutput('sess-1', `__AGENT_BEGIN__ ${m0}\r\n__AGENT_END__ ${m0}:0\r\n`)
    const m1 = await deliverAuto('t-exit1')
    ingestOutput('sess-1', `__AGENT_BEGIN__ ${m1}\r\n__AGENT_END__ ${m1}:1\r\n`)
    const mE = await deliverAuto('t-exitE')
    ingestOutput('sess-1', `__AGENT_BEGIN__ ${mE}\r\n__AGENT_END__ ${mE}:\r\n`)
    await flushAsync()

    const d0 = deliveryOf('t-exit0')
    const d1 = deliveryOf('t-exit1')
    const dE = deliveryOf('t-exitE')
    expect(d0?.exit_code).toBe(0)
    expect(d0?.status).toBe('done')
    expect(d1?.exit_code).toBe(1)
    expect(d1?.status).toBe('failed')
    expect(dE?.exit_code).toBe(0)
    expect(dE?.status).toBe('done')

    // 失败结果按 success=false 回传，与汇总栏失败口径一致
    const call1 = postMock.mock.calls.find(c => c[0] === 't-exit1')
    expect(call1?.[2]).toBe(false)
  })

  it('行内含 ANSI 转义（CSI 着色 / OSC 标题）仍命中', async () => {
    const marker = await deliverAuto('t-ansi')
    // OSC 标题序列 + 带色提示符粘连 BEGIN（走 endsWith），CSI 包裹的哨兵行
    ingestOutput(
      'sess-1',
      `\x1b]0;user@host:~\x07\x1b[01;32muser@host\x1b[00m:~$ \x1b[32m__AGENT_BEGIN__ ${marker}\x1b[0m\r\n`,
    )
    ingestOutput('sess-1', `\x1b[33mwarn-line\x1b[0m\r\n`)
    ingestOutput('sess-1', `\x1b[31m__AGENT_END__ ${marker}:1\x1b[0m\r\n`)
    await flushAsync()
    const d = deliveryOf('t-ansi')
    expect(d?.status).toBe('failed')
    expect(d?.exit_code).toBe(1)
    expect(d?.output).toBe('warn-line') // 采集输出同样清理 ANSI
  })

  it('提示符粘连 BEGIN（user@host:~$ __AGENT_BEGIN__ m）经 endsWith 命中', async () => {
    const marker = await deliverAuto('t-prompt')
    ingestOutput(
      'sess-1',
      `user@host:~$ __AGENT_BEGIN__ ${marker}\r\nout\r\n__AGENT_END__ ${marker}:0\r\n`,
    )
    await flushAsync()
    const d = deliveryOf('t-prompt')
    expect(d?.status).toBe('done')
    expect(d?.output).toBe('out')
  })

  it('60s 未见 END → status timeout、success=false 回传，并输出结构化诊断', async () => {
    vi.useFakeTimers()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const marker = await deliverAuto('t-timeout')
      // 有回显数据、命中过 BEGIN，但 END 永远不来（如交互式命令挂起）
      ingestOutput('sess-1', `__AGENT_BEGIN__ ${marker}\r\nstill-running...\r\n`)

      await vi.advanceTimersByTimeAsync(60_000)

      const d = deliveryOf('t-timeout')
      expect(d?.status).toBe('timeout')
      expect(d?.output).toContain('still-running...')
      expect(d?.output).toContain('执行超时')
      const last = postMock.mock.calls[postMock.mock.calls.length - 1]
      expect(last?.[0]).toBe('t-timeout')
      expect(last?.[2]).toBe(false) // success=false

      // 结构化诊断：收到过数据、命中过 BEGIN → 指向 END 断流而非订阅断连
      const payloads = warnSpy.mock.calls
        .filter(c => String(c[0]).includes('捕获超时'))
        .map(c => c[1] as Record<string, unknown>)
      expect(payloads).toHaveLength(1)
      expect(payloads[0]).toMatchObject({
        tool_id: 't-timeout',
        session_id: 'sess-1',
        receivedAny: true,
        sawBegin: true,
      })
    } finally {
      warnSpy.mockRestore()
      vi.useRealTimers()
    }
  })
})

describe('捕获输出超限截断', () => {
  beforeEach(() => {
    useAgentDeliver().resetDeliveries()
    postMock.mockClear()
    installElectronApi()
  })

  /** 生成 n 行 1KB 的回显（\r\n 行尾，贴合真实终端流；n 行约 n KB） */
  function burstLines(n: number): string {
    const line = 'x'.repeat(1000)
    return Array.from({ length: n }, () => line).join('\r\n') + '\r\n'
  }

  it('输出达到 256KB 上限即停止累加：超限期间不收尾，END 到来才闭合且回传受限', async () => {
    const marker = await deliverAuto('t-cap-trunc')
    ingestOutput('sess-1', `__AGENT_BEGIN__ ${marker}\r\n`)
    ingestOutput('sess-1', burstLines(300)) // ~300KB，越过 256KB 上限
    // 超限不等于收尾：还没看到 END 哨兵，不回传、不落终态
    expect(deliveryOf('t-cap-trunc')?.status).toBe('capturing')
    expect(postMock).not.toHaveBeenCalled()

    // 截断后 END 哨兵仍能正常收尾
    ingestOutput('sess-1', `__AGENT_END__ ${marker}:0\r\n`)
    await flushAsync()

    const d = deliveryOf('t-cap-trunc')
    expect(d?.status).toBe('done')
    expect(d?.exit_code).toBe(0)
    // 采集在 256KB 上限处截断，尾部是固定的截断标记
    expect(d?.output?.endsWith('[输出已截断，超过 256KB 上限]')).toBe(true)
    // 采集内容受限：不再随注入量继续增长
    expect(d?.output?.length).toBeLessThan(256 * 1024)
    // 回传与卡片展示是同一份受限内容：轨迹里不会出现全量输出
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][1]).toBe(d?.output)
  })

  it('截断标记只追加一次：之后持续回显（tail -f 式突发）不再累加', async () => {
    const marker = await deliverAuto('t-cap-once')
    ingestOutput('sess-1', `__AGENT_BEGIN__ ${marker}\r\n`)
    ingestOutput('sess-1', burstLines(300))
    ingestOutput('sess-1', burstLines(300))
    ingestOutput('sess-1', burstLines(300))
    ingestOutput('sess-1', `__AGENT_END__ ${marker}:0\r\n`)
    await flushAsync()

    const out = deliveryOf('t-cap-once')?.output ?? ''
    // 恰好出现 1 次：split 出 2 段
    expect(out.split('[输出已截断，超过 256KB 上限]')).toHaveLength(2)
    // 三波共 ~900KB 注入，回传仍停在截断时那一份
    expect(out.length).toBeLessThan(256 * 1024)
  })

  it('截断后 60s 超时兜底：回传的仍是受限内容而非全量输出', async () => {
    vi.useFakeTimers()
    try {
      const marker = await deliverAuto('t-cap-timeout')
      ingestOutput('sess-1', `__AGENT_BEGIN__ ${marker}\r\n`)
      ingestOutput('sess-1', burstLines(600))
      await vi.advanceTimersByTimeAsync(60_000)

      const d = deliveryOf('t-cap-timeout')
      expect(d?.status).toBe('timeout')
      expect(d?.output).toContain('[输出已截断，超过 256KB 上限]')
      expect(d?.output).toContain('执行超时')
      const last = postMock.mock.calls[postMock.mock.calls.length - 1]
      expect(last?.[0]).toBe('t-cap-timeout')
      expect((last?.[1] as string).length).toBeLessThan(256 * 1024 + 64)
    } finally {
      vi.useRealTimers()
    }
  })
})
