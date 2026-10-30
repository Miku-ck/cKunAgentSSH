/**
 * exec 退出码三态判据（shared/exec-outcome-core.ts）：主进程 ssh-manager 的打包收尾
 * 与渲染层 Docker save/load 共用一份，两面对「结果未知」的处理方向不同（一边保留
 * 产物、一边原样回包），判据漂开就会出现「同一件事一边报失败一边报未知」，所以钉死。
 */
import { describe, expect, it } from 'vitest'
import { classifyExit, isOutputTruncated, EXEC_TRUNCATED_NOTE } from '../shared/exec-outcome-core'

describe('classifyExit', () => {
  it('0 是 ok；null 是不知道；其余非零才是失败', () => {
    expect(classifyExit(0)).toBe('ok')
    expect(classifyExit(null)).toBe('unknown')
    expect(classifyExit(1)).toBe('failed')
    expect(classifyExit(127)).toBe('failed')
  })

  // null 与 0 在 JS 里同为 falsy：任何 `if (!exitCode)` 改写都会把「没回退出码」
  // 静默并入成功，而这条判据的全部意义就在于二者不能混。
  it('null 不落进与 0 相同的分支', () => {
    expect(classifyExit(null)).not.toBe(classifyExit(0))
  })

  it('未知态不是失败态（调用方据此决定保留还是删除产物）', () => {
    expect(classifyExit(null)).not.toBe('failed')
  })
})

describe('isOutputTruncated', () => {
  it('stdout / stderr 命中截断标记都算', () => {
    expect(isOutputTruncated('rows\n[输出已截断，超过 1MB 上限]')).toBe(true)
    expect(isOutputTruncated('', 'err\n[输出已截断，超过 1MB 上限]')).toBe(true)
  })
  it('主进程现行文案也命中（全等 includes 曾因改文案永久失配）', () => {
    // 旧实现按整句全等匹配，主进程把「1MB 上限」改成「1M 字符上限」后，Docker 页签的
    // 「输出已截断」提示条从此不再出现，而用例是自造输入喂判据函数，测不出跨层断裂。
    expect(isOutputTruncated('rows\n[输出已截断，超过 1M 字符上限]')).toBe(true)
  })
  it('主进程实际追加的那句串，判据必须认（两侧同一份字面量，漂移的根子已拔）', () => {
    expect(isOutputTruncated(`x\n${EXEC_TRUNCATED_NOTE}`)).toBe(true)
    expect(isOutputTruncated('', `\n${EXEC_TRUNCATED_NOTE}`)).toBe(true)
  })
  it('正常输出不算', () => {
    expect(isOutputTruncated('sha256:abc\tnginx\tlatest')).toBe(false)
    expect(isOutputTruncated('')).toBe(false)
  })
})
