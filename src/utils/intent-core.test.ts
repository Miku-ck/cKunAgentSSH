/**
 * intent-core 纯函数守卫：意图句唯一事实源 + 拼接/推导两侧同源。
 * badge 撒谎 = 渲染层推导与发送时拼接用了两份措辞 —— 本文件钉死同源。
 */
import { describe, expect, it } from 'vitest'
import {
  INTENT_SENTENCES,
  buildIntentSuffix,
  extractIntents,
  type IntentKind,
} from './intent-core'

describe('buildIntentSuffix', () => {
  it('空激活返回空串：text + suffix 恒等于原文', () => {
    expect(buildIntentSuffix([])).toBe('')
  })

  it('单一意图：两句空行 + 意图句紧跟任务文本', () => {
    const s = buildIntentSuffix(['goal'])
    expect(s).toBe('\n\n' + INTENT_SENTENCES.goal)
  })

  it('叠加时按固定顺序（goal→plan→spec），每句独立一行', () => {
    const s = buildIntentSuffix(['spec', 'goal', 'plan'])
    expect(s).toBe(
      '\n\n' + INTENT_SENTENCES.goal + '\n' + INTENT_SENTENCES.plan + '\n' + INTENT_SENTENCES.spec,
    )
  })

  it('三条意图句都不含「验收」—— 验收是 Goal（criteria）的专属词汇', () => {
    for (const sentence of Object.values(INTENT_SENTENCES)) {
      expect(sentence).not.toContain('验收')
    }
  })
})

describe('extractIntents', () => {
  const kinds: IntentKind[] = ['plan', 'spec', 'goal']

  it('与 buildIntentSuffix 同源：拼进去的都能推导出来', () => {
    const text = '清理临时垃圾' + buildIntentSuffix(['goal', 'plan'])
    expect(extractIntents(text)).toEqual(['goal', 'plan'])
  })

  it('无意图的普通消息返回空数组', () => {
    expect(extractIntents('清理临时垃圾')).toEqual([])
  })

  it('三条句子互不为彼此的子串（推导不误伤）', () => {
    for (const a of kinds) {
      for (const b of kinds) {
        if (a === b) continue
        expect(INTENT_SENTENCES[a].includes(INTENT_SENTENCES[b])).toBe(false)
      }
    }
  })
})
