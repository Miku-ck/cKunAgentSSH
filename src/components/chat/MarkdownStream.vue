<template>
  <div class="markdown-body" v-html="renderedHtml" @click="onMarkdownClick"></div>
</template>

<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { renderMarkdown } from '@/utils/markdown'
import { useMarkdownDelegation } from '@/composables/useMarkdownDelegation'

const props = defineProps<{
  content: string
}>()

const { onMarkdownClick } = useMarkdownDelegation()

// C34 + P3 修复：流式场景下原实现每 150ms 对【整条消息】全量 marked+highlight+DOMPurify
// —— 已确认的段落（稳定前缀）被反复重渲染，随消息变长开销线性增长。
// 这里做"稳定前缀缓存"：按最后一个【围栏外】段落边界（\n\n）把内容切成 stable 前缀 + 动态尾部；
// stable 前缀的渲染结果缓存，仅当前缀本身变化时重渲；每 tick 只重渲尾部，
// 最终 innerHTML = 前缀缓存 html + 尾部 html。
//
// 边界安全（为何不直接按 \n\n 切）：围栏代码块 ``` 内部可能含 \n\n，若在其中切分会
// 把代码块劈成未闭合围栏，marked 会把尾部内容吞进前缀导致渲染错乱。故：
//   1) 仅当全文围栏行配平（偶数）时才切分 —— 未配平（代码块流式输入中）回退整串渲染；
//   2) 切分点必须是"围栏外"（到该点为止围栏行为偶数）。
// 表格行内无 \n\n，天然不会被切开。前缀与尾部各自经 renderMarkdown（内含 DOMPurify）
// 净化后再拼接 —— 拼接两个已消毒片段是安全的。
const RENDER_THROTTLE_MS = 150
const renderedHtml = ref('')
let _renderTimer: ReturnType<typeof setTimeout> | null = null
let _cachedStable = ''
let _cachedStableHtml = ''

/** 找最后一个围栏外 \n\n 的切分位置；返回 stable 结束下标（含该 \n\n），无安全切分点返回 -1 */
function stableSplit(content: string): number {
  // 全文围栏行（行首 ``` 或 ~~~）必须配平，才存在真正的"围栏外"块边界
  const fenceTotal = (content.match(/^[\t ]*(?:```|~~~)/gm) || []).length
  if (fenceTotal % 2 !== 0) return -1

  let split = -1
  let running = 0
  let lineStart = 0
  const n = content.length
  while (lineStart <= n) {
    let lineEnd = content.indexOf('\n', lineStart)
    if (lineEnd === -1) lineEnd = n
    if (/^[\t ]*(?:```|~~~)/.test(content.slice(lineStart, lineEnd))) running++
    // 本行后紧跟空行（\n\n）且当前不在围栏内 → 真正的块边界，可作切分点
    if (lineEnd < n && content[lineEnd + 1] === '\n' && running % 2 === 0) {
      split = lineEnd + 2
    }
    lineStart = lineEnd + 1
  }
  return split
}

function render(): void {
  const content = props.content || ''
  const split = stableSplit(content)
  const stable = split >= 0 ? content.slice(0, split) : ''
  const tail = split >= 0 ? content.slice(split) : content
  // 前缀不变则复用缓存，只重渲尾部
  if (stable !== _cachedStable) {
    _cachedStable = stable
    _cachedStableHtml = renderMarkdown(stable)
  }
  renderedHtml.value = _cachedStableHtml + renderMarkdown(tail)
}

function scheduleRender(): void {
  if (_renderTimer !== null) return // 已有待执行的 flush，尾沿节流复用同一 timer
  _renderTimer = setTimeout(flushRender, RENDER_THROTTLE_MS)
}

function flushRender(): void {
  _renderTimer = null
  render()
}

// 初始渲染（挂载时内容可能非空且不会再触发 watch）
render()

watch(() => props.content, (v) => {
  if (v === '') {
    // 消息重置/撤销会把 content 清空：这里若直接早退，renderedHtml 与稳定前缀
    // 缓存都残留上一条内容，复用组件实例的调用方（未按 msg.id 重挂载）会看到残影。
    // 同步清空即回到挂载初态（renderMarkdown('') 本就返回 ''，缓存不变量仍成立），
    // 也无须排渲染 —— 空内容没有可渲染的东西。
    renderedHtml.value = ''
    _cachedStable = ''
    _cachedStableHtml = ''
    return
  }
  scheduleRender()
})

// 组件卸载时清理未触发的定时器（卸载后内容不再变化，无需补 flush）
onUnmounted(() => {
  if (_renderTimer !== null) {
    clearTimeout(_renderTimer)
    _renderTimer = null
  }
})

/** 事件委托（复制 + 外链拦截）已下沉 useMarkdownDelegation，聊天与历史共用 */
</script>

<style scoped>
/* 容器排版留在组件内（聊天字号走 --chat-font-size）；正文/代码块/表格等
   共享视觉词汇在 styles/markdown.css，聊天与历史共用一份。 */
.markdown-body {
  /* 对话设置里的「消息字号」写进 --chat-font-size（useAppearance）。
     这里此前是裸 13px，控件调了只有 :root 上的变量在变，气泡文字一动不动。 */
  font-size: var(--chat-font-size, 13px);
  line-height: 1.7;
  color: var(--text-primary);
}
</style>
