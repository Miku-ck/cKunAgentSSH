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

describe('同侧复制入口收敛（S5-21）', () => {
  it('store.localCopy / remoteCopy 在页面里只被 doTransfer 调用', () => {
    // 同侧复制曾由右键「粘贴」直调 store.localCopy/remoteCopy，绕过冲突确认 ——
    // 同名项被静默覆盖。现在两条路径都必须经 doTransfer 的 askConflicts。
    const copyCalls = src.match(/store\.(localCopy|remoteCopy)\(/g) ?? []
    expect(copyCalls.length, '同侧复制只允许出现在 doTransfer 的 copy 分支').toBe(2)
    const doTransfer = sliceUntil('async function doTransfer(', '// ─── 右键菜单')
    for (const call of copyCalls) {
      expect(doTransfer).toContain(call)
    }
  })

  it('粘贴分支走 onTransfer({ copy: true })，不直调 store 复制', () => {
    const paste = src.slice(src.indexOf("if (action === 'paste')"))
    const pasteBody = paste.slice(0, paste.indexOf("if (action === 'newFolder')"))
    expect(pasteBody).toContain('await onTransfer({ side, paths: cb.paths, targetDir, copy: true })')
    expect(pasteBody, '粘贴不得直调 localCopy/remoteCopy').not.toMatch(/store\.(localCopy|remoteCopy)\(/)
  })

  it('copy 分支的复制调用发生在冲突决策之后，且带 renameMap', () => {
    const doTransfer = sliceUntil('async function doTransfer(', '// ─── 右键菜单')
    const copyAt = doTransfer.indexOf('if (payload.copy)')
    expect(copyAt).toBeGreaterThanOrEqual(0)
    // 冲突决策必须在 copy 分支之前：先 askConflicts → 拿到 renameMap → 才派发复制
    expect(doTransfer.indexOf('await askConflicts(items)')).toBeLessThan(copyAt)
    const copyBranch = doTransfer.slice(copyAt)
    expect(copyBranch).toContain('store.localCopy(paths, payload.targetDir!, renameMap)')
    expect(copyBranch).toContain('store.remoteCopy(paths, payload.targetDir!, renameMap)')
    // 决策产出的 renameMap 必须真的落到复制入参，而不是丢弃
    expect(doTransfer).toContain('renameMap = Object.keys(plan.renameMap).length ? plan.renameMap : undefined')
  })
})
