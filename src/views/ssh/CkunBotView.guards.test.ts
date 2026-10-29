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
    expect(view).toContain('peerTag(c.external_id)')
    expect(view).toContain('来自 QQ · 仅供查看')
    // 没有选中会话时只能出空态/错误态：两个空态分支必须排在共用列表分支之前
    expect(view).toContain('v-else-if="store.isQq && !store.qqActiveId"')
    expect(view.indexOf('v-else-if="store.isQq && !store.qqActiveId"')).toBeLessThan(view.indexOf('v-for="d in messageList"'))
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
    // 思考块由「当前应答人格」的开关裁决：没勾思考的人格，历史块与流式块都不渲染
    // （后端已丢弃 reasoning，这里是不让旧数据/漏网数据漏出来的第二道闸）
    expect(view).toContain('thinkingOn && d.item.reasoning')
    expect(view).toContain('store.streamReasoning && thinkingOn')
    // 开关取的是正在说话的那一侧：QQ 记录没有思考块，也不拿本地选中人格冒充
    const gate = view.match(/const thinkingOn = computed\([\s\S]*?\)\)/)?.[0] || ''
    expect(gate).toContain('!store.isQq')
    expect(gate).toContain('store.activePersona?.enable_thinking')
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
    expect(view).toContain('personaName(qqActivePersona.value)')
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

  it('助手一轮按空行展开成多条气泡，用户原文不替他拆', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // 先展开再装饰：同轮的后续气泡走既有 cont 规则（隐头像、收间距），不新增样式
    expect(view).toContain('decorateMessages(expandBubbles(store.messages))')
    expect(view).toContain('decorateMessages(expandBubbles(store.qqMessages))')
    expect(view).toContain(':content="d.item.bubble"')
    expect(view).toContain('{{ d.item.bubble }}')
    // 附属信息一轮只挂第一条：图片、思考折叠不随气泡重复
    expect(view).toContain('d.item.images?.length && !d.item.bubbleIndex')
    expect(view).toContain("!d.item.bubbleIndex && d.item.role !== 'user'")
    // 可访问名字每条气泡都要有：meta 那行连发时不渲染，读屏不能丢掉说话人
    expect(view).toContain('<span class="sr-only">{{ speakerLabel(d.item) }}：</span>')
    // 流式阶段同规则分条，收尾换成落库消息时看不到气泡重排
    expect(view).toContain('splitBubbles(store.streamText)')
  })

  it('本地会话用户气泡有头像位：只认你的头像图，没设就透明空位', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('store.userAvatar')
    expect(view).toMatch(/\.bot-message\.user \.msg-avatar \{[^}]*order: 2/)
    // 头像位两边共用一个 span：本地你=store.userAvatar，QQ 对方=会话显示头像，bot=人格头像
    expect(view).toContain('<span class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="bubbleAvatar(d.item)" /></span>')
    const resolver = view.match(/function bubbleAvatar[\s\S]*?\n}/)?.[0] || ''
    expect(resolver).toContain('store.userAvatar')
    expect(resolver).toContain('display_avatar')
  })

  it('本地会话删除直接执行，不二次确认', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const fn = view.match(/async function removeConversation[\s\S]*?\n}/)?.[0] || ''
    expect(fn).toContain('store.deleteConversation(id)')
    expect(fn).not.toContain('confirm(')
    // 人格删除仍留确认：它连带动一批会话与记录，不在这次的口径里
    expect(view).toContain('删除人格「')
  })

  it('气泡基准取自 QQ 记录那套：bot 强调色、对方中性卡片，两个 tab 同一副', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // 基准 = 人格说的话用强调色（读的是它），人说的话走中性卡片 —— 两侧都按角色取色
    const assistant = view.match(/^\.message-content \{[^}]*\}/m)?.[0] || ''
    expect(assistant).toContain('color-mix(in srgb, var(--accent-primary) 12%, transparent)')
    expect(assistant).toContain('box-shadow: none')
    const user = view.match(/\.bot-message\.user \.message-content \{[^}]*\}/)?.[0] || ''
    expect(user).toContain('background: var(--bg-elevated)')
    expect(user).toContain('box-shadow: var(--shadow-sm)')
    // 开场白与 bot 气泡同色：它也是人格说的话
    expect(view.match(/^\.greeting \{[^}]*\}/m)?.[0]).toContain('color-mix(in srgb, var(--accent-primary) 12%, transparent)')
    // 尾巴朝自己这侧：bot 在左收左下、人在右收右下
    expect(assistant).toContain('border-radius: var(--bubble-radius) var(--bubble-radius) var(--bubble-radius) var(--radius-sm)')
    expect(user).toContain('border-radius: var(--bubble-radius) var(--bubble-radius) var(--radius-sm) var(--bubble-radius)')
  })

  it('本地与 QQ 记录共用一套气泡：配色按角色，meta 两边同规则', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // 一份循环、一份气泡模板：两个 tab 不可能再各长回去
    expect(view).toContain('v-for="d in messageList"')
    expect((view.match(/class="bot-message"/g) || []).length).toBe(1)
    // 渠道专属方向/配色覆盖全部退役：取色只按角色，两个 tab 同一条规则
    expect(view).not.toContain('.chat-main.qq')
    expect(view).not.toContain('{ qq: store.isQq }')
    // meta 不再是 QQ 专属：换人说话的第一条显示，连发时整条不渲染（不占空行）
    expect(view).toContain('<span v-if="!d.cont" class="msg-meta">{{ speakerLabel(d.item) }}')
    for (const gone of ['qq-message-body', 'qq-meta', 'qqRoleLabel']) {
      expect(view).not.toContain(gone)
    }
    // 对方侧也显示名字而不是「对方」代称：与本地同一口径（QQ 用会话显示名）
    const speaker = view.match(/function speakerLabel[\s\S]*?\n}/)?.[0] || ''
    expect(speaker).toContain('display_name')
  })

  it('QQ 记录与本地会话同向：人格在左、对方在右，切 tab 不换边', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // 方向只在通用规则里写一次，QQ 不再有自己的第二套
    expect(view).toMatch(/\.bot-message\.assistant \{ align-self: flex-start; \}/)
    expect(view).toMatch(/\.bot-message\.user \{ align-self: flex-end; \}/)
    expect(view).toMatch(/\.bot-message\.user \.msg-avatar \{[^}]*order: 2/)
    expect(view).toMatch(/\.bot-message\.user \.msg-body \{ align-items: flex-end; \}/)
  })

  it('QQ 会话行收成一行：名字 + 群标，代号与时间进悬浮提示', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const row = view.match(/<button class="conversation-main qq-single"[\s\S]*?<\/button>/)?.[0] || ''
    expect(row).toContain(':title="qqRowHint(c)"')
    expect(row).toContain('c.display_name || c.title')
    expect(row).toContain('peerTag(c.external_id)')
    // 第二段说明文字退役：250px 窄栏里必然折行，行高跟着文案长度漂；
    // 应答人格名右侧标题栏已经写了，代号只有排查价值，都挪进 title。
    expect(row).not.toContain('<small>')
    expect(view).not.toContain('qqPersonaLabel')
    const hint = view.match(/function qqRowHint[\s\S]*?\n}/)?.[0] || ''
    expect(hint).toContain('peerLabel(conversation.external_id)')
    expect(hint).toContain('formatLocalDateTime(conversation.updated_at)')
  })

  it('自定义显示弹窗：显示名并进头像那一栏，重置下沉到底栏', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const modal = view.match(/<BaseModal v-if="qqDisplayTarget"[\s\S]*?<\/BaseModal>/)?.[0] || ''
    // 字段由 #fields 插槽交给共享选择器排版，弹窗自己不再套一层 div
    expect(modal).toContain('<template #fields>')
    expect(modal).toContain(':clearable="false"')
    expect(modal).not.toContain('qq-display-modal')
    // 恢复默认 = 名称与头像一起置空，所以选择器自带的「清除」在这个弹窗里多余
    const actions = modal.match(/<template #actions>([\s\S]*?)<\/template>/)?.[0] || ''
    expect(actions.indexOf('恢复默认')).toBeLessThan(actions.indexOf('取消'))
    expect(view).toMatch(/\.qq-display-reset \{[^}]*margin-right: auto/)
  })
})
