/**
 * QQ 渠道卡守卫 —— 配置、状态、凭据三件事都收在一张卡里。
 * 钉：Secret 只写不回显；状态 5 秒轮询且卸载清理；连接/断开动作齐全。
 */
import cardRaw from './QqChannelCard.vue?raw'
import { describe, expect, it } from 'vitest'

const src = cardRaw.replace(/\r\n/g, '\n')

describe('QQ 渠道卡配置', () => {
  it('AppID / AppSecret / 人格三项齐全', () => {
    for (const needle of ['AppID', 'AppSecret', '当前人格', '保存并连接', '断开']) {
      expect(src).toContain(needle)
    }
  })

  it('Secret 不回显：留空即不修改，保存后清空输入', () => {
    expect(src).not.toContain('app_secret: config.app_secret')
    expect(src).toContain('...(qqForm.app_secret ? { app_secret: qqForm.app_secret } : {})')
    expect(src).toContain("qqForm.app_secret = ''")
    expect(src).toContain('AppSecret 只写入本地加密存储，配置接口不会回显。')
  })
})

describe('QQ 渠道卡状态', () => {
  it('5 秒轮询，卸载时清理定时器', () => {
    expect(src).toMatch(/setInterval\(\(\) => \{ void refreshQq\(\) \}, 5000\)/)
    expect(src).toContain('clearInterval(timer)')
    expect(src).toContain('onUnmounted')
  })

  it('轮询不覆盖正在编辑的表单', () => {
    expect(src).toContain('if (populate)')
    expect(src).toMatch(/async function refreshQq\(populate = false\)/)
  })

  it('在线时只给断开，离线或异常时给连接', () => {
    expect(src).toContain("qqStatus.state === 'offline' || qqStatus.state === 'error'")
    expect(src).toContain('disconnectQq')
  })
})
