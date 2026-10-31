/**
 * 「试一句」结果文案 —— 后端 probe 返回的就是账本上那一行，这里只负责把它念成人话。
 * 五种动作各有各的事实：真发出去了、直投会话、离线寄存、没发出去（平台拒收或
 * 模型没吐字）、人格不在，混成一句「已发出」等于骗操作者去群里找一句根本没到的话。
 */
import type { ProactiveProbeResult } from '@/api/bot'

/** 自动路径本来过不了闸时补的那半句：gate_reason 为空说明本来就该过，不硬造。 */
function gateHint(gateReason: string): string {
  return gateReason ? `（自动开口本来会卡在：${gateReason}）` : ''
}

export function probeOutcomeText(result: ProactiveProbeResult): string {
  const said = result.text ? `：「${result.text}」` : ''
  const hint = gateHint(result.gate_reason)
  switch (result.action) {
    case 'push':
      return `已发出${said}${hint}`
    case 'send':
      return `已投进这个会话${said}${hint}`
    case 'queued':
      return `这个渠道现在发不出去，先寄存着${said}，下次连上会捎上${hint}`
    case 'skip':
      return `这句没发出去：${result.detail || '未知原因'}`
    case 'missing':
      return '这个人格已经不在了，没有发送'
    default:
      return `结果未识别：${result.action}`
  }
}
