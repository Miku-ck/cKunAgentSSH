<template>
  <div class="connection-page">
    <section class="connection-group" aria-labelledby="connection-channels">
      <div class="group-head">
        <h3 id="connection-channels">渠道</h3>
        <button v-if="!draftOpen" class="btn btn-sm btn-primary" type="button" @click="draftOpen = true">
          新增机器人
        </button>
      </div>

      <ChannelCard
        v-for="channel in channels"
        :key="channel.id"
        :channel="channel"
        :personas="personas"
        :webhook-base="webhookBase"
        @refresh="refresh"
        @webhook="onWebhook"
      />

      <ChannelCard
        v-if="draftOpen"
        :channel="null"
        :personas="personas"
        :webhook-base="webhookBase"
        @created="onCreated"
        @discard="draftOpen = false"
        @webhook="onWebhook"
      />

      <p v-if="!channels.length && !draftOpen && !error" class="empty-hint">
        还没有机器人：新增一个 QQ 或微信机器人，填好凭据并选一个要绑的人格。
      </p>
      <p v-if="error" class="page-error">{{ error }}</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import ChannelCard from './ChannelCard.vue'
import { botApi, type BotChannel, type BotPersona } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'

defineProps<{ personas: BotPersona[] }>()

const channels = ref<BotChannel[]>([])
const webhookBase = ref('')
const draftOpen = ref(false)
const error = ref('')
let timer: ReturnType<typeof setInterval> | undefined
let refreshing = false

/** 整页一次拉全部机器人和各自状态：卡片不再各轮各的，N 个机器人也只有 N 份数据、一份请求。 */
async function refresh() {
  if (refreshing) return
  refreshing = true
  try {
    const [list, webhook] = await Promise.all([botApi.listChannels(), botApi.getWebhook()])
    channels.value = list
    webhookBase.value = webhook.base_url
    error.value = ''
  } catch (e) {
    error.value = toFriendlyMessage(e, '机器人状态读取失败')
  } finally {
    refreshing = false
  }
}

function onWebhook(value: string) {
  webhookBase.value = value
}

function onCreated() {
  draftOpen.value = false
  void refresh()
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => { void refresh() }, 5000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.connection-page { flex: 1; min-height: 0; overflow-y: auto; padding: var(--spacing-24); }
.connection-group { display: flex; flex-direction: column; gap: var(--spacing-10); }
.group-head { display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-12); }
.group-head h3 { margin: 0; font-size: 1.0769rem; }
.empty-hint { margin: 0; color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1.7; }
.page-error { margin: 0; color: var(--danger-text); font-size: 0.9231rem; }
</style>
