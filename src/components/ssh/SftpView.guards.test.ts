/**
 * SFTP 上传入口收敛守卫
 *
 * 没有组件测试环境（未装 @vue/test-utils/jsdom），这里钉的是代码形状：
 * 上传只允许有一个入口 —— doTransfer 内的 store.uploadFiles，且必须经过
 * 冲突确认/重命名计划；右键「上传」不得再直调 store.uploadFiles 绕过确认。
 *
 * 纯逻辑（冲突计划、记录恢复）由各自单测覆盖。
 */
import sftpViewRaw from './SftpView.vue?raw'
import { describe, expect, it } from 'vitest'

const src = sftpViewRaw.replace(/\r\n/g, '\n')

function sliceUntil(start: string, end: string): string {
  const from = src.indexOf(start)
  expect(from, `找不到 ${start}`).toBeGreaterThanOrEqual(0)
  const to = src.indexOf(end, from + start.length)
  expect(to, `找不到 ${start} 之后的 ${end}`).toBeGreaterThan(from)
  return src.slice(from, to)
}

describe('上传入口唯一', () => {
  it('store.uploadFiles 在页面里只出现一次', () => {
    const hits = src.match(/store\.uploadFiles\(/g) ?? []
    expect(hits.length, '出现多处说明又有了绕过确认的第二条上传入口').toBe(1)
  })

  it('那次调用落在 doTransfer 内（冲突确认之后）', () => {
    const doTransfer = sliceUntil('async function doTransfer(', '// ─── 右键菜单')
    expect(doTransfer).toContain('store.uploadFiles(')
    // 调用必须在冲突决策之后：取消/全跳过都在它前面 return
    expect(doTransfer.indexOf('await askConflicts(items)')).toBeLessThan(
      doTransfer.indexOf('store.uploadFiles('),
    )
    // 发生冲突时使用重命名计划后的 paths，而不是原始 paths
    expect(doTransfer).toContain('store.uploadFiles(paths, undefined, renameMap)')
  })

  it('右键「传输/上传」走 onTransfer，与拖拽共用确认入口', () => {
    const ctxAction = sliceUntil('async function handleCtxAction(', 'if (action === \'copy\'')
    expect(ctxAction).toContain("action === 'transfer'")
    expect(ctxAction).toContain('await onTransfer({ side, paths })')
    expect(ctxAction, '右键分支不得直调上传/下载').not.toMatch(/store\.(uploadFiles|downloadFiles)\(/)
  })
})
