/**
 * 流式思考块擦洗状态机
 * 
 * 解决问题：reasoning 模型输出 <think>...</think> 块时，
 * 流式 delta 可能跨边界切断标签，一次性正则会漏掉。
 * 
 * 设计：
 * - 维护 _inBlock 状态，进入 think 块后不输出内容
 * - 维护 _buf 缓冲跨 delta 的半截标签
 * - block-boundary 规则：标签必须在行首/空行才认定为真标签
 */

const THINK_OPEN_TAGS = ['<think>', '<thinking>', '<reasoning>', '<thought>', '<REASONING_SCRATCHPAD>']
const THINK_CLOSE_TAGS = ['</think>', '</thinking>', '</reasoning>', '</thought>', '</REASONING_SCRATCHPAD>']
const MAX_TAG_LEN = Math.max(...THINK_OPEN_TAGS.concat(THINK_CLOSE_TAGS).map(t => t.length))

export class StreamingThinkScrubber {
  private _inBlock: boolean = false
  private _buf: string = ''
  private _lastEmittedEndedNewline: boolean = true

  /**
   * 喂入一段 delta 文本，返回可见部分（think 块内容被擦洗）
   */
  feed(text: string): string {
    let output = ''
    let i = 0
    // 把上次残留的 buf 和本次 text 拼起来处理
    let pending = this._buf + text
    this._buf = ''
    
    while (i < pending.length) {
      if (this._inBlock) {
        // 在 think 块内，寻找闭合标签
        const closeIdx = this._findCloseTag(pending, i)
        if (closeIdx.found) {
          // 找到闭合标签，跳过它
          this._inBlock = false
          i = closeIdx.end
          // 闭合标签后如果是换行，消耗掉
          if (i < pending.length && pending[i] === '\n') {
            i++
          }
        } else {
          // 未找到闭合标签，可能跨 delta，保留剩余到 buf
          // 但要检查 pending 末尾是否可能是半截闭合标签
          const tail = pending.substring(Math.max(i, pending.length - MAX_TAG_LEN))
          if (this._couldBePartialTag(tail, THINK_CLOSE_TAGS)) {
            this._buf = tail
          }
          break
        }
      } else {
        // 不在 think 块内，寻找开标签
        const openIdx = this._findOpenTag(pending, i)
        if (openIdx.found) {
          // 输出开标签之前的内容
          output += pending.substring(i, openIdx.start)
          this._inBlock = true
          i = openIdx.end
          this._lastEmittedEndedNewline = output.endsWith('\n')
        } else {
          // 未找到开标签，检查 pending 末尾是否可能是半截开标签
          const remaining = pending.substring(i)
          const partialMatch = this._matchPartialTag(remaining, THINK_OPEN_TAGS)
          if (partialMatch > 0) {
            // 保留半截到 buf
            output += remaining.substring(0, remaining.length - partialMatch)
            this._buf = remaining.substring(remaining.length - partialMatch)
            this._lastEmittedEndedNewline = output.endsWith('\n')
          } else {
            // 没有半截，全部输出
            output += remaining
            this._lastEmittedEndedNewline = output.endsWith('\n')
          }
          break
        }
      }
    }
    
    return output
  }

  /**
   * 流结束时调用，处理残留 buf
   */
  flush(): string {
    if (this._inBlock) {
      // 仍在 think 块内，丢弃（宁可截断也不泄露 partial CoT）
      this._buf = ''
      return ''
    }
    // 不在块内，输出残留 buf
    const result = this._buf
    this._buf = ''
    return result
  }

  /**
   * 每个新 turn 调用，重置状态
   */
  reset(): void {
    this._inBlock = false
    this._buf = ''
    this._lastEmittedEndedNewline = true
  }

  /**
   * 判断是否在 think 块内（用于 UI 状态判断）
   */
  isInThinkBlock(): boolean {
    return this._inBlock
  }

  // ── 内部方法 ──

  private _findOpenTag(text: string, start: number): { found: boolean; start: number; end: number } {
    for (const tag of THINK_OPEN_TAGS) {
      const idx = text.indexOf(tag, start)
      if (idx >= 0) {
        // block-boundary 检查：标签前必须是换行或字符串开头
        if (idx > 0 && !this._isBlockBoundary(text[idx - 1])) {
          continue
        }
        return { found: true, start: idx, end: idx + tag.length }
      }
    }
    return { found: false, start: -1, end: -1 }
  }

  private _findCloseTag(text: string, start: number): { found: boolean; end: number } {
    for (const tag of THINK_CLOSE_TAGS) {
      const idx = text.indexOf(tag, start)
      if (idx >= 0) {
        return { found: true, end: idx + tag.length }
      }
    }
    return { found: false, end: -1 }
  }

  private _isBlockBoundary(ch: string): boolean {
    return ch === '\n' || ch === '\r'
  }

  private _couldBePartialTag(tail: string, tags: string[]): boolean {
    for (const tag of tags) {
      // 检查 tail 是否是某个 tag 的前缀
      if (tag.startsWith(tail) && tail.length < tag.length) {
        return true
      }
      // 检查 tag 是否以 tail 结尾的部分前缀开头
      for (let len = Math.min(tail.length, tag.length - 1); len > 0; len--) {
        if (tail.endsWith(tag.substring(0, len))) {
          return true
        }
      }
    }
    return false
  }

  private _matchPartialTag(remaining: string, tags: string[]): number {
    // 检查 remaining 末尾是否匹配某个 tag 的前缀
    for (const tag of tags) {
      for (let len = Math.min(remaining.length, tag.length - 1); len > 0; len--) {
        if (remaining.endsWith(tag.substring(0, len))) {
          return len
        }
      }
    }
    return 0
  }
}

/**
 * 一次性擦洗函数（用于非流式场景）
 */
export function scrubThinkBlocks(text: string): string {
  const scrubber = new StreamingThinkScrubber()
  return scrubber.feed(text) + scrubber.flush()
}

/**
 * 检测文本是否包含 think 块
 */
export function hasThinkBlock(text: string): boolean {
  return THINK_OPEN_TAGS.some(tag => text.includes(tag))
}
