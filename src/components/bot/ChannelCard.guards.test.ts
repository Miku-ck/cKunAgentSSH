/**
 * 机器人渠道卡守卫 —— 一个机器人一张卡：配置、上下线、删除都收在卡里。
 * 钉：Secret 只写不回显；在线只给断开；删除要确认并说清会带走多少会话。
 */
import cardRaw from './ChannelCard.vue?raw'
import { describe, expect, it } from 'vitest'

const src = cardRaw.replace(/\r\n/g, '\n')

describe('渠道卡配置', () => {
  it('名称 / AppID / AppSecret / 人格四项齐全', () => {
    for (const needle of ['名称', 'AppID', 'AppSecret', '当前人格']) {
      expect(src).toContain(needle)
    }
    expect(src).toContain('保存修改')
    expect(src).toContain('保存并连接')
  })

  it('Secret 不回显：留空即不修改，明文只在提交时带上', () => {
    expect(src).toContain("form.app_secret ? { app_secret: form.app_secret } : {}")
    expect(src).toContain("form.app_secret = ''")
    expect(src).toContain("已保存；留空表示不修改")
    expect(src).not.toContain('app_secret: channel.app_secret')
    expect(src).toContain('AppSecret 只写入本地加密存储，配置接口不会回显。')
  })

  it('密钥存在与否以服务端为准，不拿刚输入的内容冒充', () => {
    expect(src).toContain('if (form.app_secret) savedSecret.value = true')
  })

  it('人格在线时不可改，避免正在生成的回复用错人格', () => {
    expect(src).toContain(":disabled=\"state !== 'offline'\"")
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
