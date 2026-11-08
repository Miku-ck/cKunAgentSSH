/**
 * 「异步动作 + 进度/成功/失败 toast」的统一编排。
 *
 * 改造前 SFTP 的 4 个 ZIP 动作与 DockerView 的 5 个容器/镜像动作各自手抄一份
 * try/catch + toast 模板，文案与兜底口径散落。这里只固定**顺序**，所有文案仍由
 * 调用点传入，不改变任何一条提示文字与触发时机：
 *   pending(info) → action → success(success/info) → afterSuccess → 返回
 *   catch → onError(err) 或 error(failure(err)) → 可选 rethrow
 */
export interface ActionToast {
  info(message: string): void
  success(message: string): void
  error(message: string): void
}

interface ActionToastOptions<T> {
  toast: ActionToast
  action: () => Promise<T>
  /** 动作开始前的信息提示；缺省则不发（DockerView 的动作没有前置提示） */
  pending?: string
  success: string
  /** 默认 success；停止容器等中性动作用 info */
  successLevel?: 'success' | 'info'
  /** 错误文案或文案构造器；onError 存在时不使用 */
  failure?: string | ((err: unknown) => string)
  /** 调用方自行决定错误呈现（如 DockerView 的 toastErr） */
  onError?: (err: unknown) => void
  /** 成功提示之后执行的刷新；失败路径不执行 */
  afterSuccess?: () => Promise<void>
  /** 默认 false；SFTP 的 ZIP 动作沿用原逻辑把错误抛给上层 */
  rethrow?: boolean
}

export async function runWithActionToast<T>(o: ActionToastOptions<T>): Promise<T | undefined> {
  if (o.pending !== undefined) o.toast.info(o.pending)
  try {
    const result = await o.action()
    if (o.successLevel === 'info') o.toast.info(o.success)
    else o.toast.success(o.success)
    if (o.afterSuccess) await o.afterSuccess()
    return result
  } catch (err) {
    if (o.onError) o.onError(err)
    else {
      const message = typeof o.failure === 'function' ? o.failure(err) : o.failure
      o.toast.error(message ?? '操作失败，请稍后重试')
    }
    if (o.rethrow) throw err
    return undefined
  }
}
