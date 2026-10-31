/**
 * 设置导航图标必须是渲染函数组件。
 *
 * 根因：渲染层打包的是 Vue runtime-only（不含模板编译器），`{ template: '<svg…>' }`
 * 组件在 Electron 里只发一条 console warn，然后什么都不渲染 —— 而 SSR 跑在 node
 * 的完整构建上，会替模板编译，所以"渲染出了 svg"本身证明不了 runtime-only 下可用。
 * 因此除了渲染断言，还必须直接锁住：组件对象不带 template、带 render。
 */
import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import type { Component } from 'vue'
import { renderToString } from 'vue/server-renderer'
import * as icons from './settings-nav-icons'

const entries = Object.entries(icons) as [string, Component][]

describe('设置导航图标', () => {
  it('八个图标全部是渲染函数组件，不依赖运行时模板编译', () => {
    expect(entries).toHaveLength(8)
    for (const [name, icon] of entries) {
      expect(name).toMatch(/^Icon/)
      const options = icon as { template?: unknown; render?: unknown }
      expect(options.template, `${name} 不能依赖 template`).toBeUndefined()
      expect(typeof options.render, `${name} 必须有 render`).toBe('function')
    }
  })

  it('SSR 渲染出 svg 根节点，class 能落到根上', async () => {
    for (const [, icon] of entries) {
      const html = await renderToString(createSSRApp({ render: () => h(icon, { class: 'nav-icon' }) }))
      expect(html).toMatch(/^<svg/)
      expect(html).toContain('class="nav-icon"')
      // SSR 按 HTML 规则序列化时会把属性名小写，大小写不敏感地断言
      expect(html).toMatch(/viewBox="0 0 24 24"/i)
    }
  })
})
