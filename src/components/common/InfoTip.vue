<template>
  <span
    class="info-tip"
    role="img"
    :tabindex="focusable ? 0 : -1"
    :title="text"
    :aria-label="text"
  >
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="11" x2="12" y2="16.5" />
      <circle cx="12" cy="7.8" r="0.4" fill="currentColor" stroke="none" />
    </svg>
  </span>
</template>

<script setup lang="ts">
// 提示气泡统一走原生 title：自绘 tooltip 会被设置面板/菜单的 overflow 裁掉
// （HomeComposer 里已有同结论）。focusable=false 供嵌在 button 内时使用，
// 避免按钮里再套一个可 Tab 停靠点。
withDefaults(defineProps<{ text: string; focusable?: boolean }>(), { focusable: true })
</script>

<style scoped>
.info-tip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  vertical-align: -2px;
  color: var(--text-tertiary);
  cursor: help;
  border-radius: 50%;
  transition: color 0.15s ease, box-shadow 0.15s ease;
}
.info-tip:hover { color: var(--accent-text); }
.info-tip:focus { outline: none; color: var(--accent-text); box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb), 0.25); }
</style>