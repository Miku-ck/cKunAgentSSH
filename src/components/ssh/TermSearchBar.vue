<template>
  <div class="term-search-bar" role="search" @mousedown.stop>
    <input
      ref="inputRef"
      v-model="query"
      class="ts-input"
      type="text"
      placeholder="搜索…"
      spellcheck="false"
      @input="find('next')"
      @keydown.enter.prevent="find($event.shiftKey ? 'prev' : 'next')"
      @keydown.escape.prevent="close"
    />
    <span v-if="resultText" class="ts-count">{{ resultText }}</span>
    <button class="ts-btn" :class="{ on: caseSensitive }" title="区分大小写" aria-label="区分大小写" @click="toggleOption('caseSensitive')">Aa</button>
    <button class="ts-btn" :class="{ on: regex }" title="正则表达式" aria-label="正则表达式" @click="toggleOption('regex')">.*</button>
    <button class="ts-btn" title="上一个 (Shift+Enter)" aria-label="上一个匹配" @click="find('prev')">▲</button>
    <button class="ts-btn" title="下一个 (Enter)" aria-label="下一个匹配" @click="find('next')">▼</button>
    <button class="ts-btn ts-close" title="关闭 (Esc)" aria-label="关闭搜索" @click="close">✕</button>
  </div>
</template>

<script setup lang="ts">
// TermSearchBar.vue — 终端内搜索条（M2-P0），Local/Remote 终端共用。
// addon 由父组件创建并 loadAddon；本组件只管 UI 与查询触发。
// 快捷键约定：Ctrl+Shift+F 唤起（^F 是 shell 前进字符，必须避开），Enter/Shift+Enter
// 逐个导航，Esc 关闭并把焦点还给终端 —— 与 useGlobalShortcuts 的「Shift 档」原则一致。
import { onMounted, ref } from 'vue'
import { accentSearchColors } from '@/composables/useTerminalConfig'

/**
 * 搜索 addon 的最小结构契约（不直接 import @xterm/addon-search 的类型进
 * defineProps —— vue-tsc 宏解析外部 declare-module 类型会退化成 never，
 * 结构化接口与真实 SearchAddon 逐字段兼容，父组件原样传实例即可）。
 */
interface TermSearchAddon {
  findNext(term: string, options?: unknown): boolean
  findPrevious(term: string, options?: unknown): boolean
  clearDecorations(): void
  onDidChangeResults(listener: (e: { resultIndex: number; resultCount: number }) => void): unknown
}

const props = defineProps<{
  addon: TermSearchAddon
}>()

const emit = defineEmits<{ close: [] }>()

// 概览标尺三色：SearchAddon 的 decorations 直接画在 overview ruler 上，吃不到
// CSS 主题令牌 —— 与 GpuNvtopPanel 的组件局部调色板同一豁免形状：一处声明、
// 有名字、可核对。取值改由 accentSearchColors() 按当前强调色派生，
// 终端区恒为 --bg-terminal 暗底，accent 在暗底上均有足够对比（令牌表保证）。

const query = ref('')
const caseSensitive = ref(false)
const regex = ref(false)
const resultText = ref('')
const inputRef = ref<HTMLInputElement | null>(null)

onMounted(() => {
  inputRef.value?.focus()
  // decorations 开启后 addon 会推结果计数（resultIndex/resultCount，-1 表示无）。
  // 监听器随 addon 一起由 terminal.dispose() 回收，无需手动退订
  props.addon.onDidChangeResults(r => {
    if (r.resultCount < 0) resultText.value = ''
    else resultText.value = `${Math.max(r.resultIndex, 0) + 1}/${r.resultCount}`
  })
})

function searchOptions() {
  const colors = accentSearchColors()
  return {
    caseSensitive: caseSensitive.value,
    regex: regex.value,
    decorations: {
      matchOverviewRuler: colors.match,
      activeMatchColorOverviewRuler: colors.active,
      activeMatchBorder: colors.border,
    } as const,
  }
}

function find(dir: 'next' | 'prev'): void {
  const term = query.value
  if (!term) {
    props.addon.clearDecorations()
    resultText.value = ''
    return
  }
  const opts = searchOptions()
  try {
    if (dir === 'next') props.addon.findNext(term, opts)
    else props.addon.findPrevious(term, opts)
  } catch (e) {
    // 非法正则等：不炸 UI，显示 0 匹配
    console.warn('[termSearch] find failed:', e)
    resultText.value = '0/0'
  }
}

function toggleOption(name: 'caseSensitive' | 'regex'): void {
  if (name === 'caseSensitive') caseSensitive.value = !caseSensitive.value
  else regex.value = !regex.value
  // 选项变化后用当前词重查一次
  find('next')
}

function close(): void {
  props.addon.clearDecorations()
  emit('close')
}
</script>

<style scoped>
.term-search-bar {
  position: absolute;
  top: 6px;
  right: 12px;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}

.ts-input {
  width: 160px;
  height: 24px;
  padding: 0 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-input-solid);
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-sans);
  outline: none;
}

.ts-input:focus {
  border-color: var(--accent-primary);
}

.ts-count {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
  min-width: 30px;
  text-align: center;
}

.ts-btn {
  min-width: 22px;
  height: 22px;
  padding: 0 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: 11px;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.ts-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* 开关型按钮（Aa / .*）激活态：强调色描边 + 强调色文字 */
.ts-btn.on {
  border-color: var(--accent-primary);
  color: var(--accent-text);
}

.ts-close:hover {
  color: var(--accent-red);
}
</style>
