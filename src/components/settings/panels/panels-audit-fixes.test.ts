import { describe, expect, it } from 'vitest'
import modelsRaw from './ModelsPanel.vue?raw'
import appearanceRaw from './AppearancePanel.vue?raw'

const strip = (s: string) =>
  s.replace(/\r\n/g, '\n').replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '')

const models = strip(modelsRaw)
const appearance = strip(appearanceRaw)

describe('Models 空态与主操作', () => {
  it('「添加模型」是纯文字按钮，不配 ＋ 装饰图标', () => {
    const button = models.match(/<button class="btn btn-primary btn-sm"[^>]*>[\s\S]*?<\/button>/)?.[0] ?? ''
    expect(button).toContain('添加模型')
    expect(button).not.toContain('<svg')
  })

  it('空态只有一行事实，不再写操作说明', () => {
    expect(models).toContain('<p>暂无模型配置</p>')
    expect(models).not.toContain('点击「添加模型」配置第一个 LLM 端点')
  })
})

describe('背景图控件跟随总开关', () => {
  it('总开关关闭时，选择/清除与两个滑块一并禁用', () => {
    const disabled = appearance.match(/:disabled="!settings\.appearance\.backgroundEnabled"/g) ?? []
    expect(disabled.length).toBeGreaterThanOrEqual(4)
  })
})
