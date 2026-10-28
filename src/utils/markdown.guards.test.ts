/// <reference types="node" />
/**
 * markdown.ts 渲染工具守卫 —— 启动性能：highlight.js 严禁全量引入。
 *
 * 全量 `from 'highlight.js'` 会拖入 ~190 种语言（实测产物 useMarkdownDelegation
 * chunk 达 1.65MB），而它经 HomeWorkspace→(MessageContentRenderer/ThinkingBlock)→
 * MarkdownStream→markdown.ts 挂在首屏静态图上，dev 现编译与 prod 打包都被拖累。
 * 钉：只能用 highlight.js/lib/core + 精选语言 registerLanguage，行为（代码块高亮
 * + hljs-* 类 + ①已注册精确高亮 / ②标注但未注册按 plaintext / ③无标注 highlightAuto）不变。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const src = readFileSync(
  fileURLToPath(new URL('./markdown.ts', import.meta.url)),
  'utf8',
).replace(/\r\n/g, '\n')

describe('markdown.ts highlight.js 引入口径', () => {
  it('禁止全量引入 highlight.js（from/import()/require 三形态一网打尽，首屏包体红线）', () => {
    // 引号直接紧跟 `highlight.js` 即全量根入口；core/languages 子路径带 `/` 不会命中
    expect(src).not.toMatch(/['"]highlight\.js['"]/)
    // 白名单：除 /lib/core、/lib/languages/*、/styles/* 外的任何 highlight.js 入口（如
    // /lib/common 含 36 语言、/lib/index 全量）一律禁止——封死“保留逐个导入再加一句
    // /lib/common”的混合回归（那种情况语言计数仍 ≤30，靠下界偷不进）
    expect(src).not.toMatch(/['"]highlight\.js\/(?!lib\/(?:core|languages\/)|styles\/)[^'"]*['"]/)
  })

  it('必须走 highlight.js/lib/core', () => {
    expect(src).toContain('highlight.js/lib/core')
  })

  it('必须按需引入一组精选语言文件（有上下界，防重新滑回全量）', () => {
    // 真实信号是“load 了多少个语言文件”：循环注册还是逐个注册属实现细节。
    const langImports = src.match(/highlight\.js\/lib\/languages\//g) ?? []
    // 下界：覆盖运维高频集 bash/shell/powershell/python/json/yaml/ini/dockerfile/nginx/makefile/xml/http/go/sql/js/ts/diff/plaintext
    expect(langImports.length).toBeGreaterThanOrEqual(10)
    // 上界：精选集只该十余个，一旦有人往回堆到接近全量（~190）就报红，真正钉住包体红线
    expect(langImports.length).toBeLessThanOrEqual(30)
    // 且确实存在注册动作（形式不限：逐个或循环）
    expect(src).toContain('hljs.registerLanguage(')
  })

  it('保留 getLanguage 判定与 highlightAuto 回退（裁语言后行为不变）', () => {
    expect(src).toContain('hljs.getLanguage(')
    expect(src).toContain('hljs.highlightAuto(')
  })
})
