const IMAGE_DATA_URL = /^data:image\/(?:png|jpe?g|webp|gif);base64,/

export const MAX_CHAT_IMAGES = 4
const MAX_EDGE = 1280
const MAX_DATA_URL_LENGTH = 1_500_000

/** 与后端 _AVATAR_DATA_URL 同口径：只认上传图片压缩后的 data URL。 */
export function isChatImage(dataUrl: string): boolean {
  return IMAGE_DATA_URL.test(dataUrl)
}

/**
 * 等比缩放到长边 ≤1280 的 webp data URL，供 vision 模型看图。
 * 与头像口径不同：不做方形裁切，保留原图宽高比，避免裁掉关键内容。
 */
export async function readChatImage(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) throw new Error('请选择图片文件')
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    throw new Error('这张图片打不开，请换一张')
  }
  try {
    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height))
    const w = Math.max(1, Math.round(bitmap.width * scale))
    const h = Math.max(1, Math.round(bitmap.height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = w
    canvas.height = h
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('当前环境无法处理图片')
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(bitmap, 0, 0, w, h)
    const url = canvas.toDataURL('image/webp', 0.85)
    if (!isChatImage(url)) throw new Error('图片编码失败，请换一张')
    if (url.length > MAX_DATA_URL_LENGTH) throw new Error('图片太大，请换一张更小或更简单的')
    return url
  } finally {
    bitmap.close()
  }
}
