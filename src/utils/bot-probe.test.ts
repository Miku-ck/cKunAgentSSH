import { describe, expect, it } from 'vitest'
import type { ProactiveProbeResult } from '@/api/bot'
import { probeOutcomeText } from './bot-probe'

const base: ProactiveProbeResult = {
  conversation_id: 'c1',
  action: 'push',
  desire: 0.2,
  gate_reason: '',
  text: '想起你上次说的那个展',
  detail: '',
}

describe('「试一句」结果文案', () => {
  it('真发出去了就照实说，并把那句话回显（群主动那句不落本地记录）', () => {
    const text = probeOutcomeText(base)
    expect(text).toContain('已发出')
    expect(text).toContain('想起你上次说的那个展')
  })

  it('自动路径本来过不了闸时补一句原因', () => {
    expect(probeOutcomeText({ ...base, gate_reason: 'desire 0.20 < 0.55' })).toContain('desire 0.20 < 0.55')
  })

  it('能自动开口就别硬造一句「本来会被拦」', () => {
    expect(probeOutcomeText(base)).not.toContain('卡在')
  })

  it('直投、寄存、被平台拒、人格没了各说各的，不许混成「已发出」', () => {
    expect(probeOutcomeText({ ...base, action: 'send' })).not.toContain('已发出')
    expect(probeOutcomeText({ ...base, action: 'queued', detail: 'manual:queued:offline:free' })).toContain('寄存')
    expect(probeOutcomeText({ ...base, action: 'skip', detail: 'push:quota' })).toContain('quota')
    expect(probeOutcomeText({ ...base, action: 'missing' })).toContain('人格')
  })

  it('后端加了新动作时原样念出来，不静默显示成已发出', () => {
    const text = probeOutcomeText({ ...base, action: 'someday' })
    expect(text).toContain('someday')
    expect(text).not.toContain('已发出')
  })
})
