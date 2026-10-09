/**
 * S5-16 回归 —— 编辑器保存必须落回「显示的那一份」。
 *
 * loadFile 钉住载入时的 (path, side, sessionId)，save 若用**实时 props** 重推三元组，
 * props 在编辑器打开期间变化时就是「看到 A、写进 B」。今天这被全屏蒙皮挡住（切不动页），
 * 属于编辑器一旦改成非模态就立刻成立的潜伏缺口 —— 所以钉的是代码形状，不是当前可达性
 * （可达性测试要挂 monaco + electronAPI，代价远超这条守卫的价值）。
 */
// ?raw 由 vite/client 声明类型：本文件在 tsconfig.web 下检查，那里没有 node 类型，
// 读 node:fs 会让 typecheck 报错（同 src/styles/variables.test.ts 的口径）
import src from './SftpFileEditor.vue?raw'

import { describe, expect, it } from 'vitest'

function sliceFunction(header: string): string {
  const start = src.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = src.indexOf('\n}\n', start)
  expect(end).toBeGreaterThan(start)
  return src.slice(start, end)
}

describe('SftpFileEditor 保存落点（S5-16）', () => {
  const saveBody = sliceFunction('async function save(): Promise<void> {')

  it('save 不再从实时 props 推落点', () => {
    for (const leaked of ['props.path', 'props.side', 'props.sshSessionId']) {
      expect(saveBody, `save() 里不该出现 ${leaked}`).not.toContain(leaked)
    }
  })

  it('save 认载入时钉下的三元组', () => {
    expect(saveBody).toContain('loadedTarget')
    expect(saveBody).toContain('api.fsWriteFile(target.path, content.value)')
    expect(saveBody).toContain('api.sftpWriteFile(target.sessionId, target.path, content.value)')
  })

  it('没有成功载入过就不写任何东西', () => {
    const nullGuard = saveBody.indexOf('未写入任何内容')
    expect(nullGuard).toBeGreaterThan(-1)
    // 守卫必须在 saving.value = true 之前：否则一次拒绝会把保存按钮永久按住
    expect(nullGuard).toBeLessThan(saveBody.indexOf('saving.value = true'))
  })

  it('落点在新鲜度守卫之后才钉，且每次重新载入先作废', () => {
    const loadBody = sliceFunction('async function loadFile(): Promise<void> {')
    const fresh = loadBody.indexOf('if (!isFreshLoad(seq, path, side)) return')
    const pinned = loadBody.indexOf('loadedTarget = { path, side, sessionId }')
    expect(fresh).toBeGreaterThanOrEqual(0)
    expect(pinned).toBeGreaterThan(fresh)
    expect(loadBody).toContain('loadedTarget = null')
  })

  it('远端读取的会话守卫与请求用同一份 id', () => {
    const loadBody = sliceFunction('async function loadFile(): Promise<void> {')
    expect(loadBody).toContain("const sessionId = props.sshSessionId ?? ''")
    expect(loadBody).toContain('if (!sessionId) throw new Error')
    expect(loadBody).toContain('api.sftpReadFile(sessionId, path)')
    expect(loadBody).not.toContain('api.sftpReadFile(props.sshSessionId')
  })
})
