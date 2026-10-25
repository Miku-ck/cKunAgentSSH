/**
 * Q-E3 回归：hosts 托管块的整文件读-改-写必须串行。
 *
 * apply/remove 的「读 hosts → 改托管块 → 写回」跨多个 await；两路并发各自握着
 * 同一份旧内容再写回时，后写者整块覆写先写者的结果（比如 remove 排在 apply 之后
 * 发起，却读到 apply 写盘前的旧态，块明明被要求移除却最终留在文件里）。
 * 修法：apply/remove 进同一条 FIFO 写队列（对齐 github-accel.ts 的 enqueue 纪律），
 * 错误透传给当次调用方、失败不阻塞队列上的后续 op。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const fsMock = vi.hoisted(() => {
  const state = {
    content: '',
    calls: [] as string[],
    readsSnapshot: [] as string[],
    /** 非空时下一次 readFile 挂起直到它 resolve（制造跨 await 的读-写窗口） */
    blocker: null as Promise<void> | null,
  }
  return { state }
})

vi.mock('electron', () => ({
  app: { getPath: () => '/tmp/ckun-agent-ssh-test' },
}))
vi.mock('fs', () => ({ existsSync: () => true }))
vi.mock('fs/promises', () => ({
  readFile: async () => {
    const s = fsMock.state
    s.calls.push('read')
    s.readsSnapshot.push(s.content)
    if (s.blocker) await s.blocker
    return Buffer.from(s.content, 'utf-8')
  },
  writeFile: async (_p: unknown, data: Uint8Array | string) => {
    const s = fsMock.state
    s.calls.push('write')
    s.content = typeof data === 'string' ? data : Buffer.from(data).toString('utf-8')
  },
  copyFile: async () => { fsMock.state.calls.push('copy') },
  mkdir: async () => {},
  unlink: async () => {},
}))

import { GhHostsService } from './gh-hosts'
import { HOSTS_BLOCK_BEGIN } from '../shared/github-accel-core'

const ENTRIES_A = [{ domain: 'github.com', ip: '140.82.112.3' }]
const ENTRIES_B = [{ domain: 'codeload.github.com', ip: '140.82.113.10' }]

beforeEach(() => {
  fsMock.state.content = ''
  fsMock.state.calls = []
  fsMock.state.readsSnapshot = []
  fsMock.state.blocker = null
})

/** 串行判据：第一次 write 之前只允许发生 1 次 read（当次 op 自己的）。
 *  无队列的旧形状里，另一路的 read 也落在第一次 write 之前 → read 数 ≥ 2。 */
function expectSingleReadBeforeFirstWrite(): void {
  const firstWrite = fsMock.state.calls.indexOf('write')
  expect(firstWrite).toBeGreaterThan(-1)
  const readsBefore = fsMock.state.calls.slice(0, firstWrite).filter(c => c === 'read')
  expect(readsBefore, `第一次写之前的 read 次数（实际序列 ${fsMock.state.calls.join('→')}）`).toHaveLength(1)
}

describe('hosts 写队列串行化（Q-E3）', () => {
  it('慢读的 apply 与随后发起的 remove 并发：remove 的读必须等 apply 写完（块被真正移除）', async () => {
    let release!: () => void
    fsMock.state.blocker = new Promise<void>((r) => { release = r })
    const svc = new GhHostsService()
    const applyP = svc.apply(ENTRIES_A)
    const removeP = svc.remove()
    // apply 的读已被 gate 卡住；放行后两个 op 仍按入队顺序继续
    release()
    const [applyStatus, removeStatus] = await Promise.all([applyP, removeP])
    expect(applyStatus.applied).toBe(true)
    expect(removeStatus.applied).toBe(false)
    expectSingleReadBeforeFirstWrite()
    // 最终落盘无托管块：remove 在 apply 写完之后的块上执行
    expect(fsMock.state.content).not.toContain(HOSTS_BLOCK_BEGIN)
  })

  it('两路并发 apply：后一个读到的内容包含前一个写入的块，备份只做一次', async () => {
    const svc = new GhHostsService()
    await Promise.all([svc.apply(ENTRIES_A), svc.apply(ENTRIES_B)])
    const writes = fsMock.state.calls.filter(c => c === 'write')
    expect(writes, '两次 apply 各写一次').toHaveLength(2)
    expectSingleReadBeforeFirstWrite()
    // 第二次 apply 的读快照已含托管块（串行可见性）→ 状态 ok → 不再触发备份
    expect(fsMock.state.readsSnapshot[1]).toContain(HOSTS_BLOCK_BEGIN)
    expect(fsMock.state.calls.filter(c => c === 'copy')).toHaveLength(1)
    // 最终内容为后入队 op 的块（块被整体替换），托管标记仍在
    expect(fsMock.state.content).toContain(HOSTS_BLOCK_BEGIN)
    expect(fsMock.state.content).toContain(ENTRIES_B[0]!.domain)
  })

  it('失败透传给调用方，且不阻塞队列上的后续 op', async () => {
    const svc = new GhHostsService()
    // lastPicks 为空且 entries 为空 → apply 必须拒绝，错误原样到调用方
    await expect(svc.apply()).rejects.toThrow('没有可用的优选结果')
    // 队列没有死掉：后续 remove 正常完成
    const st = await svc.remove()
    expect(st.applied).toBe(false)
  })
})
