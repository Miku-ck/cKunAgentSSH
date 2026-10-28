<template>
  <div class="panel">
    <header class="panel-header">
      <h3>Bot 管理</h3>
      <p class="panel-desc">cKunBot 头像体系 —— 服务端头像图库与「你的头像」；QQ 会话的自定义显示名/头像在陪聊记录页就地编辑</p>
    </header>

    <div class="panel-body">
      <div class="field-group">
        <h4>你的头像</h4>
        <p class="field-desc">本地会话里你这一侧的头像；留空则不显示头像。QQ 会话中你不发言，不适用。</p>
        <AvatarPicker v-model="userAvatar" @update:model-value="saveUserAvatar" />
        <p v-if="saveError" class="form-error">{{ saveError }}</p>
      </div>

      <div class="field-group">
        <h4>头像图库</h4>
        <p class="field-desc">集中管理的头像（方形 256px）；各处头像可选自图库。删除图库项不影响已设置的头像 —— 它们存的是自己的副本。</p>
        <div class="gallery-upload">
          <input v-model.trim="galleryLabel" class="gallery-label-input" maxlength="50" placeholder="命名（可留空）" />
          <button type="button" class="btn" :disabled="uploading" @click="galleryInput?.click()">{{ uploading ? '处理中…' : '上传到图库' }}</button>
          <input ref="galleryInput" class="gallery-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" @change="uploadToGallery" />
        </div>
        <p v-if="galleryError" class="form-error">{{ galleryError }}</p>
        <div class="gallery-grid">
          <div v-for="item in gallery" :key="item.id" class="gallery-cell">
            <img :src="item.data_url" :alt="item.label || '图库头像'" />
            <div class="gallery-tools">
              <small>{{ item.label || '未命名' }}</small>
              <button type="button" class="btn tiny" @click="removeFromGallery(item.id)">删除</button>
            </div>
          </div>
          <p v-if="!gallery.length" class="gallery-empty">图库还是空的。</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * Bot 管理面板 —— 头像体系的集中管理区：
 * 「你的头像」选中即存（bot_user_avatar 设置键），图库区上传/删除走 /bot/gallery。
 * 图库列表读取失败不阻塞面板：头像还能编辑，只是网格暂时空白。
 */
import { onMounted, ref } from 'vue'
import AvatarPicker from '@/components/bot/AvatarPicker.vue'
import { botApi, type BotGalleryItem } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'
import { readAvatarFile } from '@/utils/persona-avatar'
import { useBotStore } from '@/stores/bot'

const userAvatar = ref('')
const saveError = ref('')

const gallery = ref<BotGalleryItem[]>([])
const galleryLabel = ref('')
const galleryInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const galleryError = ref('')

onMounted(async () => {
  // 两个读并行、各自兜错：一边挂了另一边照常工作
  const [avatarRes, galleryRes] = await Promise.allSettled([
    botApi.getUserAvatar(),
    botApi.listGallery(),
  ])
  if (avatarRes.status === 'fulfilled') userAvatar.value = avatarRes.value.avatar || ''
  if (galleryRes.status === 'fulfilled') gallery.value = galleryRes.value
})

// 选中即存（图库项 / 上传结果都会触发 update:modelValue），不做额外保存按钮。
async function saveUserAvatar(value: string) {
  saveError.value = ''
  try { await botApi.setUserAvatar(value ?? '') } catch (e) { saveError.value = toFriendlyMessage(e, '头像保存失败'); return }
  // 保存成功同步 bot store：陪聊用户气泡立刻换图，不用等重启（SshView 粘性挂载，init 只跑一次）
  useBotStore().loadUserAvatar()
}

async function uploadToGallery(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploading.value = true
  galleryError.value = ''
  try {
    const dataUrl = await readAvatarFile(file)
    const created = await botApi.addGalleryItem({ label: galleryLabel.value, data_url: dataUrl })
    gallery.value = [created, ...gallery.value]
    galleryLabel.value = ''
  } catch (e) { galleryError.value = toFriendlyMessage(e, '图库上传失败') }
  finally { uploading.value = false }
}

async function removeFromGallery(id: string) {
  galleryError.value = ''
  try {
    await botApi.deleteGalleryItem(id)
    gallery.value = gallery.value.filter((item) => item.id !== id)
  } catch (e) { galleryError.value = toFriendlyMessage(e, '图库删除失败') }
}
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped>
.field-group { display: flex; flex-direction: column; gap: var(--spacing-6); margin-bottom: var(--spacing-20); }
.field-group h4 { margin: 0; font-size: 0.95rem; color: var(--text-primary); }
.field-desc { margin: 0 0 var(--spacing-4); color: var(--text-tertiary); font-size: 0.8077rem; line-height: 1.6; }
.form-error { color: var(--danger-text); font-size: 0.8462rem; }
.gallery-upload { display: flex; align-items: center; gap: var(--spacing-8); flex-wrap: wrap; }
.gallery-label-input {
  width: 180px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-primary);
  padding: var(--spacing-6) var(--spacing-10);
  font: inherit;
  font-size: 0.9231rem;
}
.gallery-label-input:focus { border-color: var(--accent-primary); outline: none; }
.gallery-file { display: none; }
.gallery-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: var(--spacing-8); margin-top: var(--spacing-8); }
.gallery-cell { display: flex; flex-direction: column; gap: var(--spacing-4); }
.gallery-cell img { width: 100%; aspect-ratio: 1; display: block; object-fit: cover; border-radius: var(--radius-md); border: 1px solid var(--border-subtle); }
.gallery-tools { display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-4); }
.gallery-tools small { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-tertiary); font-size: 0.7692rem; }
.gallery-empty { grid-column: 1 / -1; color: var(--text-tertiary); font-size: 0.8462rem; }
</style>
