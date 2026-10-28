<script setup lang="ts">
import { ref } from 'vue'
import PersonaAvatar from './PersonaAvatar.vue'
import { readAvatarFile } from '@/utils/persona-avatar'
import { botApi, type BotGalleryItem } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'

// 统一头像入口（spec §4.6）：预览 + 上传（沿用 readAvatarFile 方形 256 webp 一份口径）
// + 服务端图库选取。PersonaEditor / Bot 管理「你的头像」/ QQ 会话头像三处复用。
defineProps<{ modelValue?: string | null; seed?: string | null; label?: string | null }>()
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
    <span class="avatar-preview"><PersonaAvatar :avatar="modelValue" :seed="seed" :label="label" /></span>
    <div class="avatar-picker-body">
      <div class="avatar-picker-actions">
        <button type="button" class="btn" :disabled="busy" @click="fileInput?.click()">{{ busy ? '处理中…' : '上传图片' }}</button>
        <button type="button" class="btn" @click="toggleGallery">{{ showGallery ? '收起图库' : '从图库选' }}</button>
        <button v-if="modelValue" type="button" class="btn" @click="emit('update:modelValue', '')">清除</button>
      </div>
      <label class="keep-gallery"><input type="checkbox" v-model="keepInGallery" /> 上传后存入图库</label>
      <small>PNG / JPG / WebP，自动裁成方形 256px</small>
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
.avatar-picker { display: flex; gap: var(--spacing-12); align-items: flex-start; }
.avatar-preview { width: 56px; height: 56px; flex: 0 0 56px; display: grid; place-items: center; border-radius: 50%; overflow: hidden; background: var(--bg-input); font-size: 1.5385rem; }
.avatar-picker-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--spacing-6); }
.avatar-picker-actions { display: flex; gap: var(--spacing-8); flex-wrap: wrap; }
.keep-gallery { display: flex; align-items: center; gap: var(--spacing-4); color: var(--text-secondary); font-size: 0.7692rem; }
.avatar-picker-body small { color: var(--text-tertiary); font-size: 0.7692rem; }
.avatar-file { display: none; }
.form-error { color: var(--danger-text); font-size: 0.8462rem; }
.gallery-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(48px, 1fr)); gap: var(--spacing-6); margin-top: var(--spacing-4); }
.gallery-item { padding: 0; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); overflow: hidden; background: transparent; cursor: pointer; aspect-ratio: 1; }
.gallery-item img { width: 100%; height: 100%; display: block; object-fit: cover; }
.gallery-empty { grid-column: 1 / -1; color: var(--text-tertiary); font-size: 0.8462rem; }
</style>
