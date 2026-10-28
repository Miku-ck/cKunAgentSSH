<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
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
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="chat-lightbox" @click.self="emit('close')">
      <div ref="dialogRef" class="chat-lightbox-dialog" tabindex="-1" role="dialog" aria-modal="true" aria-label="图片预览">
        <button class="chat-lightbox-close" type="button" aria-label="关闭预览" @click="emit('close')">×</button>
        <img :src="src" alt="预览图片" draggable="false" />
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
.chat-lightbox-close { position: absolute; top: -34px; right: 0; border: 0; background: transparent; color: #fff; font-size: 2rem; line-height: 1; cursor: pointer; }
</style>
