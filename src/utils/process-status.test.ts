import { describe, expect, it } from 'vitest'
import { headline } from './process-status'

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
