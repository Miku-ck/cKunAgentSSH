/// <reference types="node" />
/**
 * 对话框可访问名守卫 —— 每个 role=dialog/alertdialog 的面都得有个名字
 *
 * 屏幕阅读器把没有 aria-label / aria-labelledby 的对话框读成"对话框"，不说是哪个。
 * 全仓四十几处对话框里已经有一半写了名字（"新建连接""文件冲突处理""卷详情：xxx"），
 * 漏掉的几处全是历史遗留 —— 这种属性最容易被下一次模板改动顺手丢掉，所以用断言钉。
 *
 * 只检查开标签本身：多行属性的标签要先拼成一段文本再判断。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { walk, rel } from '../styles/fs-walk'

interface Offender {
  file: string
  tag: string
}

/** 把源码里所有带 role="dialog"/"alertdialog" 的开标签摘出来（跨行属性也算一个）。 */
function dialogTags(source: string): string[] {
  const tags = source.match(/<[A-Za-z][\w.:-]*(?:\s[^<>]*?)?\/?>/gs) ?? []
  return tags.filter((tag) => /role="(?:dialog|alertdialog)"/.test(tag))
}

const offenders: Offender[] = []
for (const file of walk()) {
  const source = readFileSync(file, 'utf8').replace(/\r\n/g, '\n')
  for (const tag of dialogTags(source)) {
    const named = /aria-label(?:ledby)?=/.test(tag)
    const modal = /aria-modal/.test(tag)
    if (!named || !modal) {
      offenders.push({ file: rel(file), tag: tag.replace(/\s+/g, ' ') })
    }
  }
}

describe('对话框可访问名', () => {
  it('每个 role=dialog/alertdialog 都带 aria-label(ledby) 与 aria-modal', () => {
    const detail = offenders.map((o) => `${o.file}: ${o.tag}`).join('\n')
    expect(offenders, `这些对话框缺可访问名或 aria-modal：\n${detail}`).toEqual([])
  })
})
