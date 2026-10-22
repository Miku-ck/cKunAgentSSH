/**
 * 审批卡的展示派生（纯函数）
 *
 * 从 AgentRunPanel 提下来：首页「待你放行」队列要显示同一行目标主机与风险标签，
 * 在组件里各抄一份就会和面板漂移（一边显示连接名、一边显示会话 id 这类）。
 */

/** 目标主机展示名 —— 后端只带 ssh_session_id，可读标签在前端按会话反查。
 *  emptyLabel：没有 ssh_session_id 时给什么。审批卡传「本机」——目标行整条消失
 *  会让人以为卡片漏了字段，而不是「这台机器上跑、没有远程目标」。 */
export function approvalTargetLabel(
  req: { sshSessionId?: string },
  sessions: ReadonlyArray<{ id: string; name: string; connectionId?: string }>,
  connections: ReadonlyArray<{ id: string; name?: string; host: string; username: string }>,
  emptyLabel = '',
): string {
  if (!req.sshSessionId) return emptyLabel
  const session = sessions.find(s => s.id === req.sshSessionId)
  const conn = session?.connectionId
    ? connections.find(c => c.id === session.connectionId)
    : undefined
  if (conn) return `${conn.name || conn.host}（${conn.username}@${conn.host}）`
  // 连接记录被删除或本地终端会话：退到会话名，再不行显示会话 id 片段（至少可辨识）
  return session?.name || `会话 ${req.sshSessionId.slice(0, 8)}`
}

/** 风险等级人读文案：英文 dangerous/moderate 对非专业用户没有信息量。
 *  认不出的值（含空/脏值）报「未知（按高危处理）」，而不是原样透出 ——
 *  授权档位对这枚值走的是 fail-closed（approval-scopes），卡片说辞必须同口径，
 *  否则用户读到一句看不懂的英文，以为是个不认识的正常档位就点了批准。 */
const RISK_LABELS: Record<string, string> = { dangerous: '高危', moderate: '中风险', safe: '低风险' }

export function approvalRiskLabel(risk: string): string {
  return RISK_LABELS[risk] || '未知（按高危处理）'
}

/** 风险徽章的配色类：只有已知档位有样式，其余统一落到高危底色 */
export function approvalRiskClass(risk: string): string {
  return RISK_LABELS[risk] ? `risk-${risk}` : 'risk-unknown'
}

/** 卡片上「授权命令」一行要显示的串：优先归一化形态（剥混淆、可比较），
 *  退回 payload 原文（定时任务类工具显示的是实际落盘的模板）。 */
export function approvalCommand(req: { normalizedCommand?: string; command?: string }): string {
  return req.normalizedCommand || req.command || ''
}

/**
 * 写入/编辑类工具的授权键绑定的是一条**分类占位命令**
 * （`printf '%s' 'BASE64_CONTENT_PAYLOAD' | base64 -d > path`）：
 * 后端刻意用占位符让键落在路径上，内容变了不重新审批。这条模板不是要执行的
 * 命令，展示给用户看只会误导（用户以为批准的是模板里的字面内容）。
 */
const SYNTHETIC_WRITE_MARKER = 'BASE64_CONTENT_PAYLOAD'

export interface ApprovalWritePreview {
  path: string
  content?: string
  oldText?: string
  newText?: string
  /** 预览被截断（完整内容仍在「参数」里） */
  truncated?: boolean
}

const PREVIEW_LIMIT = 2000

function clipPreview(text: string): { text: string; truncated: boolean } {
  if (text.length <= PREVIEW_LIMIT) return { text, truncated: false }
  return { text: text.slice(0, PREVIEW_LIMIT), truncated: true }
}

/**
 * 写入类审批卡的可读预览。返回 null 表示这张卡不是构造型写入 ——
 * 调用方照旧渲染「授权命令」行。
 */
export function approvalWritePreview(req: {
  tool?: string
  args?: Record<string, unknown>
  command?: string
  normalizedCommand?: string
}): ApprovalWritePreview | null {
  if (!approvalCommand(req).includes(SYNTHETIC_WRITE_MARKER)) return null
  const args = req.args || {}
  const path = typeof args.path === 'string' ? args.path : ''
  if (!path) return null

  const preview: ApprovalWritePreview = { path }
  const oldText = typeof args.old_string === 'string' ? args.old_string : undefined
  const newText = typeof args.new_string === 'string' ? args.new_string : undefined
  if (oldText !== undefined && newText !== undefined) {
    const oldClip = clipPreview(oldText)
    const newClip = clipPreview(newText)
    preview.oldText = oldClip.text
    preview.newText = newClip.text
    if (oldClip.truncated || newClip.truncated) preview.truncated = true
  } else if (typeof args.content === 'string') {
    const content = clipPreview(args.content)
    preview.content = content.text
    if (content.truncated) preview.truncated = true
  }
  return preview
}
