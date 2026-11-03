import { describe, expect, it } from 'vitest'
import { resolve } from 'node:path'
// 谓词与配置同源：写在 electron.vite.config.ts 里，测试直接 import 那一份，
// 避免出现「测试里的白名单」和「dev server 用的白名单」两张嘴。
import { rendererWatchIgnored } from '../electron.vite.config'

const ROOT = resolve(__dirname, '..')

/** 由仓库内相对路径拼出 chokidar 实际喂给回调的绝对路径（Windows 上是反斜杠） */
const p = (rel: string): string => resolve(ROOT, ...rel.split('/'))
/** 同一绝对路径的 posix 写法：Vite 内部某些分支会先 normalize 再回调 */
const posix = (rel: string): string => p(rel).replace(/\\/g, '/')

describe('dev server 文件监听白名单', () => {
  it('渲染层模块图所在的目录必须继续被监听', () => {
    // 边界事实（已核实）：src 越出自身的 import 只到 shared/，入口是 index.html 里的 /src/main.ts
    for (const rel of [
      'src/App.vue',
      'src/stores/sftp.ts',
      'src/styles/variables.css',
      'shared/port-core.ts',
    ]) {
      expect(rendererWatchIgnored(p(rel)), rel).toBe(false)
    }
  })

  it('根级文件必须继续被监听：index.html 与配置文件改了要能触发 reload/重启', () => {
    for (const rel of ['index.html', 'electron.vite.config.ts', 'tsconfig.node.json']) {
      expect(rendererWatchIgnored(p(rel)), rel).toBe(false)
    }
  })

  it('非源码的根级目录一律不扫 —— 启动慢就是因为它们被整棵走了', () => {
    for (const rel of [
      'runtime/Lib/asyncio/__init__.py',
      '.mimosa/hook-state/blob/thing.json',
      'out/renderer/index.html',
      'backend/main.py',
      'docs/README.md',
      'designs/whatever.png',
      // node_modules 由 Vite 自己默认忽略（见下），这里同时断言本谓词不反对它 ——
      // 追加语义下两边判同一边，谁都不会因为对方的存在而放开。
      'node_modules/vue/dist/vue.runtime.esm-bundler.js',
    ]) {
      expect(rendererWatchIgnored(p(rel)), rel).toBe(true)
    }
  })

  it('仓库之外的路径不归本白名单管', () => {
    // 返回 false = 不表态，交给 Vite 自己的规则（monorepo/软链依赖要能被正常 watch）
    expect(rendererWatchIgnored('C:/Users/ckun/elsewhere/pkg/index.ts')).toBe(false)
  })

  it('同一目录的反斜杠与 posix 写法判定一致', () => {
    expect(rendererWatchIgnored(posix('.mimosa/hook-state'))).toBe(rendererWatchIgnored(p('.mimosa/hook-state')))
    expect(rendererWatchIgnored(posix('src/App.vue'))).toBe(false)
    expect(rendererWatchIgnored(ROOT)).toBe(false)
  })
})
