import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('设置 Bot 管理分类', () => {
  it('类型、图标、面板三处同一清单接线（漏一处编译/守卫就红）', () => {
    expect(read('src/stores/settings.ts')).toContain("| 'bot'")
    const dialog = read('src/components/common/SettingsDialog.vue')
    expect(dialog).toContain('Bot 管理')
    expect(dialog).toContain('bot:')
    expect(dialog).toContain('markRaw(BotPanel)')
    expect(read('src/components/common/settings-nav-icons.ts')).toContain('IconBot')
  })

  it('面板含「你的头像」区并接入 user-avatar 端点', () => {
    const panel = read('src/components/settings/panels/BotPanel.vue')
    expect(panel).toContain('你的头像')
    expect(panel).toContain('AvatarPicker')
    expect(panel).toContain('getUserAvatar')
    expect(panel).toContain('setUserAvatar')
    // 保存成功要同步 bot store，陪聊气泡不能等重启才换图
    expect(panel).toContain('useBotStore')
    expect(panel).toContain('loadUserAvatar')
  })

  it('图库管理区：网格展示、上传（可命名）、删除', () => {
    const panel = read('src/components/settings/panels/BotPanel.vue')
    for (const needle of ['头像图库', 'listGallery', 'addGalleryItem', 'deleteGalleryItem', 'gallery-grid', 'readAvatarFile']) {
      expect(panel).toContain(needle)
    }
  })
})
