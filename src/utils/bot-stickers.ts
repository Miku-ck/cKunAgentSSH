import { getBaseUrl } from '@/api/base'

/**
 * 表情包标记：与后端 bot/stickers.py 同一套语法。
 * 模型只输出独占一条的 [表情:开心]，本地把它渲染成内置 png；
 * QQ/微信由后端把同一条标记换成真图或颜文字兜底。
 */
const STICKER_RE = /^\[表情[:：]\s*([^[\]\s]{1,8})\s*\]$/

/** 整条只有一个标记时返回表情键；带别的字或不是标记返回 null。 */
export function stickerKey(text: string): string | null {
  const match = STICKER_RE.exec((text || '').trim())
  return match ? match[1] : null
}

/**
 * 去掉正文里的标记与流式过程中还没写完的半个标记。
 * 只用于「显示层」：落库文本保留标记，历史消息才能继续渲染成图片。
 */
export function stripStickerTokens(text: string): string {
  return (text || '')
    .replace(/\[表情[:：][^[\]\s]{1,8}\]/g, '')
    // 流式会先吐出 `[表` / `[表情:` 这类半截标记：还没收尾就先把残片藏掉
    .replace(/\[表[^\n\]]*$/, '')
    .trim()
}

/** 内置表情图片直链：/assets 是公开路由，不带 /api 前缀，也带不了鉴权头。 */
export function stickerAssetUrl(key: string): string {
  try {
    return `${getBaseUrl().replace(/\/api\/?$/, '')}/assets/stickers/${encodeURIComponent(key)}`
  } catch {
    // 后端还没就绪：先给空串，<img> 自己走失败态，别让渲染抛错
    return ''
  }
}
