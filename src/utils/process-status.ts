/**
 * 内联执行过程的纯映射：工具行的命令摘要。
 *
 * 组件只渲染这些结果，不在模板里重排分支 —— 摘要规则写在一个可测函数里，
 * 历史回放与实时运行读的是同一张表。
 *
 * 终态文案与工具行状态原先也在这里（processStatusLabel / toolState），但它们
 * 与 agent-run-status.ts 的 runStatusLabel 是两套并行实现，实际只有后者被调用，
 * 前者连同其类型（ProcessStatus / ProcessTone / ToolStateEntry / ProcessMeta）
 * 一并删除，避免再长出第二份会漂移的文案表。
 */

/** 工具行那行命令：命令执行类直接给 command，其余给参数摘要
 *  （整包 JSON 会让一行记录变成一屏噪声） */
export function headline(e: { tool: string; args?: Record<string, unknown> }): string {
  const cmd = typeof e.args?.command === 'string' ? e.args.command : ''
  if (cmd) return cmd
  const keys = Object.keys(e.args || {})
  if (!keys.length) return e.tool
  const preview = keys.slice(0, 3).map(k => {
    const v = String(e.args?.[k])
    // 截断必须留痕：不给省略号，用户会把半截参数当全文
    return `${k}=${v.length > 40 ? `${v.slice(0, 40)}…` : v}`
  }).join(' ')
  return `${e.tool}(${preview})`
}
