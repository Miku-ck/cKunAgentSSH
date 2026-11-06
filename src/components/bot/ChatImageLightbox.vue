<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useModalFocusTrap } from '@/composables/useModalFocusTrap'

// 聊天图片灯箱：应用内放大查看消息配图，不跳转不外发。
// 与全仓弹窗同口径接共享焦点件（Tab 环形 + 关闭还原焦点 + Esc 出口）。
const props = defineProps<{ visible: boolean; src: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef)
watch(() => props.visible, (v) => { if (v) activate(); else deactivate() })

function onKeydown(e: KeyboardEvent): void { if (e.key === 'Escape') emit('close') }
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

// 图片 URL 可能已失效（后端图片被清、data URL 过大被截断）：失败时给一句人话，
// 而不是让用户对着破图图标猜。切图/开关灯箱时重置，否则下一张也一直是失败态。
const failed = ref(false)
watch(() => [props.visible, props.src], () => { failed.value = false })
const imgAlt = computed(() => (failed.value ? '图片加载失败' : '预览图片'))
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="chat-lightbox" @click.self="emit('close')">
      <div ref="dialogRef" class="chat-lightbox-dialog" tabindex="-1" role="dialog" aria-modal="true" aria-label="图片预览">
        <button class="chat-lightbox-close" type="button" aria-label="关闭预览" @click="emit('close')">×</button>
        <img v-show="!failed" :src="src" :alt="imgAlt" draggable="false" @error="failed = true" />
        <p v-if="failed" class="chat-lightbox-failed">图片加载失败，可能已失效。</p>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.chat-lightbox {
  position: fixed; inset: 0; z-index: 1200;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: grid; place-items: center;
}
.chat-lightbox-dialog { position: relative; outline: none; }
.chat-lightbox-dialog img { max-width: 88vw; max-height: 88vh; border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); display: block; }
/* 失败占位：给一个明确尺寸的卡，避免遮罩中央空荡荡只有一个关闭键 */
.chat-lightbox-failed { padding: var(--padding) var(--spacing-20); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); color: var(--text-secondary); font-family: var(--font-spec); font-size: 14px; }
/* 关闭键自带圆底：它压在图片上，而图片明暗不可预测（也不跟主题变），
   写死 #fff 会在浅色图上消失、在浅色主题的淡遮罩里也糊成一片。
   圆底读 --bg-elevated（唯一浮起层）、字读 --text-primary，任何主题下都 ≥4.5:1。 */
.chat-lightbox-close {
  position: absolute; top: -34px; right: 0;
  width: 28px; height: 28px; display: grid; place-items: center;
  border: 1px solid var(--border-subtle); border-radius: 50%;
  background: var(--bg-elevated); color: var(--text-primary);
  font-size: 1.25rem; line-height: 1; cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast);
}
.chat-lightbox-close:hover { background: var(--bg-hover); color: var(--text-primary); }
</style>
