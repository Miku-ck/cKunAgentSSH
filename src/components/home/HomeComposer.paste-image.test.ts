/**
 * 图片粘贴 + 能力预警的静态契约守卫（?raw 风格，与 HomeComposer.plus-menu.test.ts 同类）
 *
 * 钉住粘贴链路的三层友好提示：粘贴拦截 → 能力预警 toast → 失败兜底 toast。
 * 任何一层被误删（重构剪切粘贴时常见）都直接红。
 */
import { describe, expect, it } from 'vitest'
import rawComposer from './HomeComposer.vue?raw'

const composer = rawComposer.replace(/\r\n/g, '\n')

describe('输入框图片粘贴链路', () => {
  it('textarea 挂了 @paste 且处理函数拦截剪贴板图片项', () => {
    expect(composer).toContain('@paste="onPaste"')
    expect(composer).toContain("item.kind === 'file' && item.type.startsWith('image/')")
    // 文本粘贴不受影响：无图片项时不 preventDefault 直接放行
    const onPaste = composer.slice(
      composer.indexOf('function onPaste'),
      composer.indexOf('const stamp'),
    )
    expect(onPaste).toContain('if (!images.length) return')
    expect(onPaste.indexOf('e.preventDefault()'))
      .toBeGreaterThan(onPaste.indexOf('if (!images.length) return'))
  })

  it('图片附件在输入框内显示缩略图预览条（不在底部 chip 排，用户 2026-11-15 指定）', () => {
    // 预览条坐 textarea 之前（输入框内部），图片是 objectURL 缩略图
    const stripAt = composer.indexOf('class="hcc-attachments"')
    const taAt = composer.indexOf('<textarea')
    expect(stripAt).toBeGreaterThan(-1)
    expect(taAt).toBeGreaterThan(stripAt)
    expect(composer).toContain('hcc-att-thumb')
    expect(composer).toContain("sshAi.attachmentPreviews[f.file_id]")
    // 底部 chip 行只挂文本类附件（pendingTextAtts），不再有图片缩略图
    expect(composer).toContain('v-for="f in pendingTextAtts"')
    expect(composer).not.toContain('hcc-chip-thumb')
    // 无预览时回退 🖼（预览只在 store 内存里，不跨刷新存在）
    expect(composer).toContain('🖼')
  })

  it('不做基于 supports_image 的事前预警（用户 2026-11-15：模型实际支持时预警是噪音）', () => {
    // 能力标记是种子猜的、经常过期 —— 模型识别成功却弹「可能无法识别」
    // 纯属误报。事前预警已整体移除，唯一反馈点是真失败时的错误提示
    // （见下方 agent store 兜底测试）。
    expect(composer).not.toContain('未标记支持视觉输入')
    expect(composer).not.toContain('supports_image')
    // 粘贴主链路不受影响
    expect(composer).toContain('attachFiles(named)')
  })

  it('多格式支持：映射表覆盖 svg/ico/tiff/avif/heic/heif，表外类型按子类型推导而非猜 png', () => {
    for (const mime of ['image/svg+xml', 'image/x-icon', 'image/tiff',
      'image/avif', 'image/heic', 'image/heif', 'image/jpg']) {
      expect(composer).toContain(`'${mime}':`)
    }
    // 兜底：IMAGE_EXT_BY_MIME 表外不再回退 'png'（把 avif 命名成 .png 会骗过
    // 后端校验），必须走 extFromImageMime 从 MIME 子类型推导
    expect(composer).not.toMatch(/\|\| 'png'/)
    expect(composer).toContain('extFromImageMime(f.type)')
    expect(composer).toContain('function extFromImageMime')
  })
})

describe('视频附件链路', () => {
  it('媒体附件分区呈现：图片/视频进输入框内预览条（🎬/🖼），title 注明能力边界', () => {
    // 图片/视频在输入框内预览条（2026-11-15 用户指定，不在底部 chip 排）
    expect(composer).toContain('🎬')
    expect(composer).toContain('hcc-att-icon')
    expect(composer).toContain('ffprobe/shell 分析元数据')
    expect(composer).toContain(`f.kind === 'video'`)
  })

  it('发送分拣三路：图片/视频走引用字段，正文只留标记行', async () => {
    const rawStore = (await import('../../stores/ssh-ai.ts?raw')).default.replace(/\r\n/g, '\n')
    expect(rawStore).toContain(`a.kind === 'video'`)
    expect(rawStore).toContain('[视频: ')
    expect(rawStore).toContain('videos: videoRefs')
  })

  it('缩略图 objectURL 生命周期收口在 setPendingAttachments（泄漏守卫）', async () => {
    const rawStore = (await import('../../stores/ssh-ai.ts?raw')).default.replace(/\r\n/g, '\n')
    // 生成：uploadFiles 循环内对 image 附件建 objectURL
    expect(rawStore).toContain('URL.createObjectURL(file)')
    // 回收：revoke 只出现在 setPendingAttachments 一处（单一收口，两个调用方免费获得）
    expect(rawStore).toContain('URL.revokeObjectURL')
    const revokeCount = rawStore.split('URL.revokeObjectURL').length - 1
    expect(revokeCount).toBe(1)
    // 摘除与换会话清空都走 setPendingAttachments，而不是绕开它直接赋值
    expect(rawStore).toContain('setPendingAttachments(pendingAttachments.value.filter')
    expect(rawStore).toContain('setPendingAttachments([])')
  })
})

describe('agent store 的视觉失败兜底', async () => {
  it('onError 对多模态拒绝错误弹独立 toast（失败信息不淹没在面板里）', async () => {
    const rawAgent = (await import('../../stores/agent.ts?raw')).default.replace(/\r\n/g, '\n')
    expect(rawAgent).toContain('视觉多模态|图片输入|image_url')
    expect(rawAgent).toContain('当前模型可能不支持图片输入')
  })
})
