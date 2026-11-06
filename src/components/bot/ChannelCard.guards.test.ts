/**
 * 机器人渠道卡守卫 —— 一个机器人一张卡：配置、上下线、删除都收在卡里。
 * 钉：Secret 只写不回显；在线只给断开；删除要确认并说清会带走多少会话。
 */
import cardRaw from './ChannelCard.vue?raw'
import buttonCssRaw from '../../styles/button.css?raw'
import { describe, expect, it } from 'vitest'

const src = cardRaw.replace(/\r\n/g, '\n')
const btnCss = buttonCssRaw.replace(/\r\n/g, '\n')

describe('渠道卡配置', () => {
  it('名称 / AppID / AppSecret / 人格四项齐全', () => {
    for (const needle of ['名称', 'AppID', 'AppSecret', '当前人格']) {
      expect(src).toContain(needle)
    }
    expect(src).toContain('保存修改')
    expect(src).toContain('保存并连接')
  })

  it('Secret 不回显：留空即不修改，明文只在提交时带上', () => {
    expect(src).toContain('if (form.app_secret) payload.app_secret = form.app_secret')
    expect(src).toContain("form.app_secret = ''")
    expect(src).toContain("已保存；留空表示不修改")
    expect(src).not.toContain('app_secret: channel.app_secret')
    expect(src).toContain('AppSecret 只写入本地加密存储，配置接口不会回显。')
  })

  it('微信要三项凭据，并给出可复制的回调地址', () => {
    for (const needle of ['APPID', 'Token', 'EncodingAESKey', '回调地址', '公网地址']) {
      expect(src).toContain(needle)
    }
    expect(src).toContain('`/bot/wechat/${props.channel.id}`')
    expect(src).toContain('navigator.clipboard.writeText')
    expect(src).toContain('botApi.setWebhook')
  })

  it('没有公网地址时不给复制相对路径：微信只收完整地址', () => {
    expect(src).toContain('const canCopyCallback = computed(() => !!props.channel && !!baseUrl.value)')
    expect(src).toContain(':disabled="!canCopyCallback"')
    expect(src).toContain('请输入正确的接入地址')
  })

  it('provider 只在新建时可选，已建渠道不可改', () => {
    expect(src).toContain('v-if="!channel"')
    expect(src).toContain('<option value="wechat">微信</option>')
    expect(src).toContain("props.channel?.provider ?? form.provider")
  })

  it('密钥存在与否以服务端为准，不拿刚输入的内容冒充', () => {
    expect(src).toContain('if (form.app_secret) savedSecret.value = true')
  })

  it('人格下拉不看连接状态：换人格即时生效，不要求先断开', () => {
    // 后端在每条入站消息时才解析人格，界面再按 state 禁用这个 select 就是在
    // 替用户转述一个已经不存在的前提。
    expect(src).not.toContain(":disabled=\"state !== 'offline'\"")
    expect(src).toContain('切换即时生效，不用断开重连')
  })
})

describe('渠道卡动作', () => {
  it('在线或连接中只给断开，离线与异常给连接', () => {
    expect(src).toContain("state === 'offline' || state === 'error'")
    expect(src).toContain('connectChannel')
    expect(src).toContain('disconnectChannel')
  })

  it('删除要二次确认并写明连带删掉的会话数', () => {
    expect(src).toContain('useConfirm')
    expect(src).toContain('danger: true')
    expect(src).toContain('条会话与聊天记录会一并删除')
    expect(src).toContain('deleteChannel')
  })

  it('失败信息落在卡内，不静默', () => {
    expect(src).toContain('error.value = toFriendlyMessage(e')
    expect(src).toContain('{{ error }}')
  })
})

/**
 * 行内一致性 —— 状态文字和同行按钮是两套字号/两套间距时，差的是「一点点」，
 * 人工点测几乎看不出来，但它就是用户眼里的「这行排版不整齐」：
 *   · 字号：状态 14px 而 .btn-sm 13px，同一行里大一号；
 *   · 间距：按钮自带横向 padding，于是 gap 在「状态→按钮」上只算一遍（8+12=20px），
 *     在「按钮→按钮」上算两遍（12+8+12=32px）。
 * 所以钉的是**同源**：状态文字与 .btn-sm 用同一个字号、同一套横向内边距，
 * 每个边界都变成 pad+gap+pad，一行里只剩一种间距。具体数值改令牌不算破坏。
 */
describe('渠道卡行内一致性', () => {
  /** 取选择器第一条规则的声明块（到最近的 } 为止） */
  const rule = (text: string, selector: string): string => {
    const i = text.indexOf(selector)
    if (i < 0) return ''
    const open = text.indexOf('{', i)
    const close = text.indexOf('}', open)
    return open < 0 || close < 0 ? '' : text.slice(open, close + 1)
  }
  /** 取声明块里某个属性的值；没写该属性返回 null */
  const decl = (block: string, prop: string): string | null => {
    const m = new RegExp(`(?:^|[;\\s])${prop}:\\s*([^;]+)`).exec(block)
    return m ? m[1].trim() : null
  }

  const btnSm = rule(btnCss, '\n.btn-sm {')
  const statusRule = rule(src, '.channel-status {')

  it('前提：.btn-sm 显式写了字号与横向内边距（否则下面的同源断言会对着空字符串成立）', () => {
    expect(decl(btnSm, 'font-size'), 'button.css 的 .btn-sm 必须显式写 font-size').toBeTruthy()
    expect(decl(btnSm, 'padding'), 'button.css 的 .btn-sm 必须显式写 padding').toBeTruthy()
  })

  it('状态文字与同行 .btn-sm 按钮同一字号', () => {
    expect(decl(statusRule, 'font-size')).toBe(decl(btnSm, 'font-size'))
  })

  it('状态文字与按钮同一套横向内边距，gap 才不会被一边算两遍一边算一遍', () => {
    expect(decl(statusRule, 'padding')).toBe(decl(btnSm, 'padding'))
  })
})
