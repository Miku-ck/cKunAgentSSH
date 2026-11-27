import { describe, expect, it } from 'vitest'
import modelsRaw from './ModelsPanel.vue?raw'
import appearanceRaw from './AppearancePanel.vue?raw'
import skillsRaw from './SkillsPanel.vue?raw'
import expertsRaw from './ExpertsPanel.vue?raw'
import pluginsRaw from './PluginsPanel.vue?raw'
import panelBaseRaw from './settings-panel-base.css?raw'

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

describe('共享控件焦点反馈', () => {
  const base = strip(panelBaseRaw)
  it('下拉框聚焦描边只在 :focus-visible（鼠标点击不变色）', () => {
    expect(base).toContain('.setting-select:focus-visible')
    expect(base).not.toMatch(/\.setting-select:focus\s*\{/)
  })
})

describe('清单面板骨架', () => {
  const panels = { skills: strip(skillsRaw), experts: strip(expertsRaw), plugins: strip(pluginsRaw) }

  it('三页统一引入 settings-list.css，不回流折叠分组样式', () => {
    for (const [name, raw] of Object.entries(panels)) {
      expect(raw, name).toContain('src="./settings-list.css"')
      expect(raw, name).not.toContain('settings-groups.css')
      expect(raw, name).not.toMatch(/class="grp-(head|body|caret)"/)
    }
  })

  it('展开态按钮给出成对文案，专家页「复制」排在「删除」之前', () => {
    expect(panels.plugins).toContain("{{ expanded.has(p.id) ? '收起' : '工具清单' }}")
    const acts = panels.experts.match(/<div class="row-acts">[\s\S]*?<\/div>/)?.[0] ?? ''
    expect(acts.indexOf('复制')).toBeGreaterThan(-1)
    expect(acts.indexOf('复制')).toBeLessThan(acts.indexOf('删除'))
  })
})
