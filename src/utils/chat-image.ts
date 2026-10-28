/**
 * 聊天附图（Phase 2 看图回复）：与后端 BotChatRequest.images 同闸门 ——
 * 最多 4 张、单张 data URL ≤ 1_500_000 字符、只收上传图片。
 * 与头像（readAvatarFile 方形 256）不同口径：这里保比例缩边，不裁方。
 */
export const MAX_CHAT_IMAGES = 4

const IMAGE_DATA_URL = /^data:image\/(?:png|jpe?g|webp|gif);base64,/
const MAX_DATA_URL_LENGTH = 1_500_000
const MAX_FILE_BYTES = 15 * 1024 * 1024
const MAX_SIDE = 1600

export async function readChatImage(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) throw new Error('请选择图片文件')
  if (file.size > MAX_FILE_BYTES) throw new Error('图片太大，请选择 15MB 以内的图片')
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    throw new Error('这张图片打不开，请换一张')
  }
  try {
    const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height))
    const width = Math.max(1, Math.round(bitmap.width * scale))
    const height = Math.max(1, Math.round(bitmap.height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('当前环境无法处理图片')
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(bitmap, 0, 0, width, height)
    // 逐档降质量压到后端闸门以内：先保观感，压不下去再报错让人换图
    for (const quality of [0.85, 0.7, 0.55, 0.4]) {
      const url = canvas.toDataURL('image/webp', quality)
      if (IMAGE_DATA_URL.test(url) && url.length <= MAX_DATA_URL_LENGTH) return url
    }
    throw new Error('图片细节太多，请换一张更小或更简单的')
  } finally {
    bitmap.close()
  }
}
