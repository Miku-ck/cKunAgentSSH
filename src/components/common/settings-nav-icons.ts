import { defineComponent, h, markRaw } from 'vue'
import type { Component, VNode } from 'vue'

/**
 * 图标一律用渲染函数，不用 `template` 字符串。
 *
 * 渲染层打包的是 Vue runtime-only（无模板编译器）：`{ template }` 组件在 Electron
 * 里只会打一条 "runtime compilation is not supported" 警告，然后什么都不渲染。
 * SSR 测试跑在 node 的完整构建上会替模板编译，所以只断言"渲染出 svg"挡不住回归 ——
 * 同名测试还直接锁了组件对象必须带 render、不带 template。
 */
const ICON_ATTRS = {
  viewBox: '0 0 24 24',
  width: 16,
  height: 16,
  fill: 'none',
  stroke: 'currentColor',
  'stroke-width': 1.5,
  'stroke-linecap': 'round',
  'stroke-linejoin': 'round',
}

function svgIcon(name: string, draw: () => VNode[]): Component {
  return markRaw(defineComponent({
    name,
    render: () => h('svg', ICON_ATTRS, draw()),
  }))
}

const line = (x1: number, y1: number, x2: number, y2: number) => h('line', { x1, y1, x2, y2 })
const circle = (cx: number, cy: number, r: number) => h('circle', { cx, cy, r })
const path = (d: string) => h('path', { d })

export const IconModels = svgIcon('IconModels', () => [
  h('rect', { x: 2, y: 3, width: 20, height: 14, rx: 2 }),
  line(8, 21, 16, 21),
  line(12, 17, 12, 21),
])

export const IconChat = svgIcon('IconChat', () => [
  path('M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'),
])

export const IconAppearance = svgIcon('IconAppearance', () => [
  circle(12, 12, 5),
  line(12, 1, 12, 3),
  line(12, 21, 12, 23),
  line(4.22, 4.22, 5.64, 5.64),
  line(18.36, 18.36, 19.78, 19.78),
  line(1, 12, 3, 12),
  line(21, 12, 23, 12),
  line(4.22, 19.78, 5.64, 18.36),
  line(18.36, 5.64, 19.78, 4.22),
])

export const IconAgent = svgIcon('IconAgent', () => [
  path('M12 2v4'),
  h('rect', { x: 5, y: 6, width: 14, height: 10, rx: 3 }),
  circle(9.5, 11, 1),
  circle(14.5, 11, 1),
  path('M9 20v-4M15 20v-4M3 10v2M21 10v2'),
])

export const IconSkills = svgIcon('IconSkills', () => [
  path('M12 3l1.9 4.1 4.6.6-3.4 3.2.9 4.6L12 13.9 7.9 15.5l.9-4.6L5.5 7.7l4.6-.6z'),
  path('M6 19h12'),
])

export const IconMemory = svgIcon('IconMemory', () => [
  path('M9 3a3 3 0 0 0-3 3v1a3 3 0 0 0-3 3v2a3 3 0 0 0 3 3v1a3 3 0 0 0 3 3h1a3 3 0 0 0 3-3V6a3 3 0 0 0-3-3H9z'),
  path('M15 3a3 3 0 0 1 3 3v1a3 3 0 0 1 3 3v2a3 3 0 0 1-3 3v1a3 3 0 0 1-3 3h-1a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3h1z'),
])

export const IconOps = svgIcon('IconOps', () => [
  circle(12, 12, 3),
  path('M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z'),
])

/** 「关于与更新」：圆圈里一个 i（信息＋可点击的更新入口） */
export const IconAbout = svgIcon('IconAbout', () => [
  circle(12, 12, 9),
  line(12, 11, 12, 16),
  circle(12, 8, 0.6),
])
