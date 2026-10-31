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

  it('陪聊页按渠道切换：本地、QQ、微信，远程会话只读', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('role="tablist" aria-label="会话渠道"')
    expect(view).toContain("@click=\"store.selectChannel('qq')\"")
    expect(view).toContain("@click=\"store.selectChannel('wechat')\"")
    expect(view).toContain('store.remoteConversations')
    expect(view).toContain('store.remoteActiveId')
    expect(view).toContain('v-for="g in remoteGroups"')
    expect(view).toContain('来自 {{ store.remoteChannelLabel }}')
    expect(view).toContain('仅供查看，回复请在{{ store.remoteChannelLabel }}里进行')
    // 没有选中会话时只能出空态/错误态：两个空态分支必须排在共用列表分支之前
    expect(view).toContain('v-else-if="store.isRemote && !store.remoteActiveId"')
    expect(view.indexOf('v-else-if="store.isRemote && !store.remoteActiveId"')).toBeLessThan(view.indexOf('v-for="d in messageList"'))
    // 远程态不能出现可输入的胶囊；本地态才挂 composer
    expect(view).toMatch(/v-if="!store\.isRemote && store\.activeConversationId" class="chat-composer"/)
    expect(view).not.toContain('QqRecords')
  })

  it('远程会话可直接删除：与本地会话同一口径，不弹确认', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const start = view.indexOf('v-for="c in g.items"')
    const end = view.indexOf('v-if="store.remoteError"')
    expect(start).toBeGreaterThan(-1)
    expect(end).toBeGreaterThan(start)
    const rows = view.slice(start, end)
    expect(rows).toContain('@click="removeRemote(c.id)"')
    expect(rows).toContain('title="删除会话"')
    const fn = view.match(/async function removeRemote[\s\S]*?\n}/)?.[0] || ''
    expect(fn).toContain('store.deleteRemoteConversation(id)')
    expect(fn).not.toContain('confirm(')
  })

  it('QQ 与微信渠道每 5 秒刷新，贴底才自动滚动', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('store.refreshRemote()')
    // 贴底判定复用任务台的共享件（src/utils/scroll-follow.ts），不再手写差值
    expect(view).toContain('shouldFollowScroll(el.scrollTop, el.clientHeight, el.scrollHeight, 48)')
    // 四处滚动触发共用一份 scrollToBottom，裸的 scrollTop 赋值只允许在它里面出现一次
    expect(view.match(/function scrollToBottom\(\)/)?.length).toBe(1)
    expect((view.match(/scrollTop = \S+\.scrollHeight/g) || []).length).toBe(1)
    expect(view).toMatch(/setInterval\(\(\) => \{ void refreshRemoteChat\(\) \}, 5000\)/)
    expect(view).toContain('stopRemotePolling')
  })

  it('流式跟到底也要贴底判定：不许每个 token 抢滚动条', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const streamWatch = view.match(
      /watch\(\(\) => \[store\.messages\.length, store\.streamText\][\s\S]{0,400}?\n\}\)/,
    )
    expect(streamWatch, '找不到流式滚动 watcher').not.toBeNull()
    expect(streamWatch![0]).toContain('shouldFollowScroll')
    expect(streamWatch![0]).toMatch(/if \(stick\) scrollToBottom\(\)/)
    // 逐条揭开（P4 排程）同理：揭开一条不该把上翻看记录的人拽回底部
    const stagedWatch = view.match(
      /watch\(\(\) => staged\.value\?\.shown[\s\S]{0,400}?\n\}\)/,
    )
    expect(stagedWatch, '找不到逐条揭开 watcher').not.toBeNull()
    expect(stagedWatch![0]).toContain('shouldFollowScroll')
  })

  it('打字占位与正文行等高：气泡开场不弹一下', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // .message-content 的行高是 1.7，占位点低于它就会在首 token 落地时跳
    expect(view).toMatch(/\.typing-dots \{[^}]*min-height: 1\.7em/)
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
      'group_mode', 'mention_sender',
      'likes_landmines', 'memory_directive', 'dialogue_samples', 'advanced_play',
    ]) {
      expect(editor).toContain(field)
    }
  })

  it('只读侧的「试一句」：不等窗口真发一句，结果只落回显条', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const store = read('src/stores/bot.ts')
    const api = read('src/api/bot.ts')
    expect(api).toContain("request<ProactiveProbeResult>('/bot/proactive/probe'")
    expect(view).toContain('试一句')
    expect(view).toContain('store.probeProactive(store.remoteActiveId)')
    expect(view).toContain(':disabled="store.probing"')
    // 微信没有主动发送通道：按钮只能在 QQ 上挂（后端会当场拒）
    expect(view).toMatch(/v-if="store\.channel === 'qq'"[\s\S]{0,200}?store\.probeProactive/)
    expect(view).toContain('<span v-if="store.probeNote" class="probe-note">{{ store.probeNote }}</span>')
    // 回显是会话级的：换会话/换渠道都得清空，否则旧群的原因顶在新群头上
    expect(store).toContain('probeNote.value = \'\'')
    expect(store.match(/probeNote\.value = ''/g)?.length).toBeGreaterThanOrEqual(2)
    // 探口不往消息列表里写东西：群主动那句本来就不落本地库，伪造一条就是假记录
    expect(store).not.toMatch(/probeProactive[\s\S]{0,600}?remoteMessages\.value\.push/)
  })

  it('群回复不 @ 人：人格开关默认开，旧记录缺字段也照旧 @', () => {
    const editor = read('src/components/bot/PersonaEditor.vue')
    expect(editor).toContain('回复 @ 提问者')
    // `=== 1` 会把缺字段的旧人格读成关掉：升级等于悄悄换了一批人的群回复通道
    expect(editor).toContain('source?.mention_sender !== 0')
    expect(editor).not.toContain('mention_sender: source?.mention_sender === 1')
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
    expect(gate).toContain('!store.isRemote')
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
    expect(view).toContain('personaName(remoteActivePersona.value)')
  })

  it('远端会话显示覆盖：QQ 与微信同一入口，优先 display_name/display_avatar', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('c.display_name || c.title')
    expect(view).toContain('c.display_avatar')
    expect(view).toContain('updateConversationDisplay')
    expect(view).toContain('自定义显示')
    // 入口在远端会话行的 ⋯ 上；渠道不设门槛，本地行没这个按钮
    const menu = view.match(/class="icon-button conv-menu"([\s\S]{0,200}?)openDisplay\(c\)/)?.[1] || ''
    expect(menu, '找不到 ⋯ 入口').not.toBe('')
    expect(menu).not.toContain('store.channel')
    expect(view).toContain('<BaseModal v-if="displayTarget" title="自定义显示"')
    expect(view).toContain('store.updateConversationDisplay(conversation.id')
    // 标题栏下的展开面板退役：本项目偏好居中弹窗，不做抽屉式交互
    expect(view).not.toContain('qq-display-editor')
    expect(view).not.toContain('qq-display-toggle')
    // ⋯ 默认隐藏、hover/键盘聚焦淡入、触屏常显：行为由共用的 .icon-button 提供，
    // conv-menu 自身不再重复一份（重复过一次，删掉的那份回归过）
    expect(view).toMatch(/\.icon-button \{[^}]*opacity: 0/)
    expect(view).toMatch(/\.conversation:hover \.icon-button, \.conversation:focus-within \.icon-button \{ opacity: 1/)
    expect(view).toContain('@media (hover: none) { .icon-button { opacity: 1; } }')
    expect(view).not.toMatch(/\.conv-menu \{[^}]*opacity/)
    const store = read('src/stores/bot.ts')
    expect(store).toContain('updateConversationDisplay')
    expect(store).not.toContain('updateQqDisplay')
  })

  it('在场感节奏由状态层给：hesitate/relief/eager 三态，前端只认状态不猜', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain("store.pacing === 'hesitate'")
    expect(view).toContain("store.pacing === 'eager'")
    // eager 的可见形态：气泡延迟收短 + 打字点加速
    expect(view).toContain('delays[i] * 0.6')
    expect(view).toMatch(/\.typing-dots\.eager i \{ animation-duration/)
  })

  it('助手一轮按空行展开成多条气泡，用户原文不替他拆', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // 先展开再装饰：同轮的后续气泡走既有 cont 规则（隐头像、收间距），不新增样式
    expect(view).toContain('decorateMessages(expandBubbles(store.messages))')
    expect(view).toContain('decorateMessages(expandBubbles(store.remoteMessages))')
    expect(view).toContain(':content="d.item.bubble"')
    expect(view).toContain('{{ d.item.bubble }}')
    // 附属信息一轮只挂第一条：图片、思考折叠不随气泡重复
    expect(view).toContain('d.item.images?.length && !d.item.bubbleIndex')
    expect(view).toContain("!d.item.bubbleIndex && d.item.role !== 'user'")
    // 可访问名字每条气泡都要有：meta 那行连发时不渲染，读屏不能丢掉说话人
    expect(view).toContain('<span class="sr-only">{{ speakerLabel(d.item) }}：</span>')
    // 流式阶段同规则分条，收尾换成落库消息时看不到气泡重排
    // 表情包标记在流式阶段就先藏掉，落库后才由图替上
    expect(view).toContain('splitBubbles(stripStickerTokens(store.streamText))')
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

  it('默认人格不可删除：卡片标「默认」，删除按钮只出现在普通人格上', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('<span v-if="p.is_builtin" class="default-chip">默认</span>')
    expect(view).toContain('v-if="!p.is_builtin" class="btn btn-sm btn-danger" @click="removePersona(p)"')
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

  it('气泡密度：段落首尾外边距归零，markdown 输出不带幽灵空行', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // marked 的 <p> 自带 0.6em 上下 margin，落在气泡自己的 padding 里 → 一行字撑出 60px 高的框
    expect(view).toMatch(/\.message-content :deep\(\.markdown-body\) > :first-child \{ margin-top: 0; \}/)
    expect(view).toMatch(/\.message-content :deep\(\.markdown-body\) > :last-child \{ margin-bottom: 0; \}/)
    // marked 在每个块后留一个 \n，而气泡是 pre-wrap ⇒ 每条气泡底部凭空多一行
    expect(read('src/utils/markdown.ts')).toMatch(/DOMPurify\.sanitize\(highlighted, SANITIZE_OPTIONS\)\.trim\(\)/)
  })

  it('三档间距：同轮连发最紧、同人另起一轮居中、换人最松', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toMatch(/\.bot-message \{[^}]*margin-bottom: var\(--spacing-10\)/)
    expect(view).toMatch(/\.bot-message\.cont \{ margin-bottom: var\(--spacing-4\); \}/)
    expect(view).toMatch(/\.bot-message\.cont\.turn-start \{ margin-bottom: var\(--spacing-8\); \}/)
    // 负 margin 是当年为撑胖的气泡补的，框高正常后必须撤掉，否则两条气泡叠在一起
    expect(view).not.toMatch(/\.bot-message\.cont \{[^}]*margin-top: -/)
    // 说话人标识按「轮」给，不按「角色连发」给：几十条的 QQ 记录里不能长时间看不到名字
    expect(view).toContain('v-if="d.item.bubbleIndex === 0" class="msg-meta"')
    expect(view).toContain("'turn-start': d.item.bubbleIndex === 0")
  })

  it('流式演过的一轮不再二次投递：排程只管非流式来源', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const store = read('src/stores/bot.ts')
    // 真流式已经把第 1~N 条逐字演过了，落库时排程若再从 shown=1 开始，
    // 就等于把演过的内容收回去重演一遍（用户看到的「输出两次」）。
    expect(store).toContain('lastStreamedId.value = saved.id')
    expect(view).toContain('if (last.id === store.lastStreamedId)')
    // 排程本身留着：QQ 轮询与主动推送没有流式过程，靠它给在场感
    expect(view).toContain('bubbleDeliveryPlan(bubbles)')
  })

  it('本地与 QQ 记录共用一套气泡：配色按角色，meta 两边同规则', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    // 一份循环、一份气泡模板：两个 tab 不可能再各长回去
    expect(view).toContain('v-for="d in messageList"')
    expect((view.match(/class="bot-message"/g) || []).length).toBe(1)
    // 渠道专属方向/配色覆盖全部退役：取色只按角色，两个 tab 同一条规则
    expect(view).not.toContain('.chat-main.qq')
    expect(view).not.toContain('{ qq: store.isQq }')
    // meta 不再是 QQ 专属：按轮标说话人（具体口径见「三档间距」那条）
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

  it('远端会话行收成一行，名字居左，代号与时间进悬浮提示', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const row = view.match(/<button class="conversation-main conv-single"[\s\S]*?<\/button>/)?.[0] || ''
    expect(row).toContain(':title="remoteRowHint(c)"')
    expect(row).toContain('c.display_name || c.title')
    // 行内不再挂「群」小标签：群/私聊改由列表分组表达
    expect(row).not.toContain('conv-tag')
    // 第二段说明文字退役：250px 窄栏里必然折行，行高跟着文案长度漂；
    // 应答人格名右侧标题栏已经写了，代号只有排查价值，都挪进 title。
    expect(row).not.toContain('<small>')
    expect(view).not.toContain('qqPersonaLabel')
    const hint = view.match(/function remoteRowHint[\s\S]*?\n}/)?.[0] || ''
    expect(hint).toContain('peerLabel(conversation.external_id)')
    expect(hint).toContain('formatLocalDateTime(conversation.updated_at)')
    // 名字居左：<button> 默认 text-align:center，窄栏名字被居中过
    expect(view).toMatch(/\.conversation-main \{[^}]*text-align: left/)
  })

  it('远端列表按 external_id 分群聊/私聊：只有一组时不标标题', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const groups = view.match(/const REMOTE_GROUPS = \[[\s\S]*?\n\]/)?.[0] || ''
    expect(groups).toContain("label: '群聊'")
    expect(groups).toContain("label: '私聊'")
    const computed = view.match(/const remoteGroups = computed\(\(\)[\s\S]*?\n\)/)?.[0] || ''
    expect(computed).toContain("startsWith('group:')")
    expect(computed).toContain('filter(g => g.items.length)')
    expect(view).toContain('v-if="remoteGroups.length > 1" class="conv-group-label"')
    expect(view).toContain('{{ g.label }}')
    expect(view).toMatch(/\.conv-group-label \{[^}]*font-size/)
    // 分组后行内旧标签与纯函数一并退役
    expect(view).not.toContain('peerTag')
    expect(read('src/utils/bot-chat-decor.ts')).not.toContain('export function peerTag')
  })

  it('自定义显示弹窗：显示名并进头像那一栏，重置下沉到底栏', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    const modal = view.match(/<BaseModal v-if="displayTarget"[\s\S]*?<\/BaseModal>/)?.[0] || ''
    // 字段由 #fields 插槽交给共享选择器排版，弹窗自己不再套一层 div
    expect(modal).toContain('<template #fields>')
    expect(modal).toContain(':clearable="false"')
    expect(modal).not.toContain('qq-display-modal')
    // 恢复默认 = 名称与头像一起置空，所以选择器自带的「清除」在这个弹窗里多余
    const actions = modal.match(/<template #actions>([\s\S]*?)<\/template>/)?.[0] || ''
    expect(actions.indexOf('恢复默认')).toBeLessThan(actions.indexOf('取消'))
    expect(view).toMatch(/\.display-reset \{[^}]*margin-right: auto/)
  })
})
