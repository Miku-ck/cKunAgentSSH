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

  it('人格头像走统一选择器，表情路径已全移除', () => {
    // 编辑表单已拆到 PersonaEditor：头像逻辑收进 AvatarPicker 后，编辑器不再自持压缩/文件输入
    const editor = read('src/components/bot/PersonaEditor.vue')
    expect(editor).toContain('AvatarPicker')
    expect(editor).not.toContain('readAvatarFile')
    expect(editor).not.toContain('avatar-file')
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
    expect(view).toContain('peerLabel(c.external_id)')
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

  it('人格编辑是整页不是弹窗，覆盖身份到进阶玩法八区', () => {
    const editor = read('src/components/bot/PersonaEditor.vue')
    for (const label of ['身份', '性格特征', '背景设定', '说话风格', '喜好雷区', '记忆设定', '互动示例', '进阶玩法']) {
      expect(editor).toContain(label)
    }
    // 整页：没有模态外壳，左目录 + 长卷，取消/保存与 cKunBot 同一行（顶部，不是底部条）
    expect(editor).not.toContain('BaseModal')
    expect(editor).toContain('editor-rail')
    expect(editor).toContain('data-section')
    expect(editor).not.toContain('editor-foot')
    const head = editor.match(/<header class="editor-head">[\s\S]*?<\/header>/)?.[0] || ''
    expect(head).toContain('取消')
    expect(head).toContain('保存')
    // 品牌标记由页面下发到这一行，子组件不再自己写一份 cKunBot
    expect(read('src/components/bot/CkunBotView.vue')).toContain('<template #brand>')
    // 档位控件由服务端字典驱动：前端不许手抄选项文案，抄了必然和提示词漂移
    expect(editor).toContain('options?.[key].options')
    expect(editor).not.toContain('短句为主')
    // 单一 textarea 的区不重复挂字段标签：区标题已说明它是什么，
    // 可访问名字由 aria-labelledby 指回标题，不留无名控件。
    for (const gone of ['它是个什么样的人', '喜欢什么、什么一踩就炸', '该记住什么、该避讳什么', '几组问答，给它一个说话的样板', '可以玩什么、不玩什么']) {
      expect(editor).not.toContain(gone)
    }
    expect(editor).not.toContain('<span>它住在哪')
    expect(editor).toContain('aria-labelledby="persona-personality"')
    expect(editor).toContain('aria-labelledby="persona-background"')
    // 底线固定生效，只在 placeholder 里说一句，不再单开区或加告知段
    expect(editor).not.toContain('不可编辑')
    // 保存必须回传全部字段，否则后端收到的永远是默认档
    for (const field of [
      'expression_dna', 'reaction_style', 'refusal_style', 'address_user', 'relationship_speed',
      'likes_landmines', 'memory_directive', 'dialogue_samples', 'advanced_play',
    ]) {
      expect(editor).toContain(field)
    }
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

  it('本地会话撤掉上传入口，历史图片气泡仍可看', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // 「暂时删除」只针对输入区的发图入口：已落库的图还要能显示，
    // store/后端的 images 能力保留，恢复时只加回前端这一小块。
    for (const gone of ['readChatImage', 'MAX_CHAT_IMAGES', 'pendingImages', 'composer-attach', 'pickImages', 'store.sendMessage(text, imgs)']) {
      expect(view).not.toContain(gone)
    }
    expect(view).toContain('m.images')
    expect(view).toContain('msg-image')
  })

  it('发送按钮在胶囊里上下居中，QQ 只读提示是无框细字贴底', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const composer = view.match(/\.chat-composer \{[^}]*\}/)?.[0] || ''
    expect(composer).toContain('align-items: center')
    const bar = view.match(/\.readonly-bar \{[^}]*\}/)?.[0] || ''
    expect(bar).toContain('text-align: center')
    expect(bar).toContain('font-size: 0.7692rem')
    expect(bar).not.toContain('border')
    expect(bar).not.toContain('background')
  })

  it('人格卡片尺寸固定：性格截两行、开场白一行且始终占位', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const personality = view.match(/\.personality \{[^}]*\}/)?.[0] || ''
    expect(personality).toContain('-webkit-line-clamp: 2')
    expect(personality).toContain('min-height: 3.4em')
    // 没填开场白也占住那一行，否则卡片高度会随开场白有无而变
    expect(view).not.toContain('v-if="p.greeting"')
    const greeting = view.match(/\.persona-greeting \{[^}]*\}/)?.[0] || ''
    expect(greeting).toContain('min-height: 1.6em')
    expect(greeting).toContain('text-overflow: ellipsis')
  })

  it('会话只消费服务端下发的 persona 三字段，不在前端解析快照 JSON', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const api = read('src/api/bot.ts')
    // 服务端已把快照裁成 {id, name, avatar}：快照字段不该再出现在契约或视图里
    expect(api).toContain('persona: { id: string; name: string; avatar: string }')
    expect(api).not.toContain('persona_snapshot')
    expect(view).not.toContain('persona_snapshot')
    expect(view).toContain('personaName(conversation.persona)')
  })

  it('QQ 会话显示覆盖：优先 display_name/display_avatar，空则回落默认', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('c.display_name || c.title')
    expect(view).toContain('c.display_avatar')
    expect(view).toContain('updateQqDisplay')
    expect(view).toContain('自定义显示')
    // 入口在 QQ 会话行的 ⋯ 上（本地行不吃这个覆盖），弹窗绑点进来的那一行而不是活动会话
    expect(view).toMatch(/class="icon-button conv-menu"[\s\S]{0,160}?openQqDisplay\(c\)/)
    expect(view).toContain('<BaseModal v-if="qqDisplayTarget" title="自定义显示"')
    expect(view).toContain('store.updateQqDisplay(conversation.id')
    // 标题栏下的展开面板退役：本项目偏好居中弹窗，不做抽屉式交互
    expect(view).not.toContain('qq-display-editor')
    expect(view).not.toContain('qq-display-toggle')
    // ⋯ 默认隐藏，进该行或键盘聚焦才淡入；触屏无 hover 时常显
    expect(view).toMatch(/\.conv-menu \{[^}]*opacity: 0/)
    expect(view).toMatch(/\.conversation:hover \.conv-menu, \.conversation:focus-within \.conv-menu \{ opacity: 1/)
    expect(view).toContain('@media (hover: none) { .conv-menu { opacity: 1; } }')
    const store = read('src/stores/bot.ts')
    expect(store).toContain('updateConversationDisplay')
  })

  it('本地会话用户气泡有头像位：你的头像图 > 淡色「你」占位', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('store.userAvatar')
    expect(view).toMatch(/\.bot-message\.user \.msg-avatar \{[^}]*order: 2/)
    // QQ 是只读记录，用户侧不发言 → 头像位只出现在 local 分支（v-else 紧邻 assistant 头像）
    expect(view).toContain('<span v-else class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="store.userAvatar" seed="ckun-user" label="你" /></span>')
  })
})
