/**
 * 首页任务台的标题行要不要画。
 *
 * 隐式建会话时本地标题就是首条消息的前 30 字摘要（S9-05），后端命名也常落在
 * 同一句话上。标题行随消息流滚动、就贴在首条气泡正上方 —— 同文时它提供不了
 * 任何定位信息，读起来却是「两条一模一样的用户消息」（首页"两个用户气泡"
 * 正是这么来的），因此同文时不画。
 */

/** 摘要标题带省略号后缀（'...' / '…'），比之前先去掉 */
const ELLIPSIS = /(\.{2,}|…)$/

export function isTitleEcho(title: string, firstUserContent: string): boolean {
  const t = title.trim().replace(ELLIPSIS, '').trim()
  const c = firstUserContent.trim()
  return t.length > 0 && c.startsWith(t)
}
