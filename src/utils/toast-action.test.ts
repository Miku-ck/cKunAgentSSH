/**
 * runWithActionToast 顺序与异常路径守卫。
 *
 * 抽取前 SFTP 的 4 个 ZIP 动作与 DockerView 的 5 个动作各自手写 try/catch，
 * 这里把「提示顺序 / 刷新只在成功后 / 错误文案 / rethrow / onError 优先」钉死，
 * 保证收敛成单点后行为逐字不变。
 */
import { describe, expect, it, vi } from 'vitest'
import { runWithActionToast, type ActionToast } from './toast-action'

function recorder() {
  const calls: string[] = []
  const toast: ActionToast = {
    info: (m) => calls.push('info:' + m),
    success: (m) => calls.push('success:' + m),
    error: (m) => calls.push('error:' + m),
  }
  return { toast, calls }
}

describe('runWithActionToast', () => {
  it('成功路径顺序：pending → action → success → afterSuccess，并返回 action 结果', async () => {
    const { toast, calls } = recorder()
    const afterSuccess = vi.fn(async () => { calls.push('refresh') })
    const result = await runWithActionToast({
      toast,
      action: async () => { calls.push('action'); return 42 },
      pending: '正在压缩…',
      success: '已压缩',
      afterSuccess,
    })
    expect(calls).toEqual(['info:正在压缩…', 'action', 'success:已压缩', 'refresh'])
    expect(result).toBe(42)
    expect(afterSuccess).toHaveBeenCalledTimes(1)
  })

  it('successLevel=info 走 info 而非 success（容器停止动作）', async () => {
    const { toast, calls } = recorder()
    await runWithActionToast({ toast, action: async () => {}, success: '已停止 nginx', successLevel: 'info' })
    expect(calls).toEqual(['info:已停止 nginx'])
  })

  it('失败路径用 failure(err) 构造文案，且不再执行 afterSuccess；rethrow 时抛原错误', async () => {
    const { toast, calls } = recorder()
    const err = new Error('disk full')
    const afterSuccess = vi.fn(async () => {})
    await expect(
      runWithActionToast({
        toast,
        action: async () => { throw err },
        success: '已压缩',
        failure: (e) => '压缩失败:' + (e as Error).message,
        afterSuccess,
        rethrow: true,
      }),
    ).rejects.toBe(err)
    expect(calls).toEqual(['error:压缩失败:disk full'])
    expect(afterSuccess).not.toHaveBeenCalled()
  })

  it('onError 存在时优先，不再发 toast.error；默认不重抛', async () => {
    const { toast, calls } = recorder()
    const onError = vi.fn()
    const result = await runWithActionToast({
      toast,
      action: async () => { throw new Error('boom') },
      success: '已启动',
      onError,
    })
    expect(onError).toHaveBeenCalledTimes(1)
    expect(calls).toEqual([])
    expect(result).toBeUndefined()
  })

  it('afterSuccess 在 try 内：刷新本身失败也会走错误路径（与原内联模板一致）', async () => {
    const { toast, calls } = recorder()
    const err = new Error('refresh failed')
    await expect(
      runWithActionToast({
        toast,
        action: async () => {},
        success: '已压缩',
        failure: (e) => '压缩失败:' + (e as Error).message,
        afterSuccess: async () => { throw err },
        rethrow: true,
      }),
    ).rejects.toBe(err)
    expect(calls).toEqual(['success:已压缩', 'error:压缩失败:refresh failed'])
  })
})
