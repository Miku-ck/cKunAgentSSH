/**
 * 审批卡的展示派生（纯函数）
 *
 * 从 AgentRunPanel 提下来：首页「待你放行」队列要显示同一行目标主机与风险标签，
 * 在组件里各抄一份就会和面板漂移（一边显示连接名、一边显示会话 id 这类）。
 */

/** 目标主机展示名 —— 后端只带 ssh_session_id，可读标签在前端按会话反查 */
export function approvalTargetLabel(
  req: { sshSessionId?: string },
  sessions: ReadonlyArray<{ id: string; name: string; connectionId?: string }>,
  connections: ReadonlyArray<{ id: string; name?: string; host: string; username: string }>,
): string {
  if (!req.sshSessionId) return ''
  const session = sessions.find(s => s.id === req.sshSessionId)
  const conn = session?.connectionId
    ? connections.find(c => c.id === session.connectionId)
    : undefined
  if (conn) return `${conn.name || conn.host}（${conn.username}@${conn.host}）`
  // 连接记录被删除或本地终端会话：退到会话名，再不行显示会话 id 片段（至少可辨识）
  return session?.name || `会话 ${req.sshSessionId.slice(0, 8)}`
}

/** 风险等级人读文案：英文 dangerous/moderate 对非专业用户没有信息量 */
export function approvalRiskLabel(risk: string): string {
  const map: Record<string, string> = { dangerous: '高危', moderate: '中风险', safe: '低风险' }
  return map[risk] || risk
}

/** 卡片上「授权命令」一行要显示的串：优先归一化形态（剥混淆、可比较），
 *  退回 payload 原文（写入/定时任务类工具显示的是实际落盘的模板）。 */
export function approvalCommand(req: { normalizedCommand?: string; command?: string }): string {
  return req.normalizedCommand || req.command || ''
}
