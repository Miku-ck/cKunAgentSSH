<template>
  <article class="channel-card">
    <header class="channel-head">
      <div class="channel-identity">
        <span class="status-dot" :class="qqStatus.state" aria-hidden="true"></span>
        <div>
          <h3>QQ 官方机器人</h3>
          <small>{{ personaLabel }} · WebSocket 长连接</small>
        </div>
      </div>
      <div class="channel-actions">
        <span class="channel-status" :class="qqStatus.state">{{ qqStateText }}</span>
        <button class="btn btn-sm" type="button" :aria-expanded="expanded" @click="expanded = !expanded">配置</button>
        <button
          v-if="qqStatus.state === 'offline' || qqStatus.state === 'error'"
          class="btn btn-sm btn-primary"
          type="button"
          :disabled="qqBusy || !qqConfig.app_id || !qqConfig.persona_id"
          @click="connectExisting"
        >
          连接
        </button>
        <button v-else class="btn btn-sm" type="button" :disabled="qqBusy" @click="disconnectQq">断开</button>
      </div>
    </header>

    <p v-if="qqStatus.detail" class="channel-detail">{{ qqStatus.detail }}</p>

    <form v-if="expanded" class="channel-form" @submit.prevent="saveAndConnect">
      <label class="field"><span>AppID</span><input v-model.trim="qqForm.app_id" maxlength="100" autocomplete="off" placeholder="QQ 开放平台的机器人 AppID" /></label>
      <label class="field"><span>AppSecret</span><input v-model="qqForm.app_secret" type="password" maxlength="500" autocomplete="new-password" :placeholder="qqConfig.has_secret ? '已保存；留空表示不修改' : '首次接入必填'" /></label>
      <label class="field"><span>当前人格</span><select v-model="qqForm.persona_id" :disabled="qqStatus.state !== 'offline'"><option value="" disabled>请选择人格</option><option v-for="p in personas" :key="p.id" :value="p.id">{{ p.name }}</option></select></label>
      <p class="form-note">群聊只响应 @机器人，私聊直接回复。切换人格后，QQ 会话会开启新的上下文。</p>
      <p class="form-note">凭据 {{ qqConfig.has_secret ? '已加密保存' : '尚未配置' }} · 会话上下文 {{ qqConfig.context_id ? qqConfig.context_id.slice(0, 8) : '保存后生成' }}</p>
      <div v-if="error" class="form-error">{{ error }}</div>
      <div class="channel-form-actions">
        <button type="submit" class="btn btn-primary" :disabled="qqBusy || !qqForm.app_id || !qqForm.persona_id">{{ qqBusy ? '处理中…' : '保存并连接' }}</button>
        <button type="button" class="btn" @click="expanded = false">收起</button>
      </div>
      <p class="privacy-note">AppSecret 只写入本地加密存储，配置接口不会回显。</p>
    </form>
  </article>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { botApi, type BotPersona, type BotQqConfig, type BotQqStatus } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'

const props = defineProps<{ personas: BotPersona[] }>()

const expanded = ref(false)
const qqConfig = ref<BotQqConfig>({ app_id: '', has_secret: false, persona_id: '', context_id: '' })
const qqStatus = ref<BotQqStatus>({ state: 'offline', detail: '' })
const qqForm = reactive({ app_id: '', app_secret: '', persona_id: '' })
const qqBusy = ref(false)
const error = ref('')
let timer: ReturnType<typeof setInterval> | undefined

const qqStateText = computed(() => ({ offline: '离线', connecting: '连接中', online: '在线', reconnecting: '重连中', error: '连接异常' })[qqStatus.value.state])
const personaLabel = computed(() => {
  const id = qqConfig.value.persona_id || qqForm.persona_id
  return props.personas.find(p => p.id === id)?.name || '未选择人格'
})

watch(() => props.personas, (rows) => {
  if (!qqForm.persona_id && rows.length) qqForm.persona_id = qqConfig.value.persona_id || rows[0].id
})

