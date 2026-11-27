/**
 * 「意图先行」三 chip（plan / spec / goal）的纯逻辑（UI 无关）。
 *
 * 红线：意图声明是 prompt 注入，不是执行姿态 —— 本模块只产出用户口吻的
 * 句子与文本推导，不涉及任何下发参数；服务端推导与批准门零改动。
 * 意图句是唯一事实源：发送时拼接（useHomeCommand）与渲染层 badge 推导
 * （MessageItem）都从这里取，两边漂移会让 badge 撒谎。
 */

export type IntentKind = 'plan' | 'spec' | 'goal'

/** 每种意图对应的一句用户口吻请求：发送时拼进消息文本。改措辞必须两侧
 * （拼接与推导）同源本文件 —— 措辞变了，历史消息的 badge 推导随之变化，
 * 这是刻意的（badge 表达的是"这句话确实提出了什么请求"）。 */
export const INTENT_SENTENCES: Record<IntentKind, string> = {
  plan: '请先提出执行计划，经我确认后再逐步执行。',
  spec: '请先把这条需求整理成明确的任务清单与执行步骤，经我确认后再动手。',
  goal: '请先提出目标与成功标准，经我确认后再规划执行。',
}

/** 拼接顺序：goal 在前（验收锚先于步骤），与 ＋ 菜单 Spec/Plan/Goal 顺序无关
 * —— 这里排的是"句子在消息里的语义顺序"。 */
const ORDER: IntentKind[] = ['goal', 'plan', 'spec']

/** 按固定顺序拼接已激活意图的句子（叠加时每句独立一行，紧跟任务文本；
 *  空激活返回空串 —— 调用方直接 text + suffix 即可）。 */
export function buildIntentSuffix(active: IntentKind[]): string {
  const parts = ORDER.filter(k => active.includes(k)).map(k => INTENT_SENTENCES[k])
  return parts.length ? '\n\n' + parts.join('\n') : ''
}

/** 从消息文本推导携带的意图（渲染层 badge 用）：以句子完整出现为准。
 *  用户自己打出同样的话也显示 —— 语义上他确实提出了同样的请求。 */
export function extractIntents(text: string): IntentKind[] {
  return ORDER.filter(k => text.includes(INTENT_SENTENCES[k]))
}
