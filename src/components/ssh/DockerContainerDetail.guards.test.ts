/**
 * DockerContainerDetail 守卫
 *
 * 与 DockerView.guards.test.ts 同口径：没有组件测试环境，钉「回包前重新校验发起上下文」
 * 这类人工点测极难稳定复现的形状。详情弹窗同时跑 inspect / stats / logs 三条腿，
 * 任何一条的旧回包写错位置，都会让操作员对着另一台主机/另一个容器的数据排障。
 */
import detailRaw from './DockerContainerDetail.vue?raw'
import { describe, expect, it } from 'vitest'

const src = detailRaw.replace(/\r\n/g, '\n')

function sliceFunction(header: string): string {
  const start = src.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = src.indexOf('\n}\n', start)
  expect(end).toBeGreaterThan(start)
  return src.slice(start, end)
}

describe('三条取数腿的陈旧写入', () => {
  it('inspect 用序号校验后才写回', () => {
    const body = sliceFunction('async function loadInspect(): Promise<void> {')
    expect(body).toContain('inspectSeq')
    expect(body).toContain('disposed')
    expect(body).toContain('parseContainerInspect')
  })

  it('stats 用序号 + 在途锁，旧回包不得盖新回包/重复叠加', () => {
    const body = sliceFunction('async function loadStats(): Promise<void> {')
    expect(body).toContain('statsSeq')
    expect(body).toContain('statsInFlight')
    expect(body).toContain('disposed')
    expect(body).toContain('parseContainerStats')
  })

  it('日志轮询带序号', () => {
    const body = sliceFunction('async function loadLogs(quiet = false): Promise<void> {')
    expect(body).toContain('logsSeq')
    expect(body).toContain('disposed')
  })
})

describe('轮询边界', () => {
  it('stats 只在概览段且容器运行时轮询', () => {
    const body = sliceFunction('function syncStatsTimer(): void {')
    expect(body).toContain("tab.value === 'overview'")
    expect(body).toContain('isRunning.value')
  })

  it('日志只在切到日志段时首次加载，离开即停跟随', () => {
    expect(src).toMatch(
      /watch\(tab, \(next\) => \{[\s\S]*?next === 'logs'[\s\S]*?loadLogs\(\)[\s\S]*?setFollow\(false\)/,
    )
  })

  it('卸载时作废在途回包并清掉两个轮询', () => {
    expect(src).toMatch(
      /onUnmounted\(\(\) => \{[\s\S]*?disposed = true[\s\S]*?stopStatsTimer\(\)[\s\S]*?stopFollowTimer\(\)/,
    )
  })
})
