/**
 * StreamingThinkScrubber 单测 — 流式 think 块擦洗状态机
 *
 * 覆盖：完整块擦洗、跨 delta 切分（半截开/闭标签）、块边界规则、
 * 多块、flush 语义（块内丢弃防 CoT 泄漏、块外标签前缀丢弃）、标签变体。
 */
import { describe, it, expect } from 'vitest'
import { StreamingThinkScrubber } from './think-scrubber'

function run(chunks: string[]): string {
  const s = new StreamingThinkScrubber()
  let out = ''
  for (const c of chunks) out += s.feed(c)
  out += s.flush()
  return out
}

describe('StreamingThinkScrubber', () => {
  it('擦洗单个完整 think 块，保留块外内容', () => {
    expect(run(['A\n<think>\nB\n</think>\nC\nD'])).toBe('A\nC\nD')
  })

  it('块外内容原样输出', () => {
    expect(run(['hello', ' ', 'world'])).toBe('hello world')
  })

  it('开标签必须在行首/串首（block-boundary），行中同形文本不视为标签', () => {
    expect(run(['some <think> inline text', 'more'])).toBe('some <think> inline textmore')
  })

  it('跨 delta 切分：开标签被拆开', () => {
    // '<th' 与 'ink>\nB' 分属两个 delta，拼接后构成完整开标签；
    // 块内 B 被擦洗，流未闭合 → 整体丢弃
    expect(run(['A\n', '<th', 'ink>\nB'])).toBe('A\n')
  })

  it('跨 delta 切分：闭合标签被拆开', () => {
    // '</thi' 与 'nk>\n' 分属两个 delta；块内 x/B 被擦洗，块外 D 保留
    expect(run(['A\n<think>\nx\n', 'B\n', '</thi', 'nk>\n', 'D'])).toBe('A\nD')
  })

  it('流结束时仍在块内 → 丢弃剩余（不泄漏 partial CoT）', () => {
    // 块内正文与标签前缀无关，不得滞留缓存；流结束时块未闭合 → 整段丢弃
    expect(run(['A\n', '<th', 'ink>\nsecret more'])).toBe('A\n')
  })

  it('块外结尾的半截开标签在 flush 时丢弃', () => {
    expect(run(['hello ', '<t'])).toBe('hello ')
  })

  it('多个连续块分别擦洗', () => {
    expect(run(['A\n<think>\ns1\n</think>\n', 'B\n', '<think>\n', 's2\n</think>\n', 'C'])).toBe('A\nB\nC')
  })

  it('空输入 → 空输出', () => {
    const s = new StreamingThinkScrubber()
    expect(s.feed('')).toBe('')
    expect(s.flush()).toBe('')
  })

  it('reset 后重新可用', () => {
    const s = new StreamingThinkScrubber()
    s.feed('A\nx')
    s.reset()
    expect(s.feed('B') + s.flush()).toBe('B')
  })

  it('不同标签变体（thinking/reasoning/thought）均可识别', () => {
    expect(run(['A\n<thinking>\nB\n</thinking>\nC'])).toBe('A\nC')
    expect(run(['A\n<reasoning>\nB\n</reasoning>\nC'])).toBe('A\nC')
    expect(run(['A\n<thought>\nB\n</thought>\nC'])).toBe('A\nC')
  })

  it('跨 delta 切断的行中标签不算块首，正文一字不丢', () => {
    // "…答案 3<th" + "inking>不是 CoT"：标签重组后落在 pending[0]，
    // 原实现的 `idx > 0` 前置条件让它直接放行 → 误入 think 块，
    // 后面的真正文被擦洗并由 flush() 丢弃（答案悄悄变短、状态仍是"已完成"）
    expect(run(['答案是 3<th', 'inking>不是 CoT'])).toBe('答案是 3<thinking>不是 CoT')
    expect(run(['x<', 'reasoning>y'])).toBe('x<reasoning>y')
  })

  it('跨 delta 切断的行首标签仍进入块内', () => {
    // 与上一例成对：邻居是换行时必须照旧擦洗，否则就是 CoT 泄漏
    expect(run(['前文\n<th', 'ink>秘密\n', '</think>后文'])).toBe('前文\n后文')
  })
})