async function refreshQq(populate = false) {
  if (qqBusy.value) return
  try {
    const [config, status] = await Promise.all([botApi.getQqConfig(), botApi.getQqStatus()])
    qqConfig.value = config
    qqStatus.value = status
    if (populate) {
      qqForm.app_id = config.app_id
      qqForm.persona_id = config.persona_id || props.personas[0]?.id || ''
    }
  } catch (e) {
    error.value = toFriendlyMessage(e, 'QQ 状态读取失败')
  }
}

async function saveAndConnect() {
  qqBusy.value = true
  error.value = ''
  try {
    qqConfig.value = await botApi.saveQqConfig({ app_id: qqForm.app_id, ...(qqForm.app_secret ? { app_secret: qqForm.app_secret } : {}), persona_id: qqForm.persona_id })
    qqForm.app_secret = ''
    qqStatus.value = await botApi.connectQq()
  } catch (e) {
    error.value = toFriendlyMessage(e, 'QQ 连接失败')
    await refreshQq()
  } finally {
    qqBusy.value = false
  }
}

async function connectExisting() {
  qqBusy.value = true
  error.value = ''
  try {
    qqStatus.value = await botApi.connectQq()
  } catch (e) {
    error.value = toFriendlyMessage(e, 'QQ 连接失败')
    await refreshQq()
  } finally {
    qqBusy.value = false
  }
}

async function disconnectQq() {
  qqBusy.value = true
  error.value = ''
  try {
    qqStatus.value = await botApi.disconnectQq()
  } catch (e) {
    error.value = toFriendlyMessage(e, '断开失败')
  } finally {
    qqBusy.value = false
  }
}

onMounted(() => {
  void refreshQq(true)
  timer = setInterval(() => { void refreshQq() }, 5000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.channel-card { display: flex; flex-direction: column; gap: var(--spacing-12); padding: var(--spacing-16); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); }
.channel-head { display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-16); flex-wrap: wrap; }
.channel-identity { display: flex; align-items: center; gap: var(--spacing-10); min-width: 0; }
.channel-identity h3 { margin: 0 0 var(--spacing-2); font-size: 1.0769rem; }
.channel-identity small { color: var(--text-tertiary); font-size: 0.8462rem; }
.status-dot { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; background: var(--text-tertiary); }
.status-dot.online { background: var(--status-success-text); box-shadow: 0 0 0 3px color-mix(in srgb, var(--status-success-text) 18%, transparent); }
.status-dot.connecting, .status-dot.reconnecting { background: var(--status-warn-text); }
.status-dot.error { background: var(--danger-text); }
.channel-actions { display: flex; align-items: center; gap: var(--spacing-8); flex-wrap: wrap; }
.channel-status { color: var(--text-secondary); font-size: 0.9231rem; }
.channel-status.online { color: var(--status-success-text); }
.channel-status.connecting, .channel-status.reconnecting { color: var(--status-warn-text); }
.channel-status.error { color: var(--danger-text); }
.channel-detail { margin: 0; padding: var(--spacing-8) var(--spacing-10); border-radius: var(--radius-sm); background: var(--bg-hover); color: var(--danger-text); font-size: 0.9231rem; line-height: 1.6; overflow-wrap: anywhere; }
.channel-form { display: flex; flex-direction: column; gap: var(--spacing-14); padding-top: var(--spacing-12); border-top: 1px solid var(--border-subtle); }
.field { display: flex; flex-direction: column; gap: 7px; color: var(--text-secondary); font-size: 0.9231rem; }
.field input, .field select { width: 100%; box-sizing: border-box; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-base); color: var(--text-primary); padding: 9px var(--spacing-10); font: inherit; }
.field input:focus, .field select:focus { border-color: var(--accent-primary); outline: none; }
.field input:disabled, .field select:disabled { opacity: .6; cursor: not-allowed; }
.form-note, .privacy-note { margin: 0; color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1.7; }
.form-error { margin: 0; color: var(--danger-text); font-size: 0.9231rem; }
.channel-form-actions { display: flex; gap: var(--spacing-8); }
</style>
