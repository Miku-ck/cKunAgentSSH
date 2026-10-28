import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('cKunBot 页面注册与关键交互', () => {
  it('顶部导航、应用事件和工作区页面使用同一个 bot 页键', () => {
    expect(read('src/composables/useSshNavigation.ts')).toContain("const toggleBot = (): void => togglePage('bot')")
    expect(read('src/layouts/TitleBar.vue')).toContain("activePage === 'bot'")
    expect(read('src/App.vue')).toContain('@action-bot="sshViewRef?.toggleBot()"')
    expect(read('src/views/ssh/SshView.vue')).toContain("v-show=\"activeNavPage === 'bot'\"")
  })

  it('陪聊、人格、QQ 三个子页覆盖初版操作', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    for (const label of ['陪聊', '人格', 'QQ 接入', '停止生成', '重试', '复制', '保存并连接', '断开']) {
      expect(view).toContain(label)
    }
    expect(view).not.toContain('app_secret: config.app_secret')
  })

  it('人格头像只能上传图片，不再提供表情入口', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    for (const needle of ['上传图片', 'PersonaAvatar', 'readAvatarFile']) {
      expect(view).toContain(needle)
    }
    // 表情路径已全部移除：既不能留按钮/输入框，也不能留默认表情
    expect(view).not.toContain('改用表情')
    expect(view).not.toContain('avatar-emoji')
    expect(view).not.toContain('🤖')
  })

  it('胶囊输入区的焦点环由整条胶囊画，且是淡光晕', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // 和首页输入卡同一条：3px / 16% 主色，实心 2px 会让打字时的框显得很重
    expect(view).toMatch(/\.chat-composer:focus-within \{[^}]*box-shadow: 0 0 0 3px color-mix\(in srgb, var\(--accent-primary\) 16%, transparent\)/)
    expect(view).not.toMatch(/\.chat-composer:focus-within \{[^}]*box-shadow: 0 0 0 2px var\(--accent-primary\)/)
    // 全局底线已不再圈 textarea，原先那条 outline:none 豁免随之退役
    expect(view).not.toContain('.composer-input:focus-visible')
  })

  it('会话时间按本地时区显示，不再直切 UTC 字符串', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('formatLocalDateTime(c.updated_at)')
    expect(view).not.toContain('updated_at.slice(')
  })

  it('QQ 页挂载只读记录区', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain("import QqRecords from './QqRecords.vue'")
    expect(view).toContain('<QqRecords :personas="store.personas" />')
  })
})
