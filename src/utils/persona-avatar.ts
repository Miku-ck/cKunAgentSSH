const IMAGE_DATA_URL = /^data:image\/(?:png|jpe?g|webp|gif);base64,/

/** 头像只接受上传图片压缩后的 data URL；表情/文本不再当头像（与后端校验同口径）。 */
export function isImageAvatar(value?: string | null): boolean {
  return !!value && IMAGE_DATA_URL.test(value)
}

export const AVATAR_IMAGE_SIDE = 256
const MAX_FILE_BYTES = 8 * 1024 * 1024
const MAX_DATA_URL_LENGTH = 350_000

/** 裁成方形并压到 256px webp：44px 的展示位在 2x 屏上也够清晰，data URL ~20k 字符。 */
export async function readAvatarFile(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) throw new Error('请选择图片文件')
  if (file.size > MAX_FILE_BYTES) throw new Error('图片太大，请选择 8MB 以内的图片')
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    throw new Error('这张图片打不开，请换一张')
  }
  try {
    const side = Math.min(bitmap.width, bitmap.height)
    if (!side) throw new Error('这张图片没有有效尺寸')
    const canvas = document.createElement('canvas')
    canvas.width = AVATAR_IMAGE_SIDE
    canvas.height = AVATAR_IMAGE_SIDE
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('当前环境无法处理图片')
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(
      bitmap,
      (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side,
      0, 0, AVATAR_IMAGE_SIDE, AVATAR_IMAGE_SIDE,
    )
    const url = canvas.toDataURL('image/webp', 0.85)
    if (!isImageAvatar(url)) throw new Error('图片编码失败，请换一张')
    if (url.length > MAX_DATA_URL_LENGTH) throw new Error('图片细节太多，请换一张更简单的')
    return url
  } finally {
    bitmap.close()
  }
}
