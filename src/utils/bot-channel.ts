export function channelLabel(externalId: string | null | undefined): string {
  if (externalId?.startsWith('group:')) return '群聊'
  if (externalId?.startsWith('c2c:')) return '私聊'
  return 'QQ'
}

/**
 * 会话归属人格的名字。
 * 服务端已把快照裁成 {id, name, avatar} 直接下发，这里不再解析 JSON；
 * 人格被删除后 persona_id 为空，名字仍来自快照，所以比查 personas 列表更可靠。
 */
export function personaName(persona: { name?: string } | null | undefined): string {
  return persona?.name?.trim() || ''
}
