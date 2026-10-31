<script setup lang="ts">
import { ref } from 'vue'
import PersonaAvatar from './PersonaAvatar.vue'
import { readAvatarFile } from '@/utils/persona-avatar'
import { botApi, type BotGalleryItem } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'

// 统一头像入口（spec §4.6）：预览即上传入口 + 服务端图库选取。
// PersonaEditor / 远端会话显示设置 两处复用。
// 预览区未设置时只留一枚虚线空位，不生成任何默认图案。
// clearable=false 给「外层已有整体重置」的调用方（QQ 显示弹窗的恢复默认），
// 免得同一屏出现两个都把值置空的按钮。
withDefaults(defineProps<{ modelValue?: string | null; clearable?: boolean }>(), {
  modelValue: '',
  clearable: true,
})
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const busy = ref(false)
const error = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const showGallery = ref(false)
const gallery = ref<BotGalleryItem[]>([])
const keepInGallery = ref(true)

async function toggleGallery() {
  showGallery.value = !showGallery.value
  if (showGallery.value && !gallery.value.length) {
    try { gallery.value = await botApi.listGallery() }
    catch (e) { error.value = toFriendlyMessage(e, '图库读取失败') }
  }
}

function pick(item: BotGalleryItem) {
  emit('update:modelValue', item.data_url)
  showGallery.value = false
}

async function pickFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  busy.value = true
  error.value = ''
  try {
    const url = await readAvatarFile(file)
    emit('update:modelValue', url)
    // 存库尽力而为：图库写失败不该让「头像已设好」回滚成失败
    if (keepInGallery.value) {
      try { gallery.value = [await botApi.addGalleryItem({ data_url: url }), ...gallery.value] } catch { /* 忽略 */ }
    }
  } catch (e) { error.value = toFriendlyMessage(e, '头像处理失败') }
  finally { busy.value = false }
}
</script>

<template>
  <div class="avatar-picker">
    <button
      type="button"
      class="avatar-preview"
      :class="{ empty: !modelValue }"
      :title="modelValue ? '点击更换头像' : '点击上传头像'"
      aria-label="点击上传头像"
      @click="fileInput?.click()"
    ><PersonaAvatar :avatar="modelValue" /></button>
    <div class="avatar-picker-body">
      <slot name="fields" />
      <div class="avatar-picker-actions">
        <button type="button" class="btn" :disabled="busy" @click="fileInput?.click()">{{ busy ? '处理中…' : '上传图片' }}</button>
        <button type="button" class="btn" @click="toggleGallery">{{ showGallery ? '收起图库' : '从图库选' }}</button>
        <button v-if="clearable && modelValue" type="button" class="btn" @click="emit('update:modelValue', '')">清除</button>
        <label class="keep-gallery"><input type="checkbox" v-model="keepInGallery" /> 存入图库</label>
      </div>
      <div v-if="error" class="form-error">{{ error }}</div>
      <div v-if="showGallery" class="gallery-grid">
        <button
          v-for="item in gallery" :key="item.id" type="button" class="gallery-item"
          :title="item.label || '未命名'" @click="pick(item)"
        >
          <img :src="item.data_url" :alt="item.label || '图库头像'" />
        </button>
        <p v-if="!gallery.length" class="gallery-empty">图库还是空的，先上传一张吧。</p>
      </div>
    </div>
    <input ref="fileInput" class="avatar-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" @change="pickFile" />
  </div>
</template>

<style scoped>
.avatar-picker { display: flex; gap: var(--spacing-12); align-items: center; }
.avatar-preview {
  width: 64px; height: 64px; flex: 0 0 64px; display: grid; place-items: center;
  padding: 0; border: 0; border-radius: 50%; overflow: hidden;
  background: var(--bg-input); font: inherit; font-size: 1.7692rem; color: inherit; cursor: pointer;
  transition: box-shadow var(--transition-fast);
}
.avatar-preview:hover { box-shadow: inset 0 0 0 1px var(--accent-primary); }
.avatar-preview.empty { background: transparent; box-shadow: inset 0 0 0 1px var(--border-subtle); }
.avatar-preview.empty:hover { box-shadow: inset 0 0 0 1px var(--accent-primary); }
.avatar-picker-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--spacing-8); }
.avatar-picker-actions { display: flex; gap: var(--spacing-8); align-items: center; flex-wrap: wrap; }
.keep-gallery { display: flex; align-items: center; gap: var(--spacing-4); color: var(--text-tertiary); font-size: 0.7692rem; }
.avatar-file { display: none; }
.form-error { color: var(--danger-text); font-size: 0.8462rem; }
.gallery-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(48px, 1fr)); gap: var(--spacing-6); margin-top: var(--spacing-4); }
.gallery-item { padding: 0; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); overflow: hidden; background: transparent; cursor: pointer; aspect-ratio: 1; }
.gallery-item img { width: 100%; height: 100%; display: block; object-fit: cover; }
.gallery-empty { grid-column: 1 / -1; color: var(--text-tertiary); font-size: 0.8462rem; }
</style>
