<script setup lang="ts">
/**
 * 首页标题 = 目标主机选择器（spec 用户故事 2/3）
 *
 * 标题里那串 username@host 不是装饰文案，它是整页唯一的目标切换入口：点开
 * 就是已打开的会话列表，选谁下一条指令就落在谁身上。再放一枚独立的「目标」
 * chip 是重复 —— 用户判定过。
 *
 * 「全部 N 台」走的是另一条通路：批量下发是人工通道（命令风险判定 + 结果并排
 * 对比），不改变 AI 的目标。agent store 一次只跑一条 run，所以这里不假装
 * "对全部 N 台下达指令"。
 */
import { onMounted, onUnmounted, ref } from 'vue'
import { useHomeTarget } from '@/composables/useHomeTarget'

const emit = defineEmits<{ bulk: [] }>()

const { targetLabel, options, pick, remoteCount, canBulkDispatch } = useHomeTarget()
const open = ref(false)
const root = ref<HTMLElement | null>(null)

function onDocClick(e: MouseEvent) {
  if (open.value && root.value && !root.value.contains(e.target as Node)) open.value = false
}
onMounted(() => document.addEventListener('click', onDocClick))
onUnmounted(() => document.removeEventListener('click', onDocClick))

function choose(sessionId: string) {
  pick(sessionId)
  open.value = false
}

function toBulk() {
  emit('bulk')
  open.value = false
}
</script>

<template>
  <h1 class="htt">
    今天想让<span ref="root" class="htt-pick">
      <button
        type="button"
        class="htt-btn"
        :class="{ open }"
        title="切换目标主机"
        :aria-expanded="open"
        @click.stop="open = !open"
      >{{ targetLabel }}<span class="cap">▾</span></button>
      <span v-if="open" class="htt-menu" @click.stop>
        <button
          v-for="o in options"
          :key="o.sessionId"
          type="button"
          class="htt-opt"
          :class="{ on: o.active }"
          @click="choose(o.sessionId)"
        >
          <span class="l1">{{ o.label }}</span>
          <span class="l2">{{ o.hint }}</span>
          <span class="tick">{{ o.active ? '✓' : '' }}</span>
        </button>
        <span v-if="!options.length" class="htt-none">还没有打开的会话 —— 点标签条右侧的 ＋ 新建终端</span>
        <button v-if="canBulkDispatch" type="button" class="htt-opt bulk" @click="toBulk">
          <span class="l1">全部 {{ remoteCount }} 台</span>
          <span class="l2">批量下发 · 不改 AI 目标</span>
        </button>
      </span>
    </span>替你做什么？
  </h1>
</template>

<style scoped>
.htt {
  width: 100%;
  max-width: 720px;
  margin: 0 0 14px;
  font-size: 19px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
  letter-spacing: .2px;
}
.htt-pick { position: relative; display: inline-block; margin: 0 5px }
.htt-btn {
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
  padding: 1px 6px 2px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 17px;
  font-weight: 500;
  color: var(--accent-primary);
  cursor: pointer;
  transition: background var(--transition-fast);
}
.htt-btn:hover, .htt-pick .htt-btn.open { background: var(--bg-hover) }
.cap { font-size: 10px; color: var(--text-tertiary) }
.htt-menu {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  width: 268px;
  max-height: 280px;
  overflow-y: auto;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  font-family: var(--font-sans);
  font-size: 12px;
  font-weight: 400;
  letter-spacing: 0;
  z-index: 30;
}
.htt-opt {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 1px 8px;
  width: 100%;
  text-align: left;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  transition: background var(--transition-fast);
}
.htt-opt:hover { background: var(--bg-hover) }
.htt-opt.on .l1 { color: var(--accent-primary) }
.htt-opt .l1 {
  grid-column: 1;
  font-family: var(--font-mono);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.htt-opt .l2 {
  grid-column: 1;
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.htt-opt .tick {
  grid-column: 2;
  grid-row: 1 / 3;
  align-self: center;
  font-family: var(--font-mono);
  color: var(--accent-green);
}
.htt-opt.bulk { border-top: 1px solid var(--divider-primary); border-radius: 0 0 var(--radius-sm) var(--radius-sm) }
.htt-opt.bulk .l1 { font-family: var(--font-sans); color: var(--text-secondary) }
/* 菜单挂在 span 里（h1 的内容模型只允许短语内容），空态文案要自己撑一行 */
.htt-none { display: block; padding: 8px; font-size: 11px; color: var(--text-tertiary); line-height: 1.5 }
</style>
