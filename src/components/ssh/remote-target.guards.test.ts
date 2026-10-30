/**
 * 远程目标底座守卫
 *
 * 这文件存在的理由只有一个：它守的是「两个页面共用一份」的那段代码。
 * 抽之前这些形状各自埋在 docker/context.ts 的注释里；抽之后 Docker 页和 K8s 页
 * 都从这里取，一处写坏两处一起坏，所以把最贵的两条钉成源码形状断言
 * （没有组件测试环境，与 DockerView.guards.test.ts 同一套路子）。
 */
import { describe, expect, it } from 'vitest'
import remoteTargetRaw from './remote-target.ts?raw'
import dockerCtxRaw from './docker/context.ts?raw'

// 工作区是 CRLF 检出：统一成 LF 再切片，否则 '\n}\n' 找不到函数结尾
const lf = (s: string): string => s.replace(/\r\n/g, '\n')
const src = lf(remoteTargetRaw)

function sliceFunction(text: string, header: string): string {
  const start = text.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = text.indexOf('\n  }\n', start)
  expect(end).toBeGreaterThan(start)
  return text.slice(start, end)
}

describe('useRemoteTarget 的连接与目标校正', () => {
  it('目标取 connectRemote 返回的会话 id，不按 connectionId 回查', () => {
    const body = sliceFunction(src, 'async function handleConnect(conn: SSHConnection): Promise<void> {')
    expect(body).toContain('targetId.value = await sshStore.connectRemote(')
    // 回查会在同主机已有会话时命中最早那条，新开的那条没人认领：多占一个远端 PTY
    expect(body).not.toContain('connections.find(')
  })
  it('握手/认证失败经 toFriendlyMessage 再进 toast，不端原始错误', () => {
    const body = sliceFunction(src, 'async function handleConnect(conn: SSHConnection): Promise<void> {')
    expect(body).toContain('toFriendlyMessage(err,')
  })
  it('远程列表变化时把目标校正到活动会话，且首建即取（immediate）', () => {
    const start = src.indexOf('watch(remoteSessions')
    expect(start).toBeGreaterThanOrEqual(0)
    const body = src.slice(start, src.indexOf('}, { immediate', start))
    expect(body).toContain('sshStore.activeSessionId')
    expect(src.slice(start, start + 400)).toContain("{ immediate: true }")
  })
})

describe('动作锁', () => {
  it('已有动作在途时直接返回，不重复下发', () => {
    const body = sliceFunction(src, 'async function withBusyKey(key: string, fn: () => Promise<void>): Promise<void> {')
    expect(body).toContain('if (busy.value) return')
    expect(body).toContain('finally { busy.value = ')
  })
})

describe('共用而非手抄', () => {
  it('Docker 页从底座取目标，不再自己声明一份', () => {
    const ctx = lf(dockerCtxRaw)
    expect(ctx).toContain("from '../remote-target'")
    expect(ctx).toContain('useRemoteTarget()')
    expect(ctx).toContain('...remoteTarget,')
    expect(ctx).not.toContain('sshStore.connectRemote(')
  })
})
