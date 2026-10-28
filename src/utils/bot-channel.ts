export function channelLabel(externalId: string | null | undefined): string {
  if (externalId?.startsWith('group:')) return '群聊'
  if (externalId?.startsWith('c2c:')) return '私聊'
  return 'QQ'
}

export function personaNameFromSnapshot(snapshot: string | null | undefined): string {
  if (!snapshot) return ''
  try {
    const parsed = JSON.parse(snapshot) as { name?: unknown }
    return typeof parsed.name === 'string' ? parsed.name : ''
  } catch {
    return ''
  }
}
