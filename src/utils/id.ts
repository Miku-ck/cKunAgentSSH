/** 生成轻量唯一 ID（无需加密强度，用于会话/消息本地标识） */
export function genId(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36)
}
