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

  it('陪聊、人格、连接三个子页覆盖初版操作', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    for (const label of ['陪聊', '人格', '连接', '停止生成', '重试', '复制']) {
      expect(view).toContain(label)
    }
    expect(view).not.toContain('QQ 接入')
    expect(view).toContain('<ConnectionPage v-else :personas="store.personas" />')
  })

  it('人格头像只能上传图片，不再提供表情入口', () => {
    // 编辑表单已拆到 PersonaEditor：头像口径跟着组件走，页面里不该再出现第二份
    const editor = read('src/components/bot/PersonaEditor.vue')
    for (const needle of ['上传图片', 'PersonaAvatar', 'readAvatarFile']) {
      expect(editor).toContain(needle)
    }
    // 表情路径已全部移除：既不能留按钮/输入框，也不能留默认表情
    for (const gone of ['改用表情', 'avatar-emoji', '🤖']) {
      expect(editor).not.toContain(gone)
      expect(read('src/components/bot/CkunBotView.vue')).not.toContain(gone)
    }
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

  it('陪聊页按渠道切换，QQ 会话只读', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('role="tablist" aria-label="会话渠道"')
    expect(view).toContain("@click=\"store.selectChannel('qq')\"")
    expect(view).toContain('store.qqConversations')
    expect(view).toContain('store.qqActiveId')
    expect(view).toContain('channelLabel(c.external_id)')
    expect(view).toContain('来自 QQ · 仅供查看')
    // 没有选中会话时只能出空态/错误态，不能落进消息列表分支
    expect(view).toContain('v-else-if="store.isQq && !store.qqActiveId"')
    expect(view).toContain('v-else-if="store.isQq && store.qqActiveId"')
    // QQ 态不能出现可输入的胶囊；本地态才挂 composer
    expect(view).toMatch(/v-if="!store\.isQq && store\.activeConversationId" class="chat-composer"/)
    expect(view).not.toContain('QqRecords')
  })

  it('QQ 渠道每 5 秒刷新，贴底才自动滚动', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('store.refreshQq()')
    expect(view).toContain('const stick = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 48')
    expect(view).toMatch(/setInterval\(\(\) => \{ void refreshQqChat\(\) \}, 5000\)/)
    expect(view).toContain('stopQqPolling')
  })

  it('人格编辑器覆盖身份/说话方式/反应底线/关系四区', () => {
    const editor = read('src/components/bot/PersonaEditor.vue')
    for (const label of ['身份', '性格与背景', '说话方式', '反应与底线', '关系']) {
      expect(editor).toContain(label)
    }
    // 档位控件由服务端字典驱动：前端不许手抄选项文案，抄了必然和提示词漂移
    expect(editor).toContain('options?.[key].options')
    expect(editor).not.toContain('短句为主')
    // 保存必须回传活人感字段，否则后端收到的永远是默认档
    for (const field of ['expression_dna', 'reaction_style', 'refusal_style', 'address_user', 'relationship_speed']) {
      expect(editor).toContain(field)
    }
    // 底线不可编辑：只做告知，不给输入控件
    expect(editor).toContain('不可编辑')
  })

  it('陪聊页展示关系阶段，气泡走 MarkdownStream、思考可折叠', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('store.activeRelation')
    expect(view).toContain('relation-bar')
    expect(view).toContain('<PersonaEditor')
    expect(view).toContain(':options="personaOptions"')
    // 异步引入（Markdown 栈不进首屏包）+ 折叠思考块
    expect(view).toContain('defineAsyncComponent')
    expect(view).toContain('m.reasoning')
    expect(view).toContain('store.streamReasoning')
  })

  it('本地会话支持附图发送与图片气泡（Phase 2）', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    for (const needle of ['readChatImage', 'MAX_CHAT_IMAGES', 'pendingImages', 'composer-attach', 'pickImages']) {
      expect(view).toContain(needle)
    }
    expect(view).toContain('m.images')
    expect(view).toContain('msg-image')
    expect(view).toContain('store.sendMessage(text, imgs)')
  })
})
