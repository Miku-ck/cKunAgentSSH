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

export class StreamingThinkScrubber {
  private _inBlock: boolean = false
  private _buf: string = ''

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
          // 未找到闭合标签，可能跨 delta：只保留"最后一个 < 之后、且确实是
          // 某个闭合标签前缀"的片段到 buf。不能放宽到任意文本末尾 —
          // 否则 "secret"（内容词）会被当成 "</secret" 的潜在前缀而滞留缓存。
          const remaining = pending.substring(i)
          const lastOpen = remaining.lastIndexOf('<')
          let keep = ''
          if (lastOpen >= 0) {
            const tail = remaining.substring(lastOpen)
            if (this._matchPartialTag(tail, THINK_CLOSE_TAGS) === tail.length) {
              keep = tail
            }
          }
          this._buf = keep
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
        } else {
          // 未找到开标签，检查 pending 末尾是否可能是半截开标签
          const remaining = pending.substring(i)
          const partialMatch = this._matchPartialTag(remaining, THINK_OPEN_TAGS)
          if (partialMatch > 0) {
            // 保留半截到 buf
            output += remaining.substring(0, remaining.length - partialMatch)
            this._buf = remaining.substring(remaining.length - partialMatch)
          } else {
            // 没有半截，全部输出
            output += remaining
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
    // B13 修复：buf 尾部若恰好是某个开标签的前缀（如正文以 "<th" 结尾），
    // 流已再无后续字符可拼出完整标签 — 原样输出会泄漏残缺标签，丢弃该前缀；
    // 普通文本（不构成标签前缀）保留
    const partial = this._matchPartialTag(this._buf, THINK_OPEN_TAGS)
    const result = partial > 0 ? this._buf.slice(0, this._buf.length - partial) : this._buf
    this._buf = ''
    return result
  }

  /**
   * 每个新 turn 调用，重置状态
   */
  reset(): void {
    this._inBlock = false
    this._buf = ''
  }

  /**
   * 判断是否在 think 块内（用于 UI 状态判断）
   */
  isInThinkBlock(): boolean {
    return this._inBlock
  }

  // ── 内部方法 ──

  private _findOpenTag(text: string, start: number): { found: boolean; start: number; end: number } {
    // B13 修复：按标签最早出现位置（min indexOf）选择，而非列表顺序 —
    // 原实现在列表靠前标签位置更晚时会误选更靠后的标签，
    // 把中间内容（可能含真正标签）当成普通正文输出
    const candidates: Array<{ tag: string; idx: number }> = []
    for (const tag of THINK_OPEN_TAGS) {
      const idx = text.indexOf(tag, start)
      if (idx >= 0) candidates.push({ tag, idx })
    }
    // 按出现位置升序：最早出现的标签优先（位置相同时保持列表顺序）
    candidates.sort((a, b) => a.idx - b.idx)
    for (const { tag, idx } of candidates) {
      // block-boundary 检查：标签前必须是换行或字符串开头
      if (idx > 0 && !this._isBlockBoundary(text[idx - 1])) {
        continue
      }
      return { found: true, start: idx, end: idx + tag.length }
    }
    return { found: false, start: -1, end: -1 }
  }

  private _findCloseTag(text: string, start: number): { found: boolean; end: number } {
    // 审查修复（P2）：按最早出现位置（min indexOf）选择，与 _findOpenTag 对称 —
    // 原实现按列表顺序返回第一个命中的标签，若列表中靠前的闭合标签
    // （如 </thinking>）出现在较晚位置、靠后的（如 </thought>）出现较早，
    // 会把两者之间的 think 块内容（真实 CoT）误当普通正文输出（泄漏窄窗口）
    const candidates: Array<{ tag: string; idx: number }> = []
    for (const tag of THINK_CLOSE_TAGS) {
      const idx = text.indexOf(tag, start)
      if (idx >= 0) candidates.push({ tag, idx })
    }
    if (candidates.length === 0) return { found: false, end: -1 }
    candidates.sort((a, b) => a.idx - b.idx)
    const { tag, idx } = candidates[0]
    return { found: true, end: idx + tag.length }
  }

  private _isBlockBoundary(ch: string): boolean {
    return ch === '\n' || ch === '\r'
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
