/**
 * 运维专家建议 chip 守卫：渲染 [切换并重跑][忽略]，非阻塞。
 * 证伪：删掉 chip 模板 → 红。
 *
 * HomeComposer 依赖大量 store/composable，全量挂载成本过高（brief 允许的退化
 * 路径）：与 HomeWorkspace.layout.test.ts 同款 ?raw 源码守卫，钉 chip 的模板
 * 契约、两个处理函数的关键通路、以及焦点态的淡 glow 规范。
 */
import { describe, expect, it } from 'vitest'
import rawComposer from './HomeComposer.vue?raw'

const composer = rawComposer.replace(/\r\n/g, '\n')
const template = composer.slice(composer.indexOf('<template>'), composer.indexOf('</template>'))
const script = composer.slice(composer.indexOf('<script setup'), composer.indexOf('</script>'))
const css = composer.slice(composer.indexOf('<style'))

/** 截取函数体（到第一个行首 } 为止，嵌套块都有缩进不会误截） */
function fnBody(name: string): string {
  const start = script.indexOf(`function ${name}`)
  expect(start, `没找到 ${name}`).toBeGreaterThan(-1)
  return script.slice(start, script.indexOf('\n}', start))
}

describe('HomeComposer 专家建议 chip', () => {
  it('有建议时渲染 chip：v-if 挂建议态，role=status 非阻塞，展示原因与专家名', () => {
    expect(template).toContain('v-if="agent.expertSuggestion"')
    const at = template.indexOf('class="hcc-suggest"')
    expect(at, '没找到 chip 容器').toBeGreaterThan(-1)
    const tag = template.slice(template.lastIndexOf('<div', at), template.indexOf('>', at))
    expect(tag).toContain('role="status"')
    expect(template).toContain('{{ agent.expertSuggestion.reason }}')
    expect(template).toContain('{{ agent.expertSuggestion.name }}')
  })

  it('chip 坐在输入卡片顶部（hcc 之内、textarea 之前），不挡输入', () => {
    const cardAt = template.indexOf('<div class="hcc">')
    const chipAt = template.indexOf('class="hcc-suggest"')
    const taAt = template.indexOf('<textarea')
    expect(cardAt).toBeGreaterThan(-1)
    expect(chipAt).toBeGreaterThan(cardAt)
    expect(chipAt).toBeLessThan(taAt)
  })

  it('渲染切换并重跑 / 忽略两个动作，各绑各的处理函数', () => {
    const chipStart = template.indexOf('class="hcc-suggest"')
    const chipEnd = template.indexOf('</div>', chipStart)
    const chip = template.slice(chipStart, chipEnd)
    const acceptAt = chip.indexOf('切换并重跑')
    expect(acceptAt, 'chip 里没有「切换并重跑」').toBeGreaterThan(-1)
    const acceptTag = chip.slice(chip.lastIndexOf('<button', acceptAt), acceptAt)
    expect(acceptTag).toContain('@click="acceptExpertSuggestion"')
    const ignoreAt = chip.indexOf('忽略')
    expect(ignoreAt, 'chip 里没有「忽略」').toBeGreaterThan(-1)
    const ignoreTag = chip.slice(chip.lastIndexOf('<button', ignoreAt), ignoreAt)
    expect(ignoreTag).toContain('@click="ignoreExpertSuggestion"')
  })
})

describe('acceptExpertSuggestion 处理函数', () => {
  it('按 slug 找专家 → 切选中 → 中止当前 run → 用建议携带的 task 重发', () => {
    const body = fnBody('acceptExpertSuggestion')
    expect(body).toContain('expertsStore.experts.find(e => e.slug === sug.slug)')
    expect(body).toContain('sshAi.setSelectedExpert(expert.id)')
    expect(body).toContain('agent.abort()')
    expect(body).toContain('await sshAi.sendMessage(task)')
    // 顺序契约：先中止旧 run 再重发，否则两条流并行（C10 同族）
    expect(body.indexOf('agent.abort()')).toBeLessThan(body.indexOf('sshAi.sendMessage'))
    // 点击即收 chip：无论专家找不找得到，建议态都不再挂着
    expect(body).toContain('agent.dismissExpertSuggestion()')
  })

  it('专家不存在时弹错并止步，不切选中也不重发', () => {
    const body = fnBody('acceptExpertSuggestion')
    const guardAt = body.indexOf('if (!expert)')
    expect(guardAt, '缺少专家不存在的守卫').toBeGreaterThan(-1)
    // 截到行首缩进的 }（模板串 ${…} 里的 } 不算块结尾）
    const guardBlock = body.slice(guardAt, body.indexOf('\n  }', guardAt))
    expect(guardBlock).toContain('toast.error(')
    expect(guardBlock).toContain('return')
    // 守卫必须在切换/中止/重发之前
    expect(guardAt).toBeLessThan(body.indexOf('sshAi.setSelectedExpert'))
  })
})

describe('ignoreExpertSuggestion 处理函数', () => {
  it('只清建议态，不打断正在跑的 run', () => {
    const body = fnBody('ignoreExpertSuggestion')
    expect(body).toContain('agent.dismissExpertSuggestion()')
    expect(body).not.toContain('abort(')
    expect(body).not.toContain('sendMessage(')
  })
})

describe('chip 样式（柔和提示条 + 淡 glow 焦点）', () => {
  it('.hcc-suggest 一族存在且只用主题令牌', () => {
    expect(css).toMatch(/\.hcc-suggest\s*\{/)
    expect(css).toMatch(/\.hcc-suggest-accept\b/)
    expect(css).toMatch(/\.hcc-suggest-ignore\b/)
    const at = css.indexOf('.hcc-suggest')
    const block = css.slice(at, css.indexOf('</style>'))
    expect(block).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
  })

  it('两枚按钮的焦点态是淡 glow，不是硬描边', () => {
    for (const cls of ['hcc-suggest-accept', 'hcc-suggest-ignore']) {
      const at = css.indexOf(`.${cls}:focus-visible`)
      expect(at, `缺少 .${cls}:focus-visible`).toBeGreaterThan(-1)
      const block = css.slice(at, css.indexOf('}', at))
      expect(block).toContain('box-shadow')
      expect(block).not.toMatch(/outline:\s*2px/)
    }
  })
})