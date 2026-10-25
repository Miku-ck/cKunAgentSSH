/**
 * 守卫：MarkdownStream 的 content 从非空清空时必须同步清掉 renderedHtml 与
 * 稳定前缀缓存。消息重置/撤销走的就是这条路 —— watch 早退会让组件实例里
 * 残留上一条消息的 HTML，复用实例（未按 msg.id 重挂载）的调用方即见残影。
 * 项目无组件级测试设施（vitest 为 node 环境、无挂载依赖），与
 * TermSearchBar.accent.guards 等一致，用 ?raw 源码守卫钉住这段分支。
 */
import { describe, expect, it } from 'vitest'
import source from './MarkdownStream.vue?raw'

const watchBody = source.match(/\nwatch\(\(\) => props\.content,[\s\S]*?\n\}\)/)?.[0] ?? ''

describe('MarkdownStream 内容清空不残留', () => {
  it('content 变空的分支同步清空渲染结果与稳定前缀缓存', () => {
    expect(watchBody).toContain("v === ''")
    expect(watchBody).toContain("renderedHtml.value = ''")
    expect(watchBody).toContain("_cachedStable = ''")
    expect(watchBody).toContain("_cachedStableHtml = ''")
  })

  it('清空分支重置后直接 return，只有非空内容才排渲染', () => {
    const resetIdx = watchBody.indexOf("_cachedStableHtml = ''")
    const returnIdx = watchBody.indexOf('return', resetIdx)
    const scheduleIdx = watchBody.indexOf('scheduleRender()', returnIdx)
    expect(resetIdx).toBeGreaterThan(-1)
    expect(returnIdx).toBeGreaterThan(resetIdx)
    expect(scheduleIdx).toBeGreaterThan(returnIdx)
  })
})
