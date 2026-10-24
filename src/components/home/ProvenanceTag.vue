<script setup lang="ts">
/**
 * provenance 来源徽标（spec 2026-10-24-task-structuring · 接缝 S6）
 *
 * 把"这一步/这一字段从哪来"用一枚小徽标说清，好让用户在对照弹窗里分得出
 * 「用户说的」与「模型补的/加的」—— 尤其 added（新增写动作）必须一眼可辨并逼确认。
 * 穷举四态：漏一态是编译错误，不是"那态悄悄没颜色"（对齐 PERM_MARKS 的穷举口径）。
 */
import { computed } from 'vue'
import { PROVENANCE_LABELS, type Provenance } from '@/utils/task-structure-core'

const props = defineProps<{ provenance: Provenance }>()
const cls = computed(() => `pt-${props.provenance}`)
const label = computed(() => PROVENANCE_LABELS[props.provenance])
</script>

<template>
  <span class="pt" :class="cls">{{ label }}</span>
</template>

<style scoped>
.pt {
  display: inline-flex;
  align-items: center;
  height: 16px;
  padding: 0 6px;
  border-radius: var(--radius-sm);
  font-size: 11px;
  line-height: 1;
  border: 1px solid transparent;
  white-space: nowrap;
}

/* 原文 = 中性弱描边；推断/补全 = 信息蓝渐进；新增 = 警示（必须扎眼，逼用户核对） */
.pt-verbatim {
  color: var(--text-secondary);
  border-color: var(--border-subtle);
}

.pt-inferred {
  color: var(--status-info-text);
  background: color-mix(in srgb, var(--status-info-text) 12%, transparent);
}

.pt-completed {
  color: var(--accent-indigo, var(--accent-primary));
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
}

.pt-added {
  color: var(--status-warn-text);
  border-color: var(--card-warn-border);
  background: color-mix(in srgb, var(--status-warn-text) 14%, transparent);
  font-weight: 600;
}
</style>
