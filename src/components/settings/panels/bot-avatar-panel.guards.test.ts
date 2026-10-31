/**
 * 守卫：设置里不再有「cKunBot 头像」分类。
 *
 * 那段设置与陪聊页的聊天记录设置职责重叠 —— 一边是全局「你的头像 + 图库」，
 * 一边是每条会话显示名/头像的选择器，用户看到的是两个长得像的头像入口，
 * 反复被当成「第二个管机器人的地方」。已按裁决删掉设置侧，保留聊天记录侧。
 *
 * 头像能力本身没有消失：AvatarPicker（上传 / 从图库选）仍在人格编辑器与
 * 会话显示设置里；「你的头像」的历史值仍由 store 读出来给本地用户气泡用。
 */
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('设置里不再重复提供头像管理', () => {
  it('BotPanel、分类键、图标一并下线', () => {
    expect(existsSync(resolve(root, 'src/components/settings/panels/BotPanel.vue'))).toBe(false)
    // 类型层：SettingCategory 少一个 'bot'，PANELS 少一个 key（Record 漏 key 会编译报错）
    expect(read('src/stores/settings.ts')).not.toContain("| 'bot'")
    const dialog = read('src/components/common/SettingsDialog.vue')
    expect(dialog).not.toContain('BotPanel')
    expect(dialog).not.toContain('cKunBot 头像')
    // 图标没人用了就删，留着是下一轮「这图标给谁用的」的谜题
    expect(read('src/components/common/settings-nav-icons.ts')).not.toContain('IconBot')
  })

  it('头像入口留在陪聊页的聊天记录设置里', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('AvatarPicker')
    // 本地会话「你的头像」仍读 store，历史值不清空
    expect(view).toContain('store.userAvatar')
  })
})
