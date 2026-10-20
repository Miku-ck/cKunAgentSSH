import { describe, expect, it } from 'vitest'
import type { AgentTraceSnapshot } from '@/api/client'
import { headline, processStatusLabel, toolState } from './process-status'

function trace(over: Partial<AgentTraceSnapshot> = {}): AgentTraceSnapshot {
  return {
    status: 'completed',
    currentState: 'completed',
    iterations: 1,
    toolCalls: [],
    reasoning: '',
    lastRunId: '',
    ...over,
  }
}

describe('processStatusLabel', () => {
  it('运行中优先显示执行中', () => {
    expect(processStatusLabel(trace({ status: 'failed' }), true)).toEqual({ text: '执行中', tone: 'run' })
  })

  it('中止优先于 failed：同一条记录不能显示成失败或完成', () => {
    expect(processStatusLabel(trace({ status: 'failed', aborted: true }), false)).toEqual({ text: '已中止', tone: 'bad' })
  })

  it.each([
    ['failed', '失败'],
    ['human_help', '需要人工'],
    ['max_reached', '达到迭代上限'],
    ['completed', '已完成'],
  ] as const)('%s → %s', (status, text) => {
    expect(processStatusLabel(trace({ status }), false).text).toBe(text)
  })

  it('未知终态回落成已完成，不谎报失败', () => {
    expect(processStatusLabel(trace({ status: 'weird' as never }), false)).toEqual({ text: '已完成', tone: 'ok' })
  })

  it('旧数据没有 aborted 字段时按 status 展示', () => {
    expect(processStatusLabel(trace({ status: 'failed' }), false).text).toBe('失败')
    expect(processStatusLabel(trace({ status: 'completed' }), false).text).toBe('已完成')
  })
})

describe('toolState', () => {
  it('未回填是在途，不是失败', () => {
    expect(toolState({ result: undefined, success: false })).toBe('running')
    expect(toolState({ result: undefined, success: undefined })).toBe('running')
  })

  it('回填后按 success 判成败；空串结果也算跑完了', () => {
    expect(toolState({ result: '', success: true })).toBe('done')
    expect(toolState({ result: 'out', success: false })).toBe('failed')
    expect(toolState({ result: 'out', success: undefined })).toBe('done')
  })
})

describe('headline', () => {
  it('命令执行类直接给 command', () => {
    expect(headline({ tool: 'ssh_exec', args: { command: 'tail -n 5 app.log' } })).toBe('tail -n 5 app.log')
  })

  it('无参数时退回工具名', () => {
    expect(headline({ tool: 'list_dir' })).toBe('list_dir')
    expect(headline({ tool: 'list_dir', args: {} })).toBe('list_dir')
  })

  it('其余工具给参数摘要，最多三项且截断留痕', () => {
    const long = 'x'.repeat(50)
    expect(headline({ tool: 'write_file', args: { path: '/tmp/a', content: long, mode: 'w', extra: 'ignored' } }))
      .toBe(`write_file(path=/tmp/a content=${'x'.repeat(40)}… mode=w)`)
  })
})
