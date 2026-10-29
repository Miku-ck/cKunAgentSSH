<template>
  <article class="channel-card">
    <header class="channel-head">
      <div class="channel-identity">
        <span class="status-dot" :class="state" aria-hidden="true"></span>
        <div class="channel-titles">
          <h3>{{ title }}</h3>
          <small>{{ personaLabel }} · {{ transportLabel }}</small>
        </div>
      </div>
      <div class="channel-actions">
        <span v-if="channel" class="channel-status" :class="state">{{ stateText }}</span>
        <button class="btn btn-sm" type="button" :aria-expanded="expanded" @click="toggleForm">
          {{ channel ? '配置' : '填写' }}
        </button>
        <button
          v-if="channel && (state === 'offline' || state === 'error')"
          class="btn btn-sm btn-primary"
          type="button"
          :disabled="busy"
          @click="connect"
        >
          连接
        </button>
        <button
          v-else-if="channel"
          class="btn btn-sm"
          type="button"
          :disabled="busy"
          @click="disconnect"
        >
          断开
        </button>
      </div>
    </header>

    <p v-if="channel && status.detail" class="channel-detail">{{ status.detail }}</p>

    <form v-if="expanded" class="channel-form" @submit.prevent="submit">
      <label v-if="!channel" class="field">
        <span>平台</span>
        <select v-model="form.provider">
          <option value="qq">QQ</option>
          <option value="wechat">微信</option>
        </select>
      </label>
      <label class="field">
        <span>名称</span>
        <input v-model.trim="form.display_name" maxlength="40" autocomplete="off" placeholder="这台机器人在界面上叫什么" />
      </label>
      <label class="field">
        <span>{{ isWechat ? 'APPID' : 'AppID' }}</span>
        <input
          v-model.trim="form.app_id"
          maxlength="100"
          autocomplete="off"
          :placeholder="isWechat ? '微信对话开放平台的 APPID' : 'QQ 开放平台的机器人 AppID'"
        />
      </label>
      <label class="field">
        <span>{{ isWechat ? 'Token' : 'AppSecret' }}</span>
        <input
          v-model="form.app_secret"
          type="password"
          maxlength="500"
          autocomplete="new-password"
          :placeholder="hasSecret ? '已保存；留空表示不修改' : '首次接入必填'"
        />
      </label>
      <label v-if="isWechat" class="field">
        <span>EncodingAESKey</span>
        <input
          v-model="form.extra_secret"
          type="password"
          maxlength="500"
          autocomplete="new-password"
          :placeholder="hasExtraSecret ? '已保存；留空表示不修改' : '开放API 页的 43 位密钥'"
        />
      </label>
      <div v-if="isWechat" class="webhook-block">
        <label class="field">
          <span>公网地址</span>
          <input
            v-model.trim="form.webhook_base"
            maxlength="300"
            autocomplete="off"
            placeholder="https://xxx.trycloudflare.com"
            @blur="saveWebhook"
          />
        </label>
        <label class="field">
          <span>回调地址</span>
          <div class="copy-row">
            <input :value="callbackUrl" readonly :placeholder="channel ? '先填公网地址' : '保存并连接后生成'" />
            <button type="button" class="btn btn-sm" :disabled="!canCopyCallback" @click="copyCallback">
              {{ copied ? '已复制' : '复制' }}
            </button>
          </div>
          <p v-if="channel && !baseUrl" class="webhook-warning">
            还没有公网地址，上面只是路径；微信只收完整 https 地址，直接粘贴会提示「请输入正确的接入地址」。
          </p>
        </label>
        <ol class="webhook-steps">
          <li>用隧道把本机后端映射到公网 HTTPS。</li>
          <li>把公网地址填到上面。</li>
          <li>把回调地址粘到「发布管理 → 应用绑定 → 开放API → 回调地址」。</li>
        </ol>
      </div>
      <label class="field">
        <span>当前人格</span>
        <select v-model="form.persona_id" :disabled="state !== 'offline'">
          <option value="" disabled>请选择人格</option>
          <option v-for="p in personas" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
      </label>
      <p class="form-note">一个机器人绑一个人格，在任何群和私聊里都是它。切换人格后，这个机器人在各群会另起一条会话，旧会话保留可读。</p>
      <p class="form-note">凭据 {{ hasSecret ? '已加密保存' : '尚未配置' }}<template v-if="channel"> · 会话 {{ channel.conversation_count }} 条</template></p>
      <div v-if="error" class="form-error">{{ error }}</div>
      <div class="channel-form-actions">
        <button type="submit" class="btn btn-primary" :disabled="busy || !canSubmit">
          {{ busy ? '处理中…' : (channel ? '保存修改' : '保存并连接') }}
        </button>
        <button v-if="channel" type="button" class="btn" @click="collapse">收起</button>
        <button v-else type="button" class="btn" @click="emit('discard')">取消</button>
        <button
          v-if="channel"
          class="btn btn-danger"
          type="button"
          :disabled="busy"
          @click="remove"
        >
          删除机器人
        </button>
      </div>
      <p class="privacy-note">
        {{ isWechat ? 'Token 与 EncodingAESKey 只写入本地加密存储，配置接口不会回显。' : 'AppSecret 只写入本地加密存储，配置接口不会回显。' }}
      </p>
    </form>
  </article>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { botApi, type BotChannel, type BotChannelStatus, type BotPersona } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'
import { useConfirm } from '@/composables/useConfirm'

const props = defineProps<{
  channel: BotChannel | null
  personas: BotPersona[]
  webhookBase: string
}>()
const emit = defineEmits<{ refresh: []; created: []; discard: []; webhook: [value: string] }>()

const { confirm } = useConfirm()

const OFFLINE: BotChannelStatus = { state: 'offline', detail: '' }
const STATE_TEXT: Record<BotChannelStatus['state'], string> = {
  offline: '离线',
  connecting: '连接中',
  online: '在线',
  reconnecting: '重连中',
  error: '连接异常',
}

const expanded = ref(!props.channel)
const busy = ref(false)
const error = ref('')
const form = reactive({
  provider: 'qq' as BotChannel['provider'],
  display_name: '',
  app_id: '',
  app_secret: '',
  extra_secret: '',
  persona_id: '',
  webhook_base: '',
})
/** 已保存的密钥是否存在：只跟服务端同步，不拿用户刚输入的内容冒充。 */
const savedSecret = ref(false)
const savedExtraSecret = ref(false)
const copied = ref(false)

const status = computed(() => props.channel?.status ?? OFFLINE)
const state = computed(() => status.value.state)
const stateText = computed(() => STATE_TEXT[state.value])
const provider = computed(() => props.channel?.provider ?? form.provider)
const isWechat = computed(() => provider.value === 'wechat')
const transportLabel = computed(() => (isWechat.value ? '回调推送 · HTTPS' : 'WebSocket 长连接'))
const title = computed(() => props.channel?.display_name || '新增机器人')
const personaLabel = computed(() => {
  const id = props.channel?.persona_id || form.persona_id
  return props.personas.find(p => p.id === id)?.name || '未选择人格'
})
const hasSecret = computed(() => props.channel ? savedSecret.value : !!form.app_secret)
const hasExtraSecret = computed(() =>
  props.channel ? savedExtraSecret.value : !!form.extra_secret
)
const canSubmit = computed(() =>
  !!form.display_name &&
  !!form.app_id &&
  !!form.persona_id &&
  // 微信首次接入要三项凭据齐全；QQ 的首次校验仍交给服务端报错。
  (!isWechat.value || !!props.channel || (!!form.app_secret && !!form.extra_secret))
)
const baseUrl = computed(() => (form.webhook_base || props.webhookBase).replace(/\/+$/, ''))
const callbackUrl = computed(() => {
  if (!props.channel) return ''
  const path = `/bot/wechat/${props.channel.id}`
  return baseUrl.value ? `${baseUrl.value}${path}` : path
})
/** 没有公网地址时这条只是路径，微信只收完整 http(s) 地址，复制出去必然报错。 */
const canCopyCallback = computed(() => !!props.channel && !!baseUrl.value)

watch(
  () => props.channel,
  channel => {
    if (!expanded.value) fill(channel)
    savedSecret.value = !!channel?.has_secret
    savedExtraSecret.value = !!channel?.has_extra_secret
  },
  { immediate: true }
)

watch(
  () => props.webhookBase,
  value => {
    if (!form.webhook_base) form.webhook_base = value
  },
  { immediate: true }
)

function fill(channel: BotChannel | null) {
  form.provider = channel?.provider || 'qq'
  form.display_name = channel?.display_name || ''
  form.app_id = channel?.app_id || ''
  form.app_secret = ''
  form.extra_secret = ''
  form.persona_id = channel?.persona_id || props.personas[0]?.id || ''
  form.webhook_base = props.webhookBase || ''
}

function toggleForm() {
  expanded.value = !expanded.value
  // 展开时按服务端当前值填一次，之后轮询不再覆盖正在编辑的内容。
  if (expanded.value) fill(props.channel)
}

function collapse() {
  expanded.value = false
  fill(props.channel)
}

function secretPayload() {
  const payload: { app_secret?: string; extra_secret?: string } = {}
  if (form.app_secret) payload.app_secret = form.app_secret
  if (form.extra_secret) payload.extra_secret = form.extra_secret
  return payload
}

/** 公网地址是全局设置：失焦即保存，卡片只负责把改后的值报给页面。 */
async function saveWebhook() {
  if (!isWechat.value || form.webhook_base === props.webhookBase) return
  try {
    const saved = await botApi.setWebhook(form.webhook_base)
    form.webhook_base = saved.base_url
    emit('webhook', saved.base_url)
  } catch (e) {
    error.value = toFriendlyMessage(e, '公网地址保存失败')
  }
}

async function copyCallback() {
  if (!callbackUrl.value) return
  try {
    await navigator.clipboard.writeText(callbackUrl.value)
    copied.value = true
    window.setTimeout(() => { copied.value = false }, 1500)
  } catch {
    error.value = '复制失败，请手动选中复制'
  }
}

async function submit() {
  busy.value = true
  error.value = ''
  try {
    if (isWechat.value && form.webhook_base !== props.webhookBase) {
      const saved = await botApi.setWebhook(form.webhook_base)
      form.webhook_base = saved.base_url
      emit('webhook', saved.base_url)
    }
    if (props.channel) {
      await botApi.updateChannel(props.channel.id, {
        display_name: form.display_name,
        app_id: form.app_id,
        persona_id: form.persona_id,
        ...secretPayload(),
      })
      if (form.app_secret) savedSecret.value = true
      if (form.extra_secret) savedExtraSecret.value = true
      form.app_secret = ''
      form.extra_secret = ''
      expanded.value = false
      emit('refresh')
    } else {
      const created = await botApi.createChannel({
        provider: form.provider,
        display_name: form.display_name,
        app_id: form.app_id,
        persona_id: form.persona_id,
        app_secret: form.app_secret,
        ...(form.provider === 'wechat' ? { extra_secret: form.extra_secret } : {}),
      })
      await botApi.connectChannel(created.id)
      emit('created')
    }
  } catch (e) {
    error.value = toFriendlyMessage(e, props.channel ? '保存失败' : '连接失败')
    emit('refresh')
  } finally {
    busy.value = false
  }
}

async function connect() {
  if (!props.channel) return
  busy.value = true
  error.value = ''
  try {
    await botApi.connectChannel(props.channel.id)
  } catch (e) {
    error.value = toFriendlyMessage(e, '连接失败')
  } finally {
    busy.value = false
    emit('refresh')
  }
}

async function disconnect() {
  if (!props.channel) return
  busy.value = true
  error.value = ''
  try {
    await botApi.disconnectChannel(props.channel.id)
  } catch (e) {
    error.value = toFriendlyMessage(e, '断开失败')
  } finally {
    busy.value = false
    emit('refresh')
  }
}

async function remove() {
  const channel = props.channel
  if (!channel) return
  const history = channel.conversation_count
  const ok = await confirm({
    message: history
      ? `删除「${channel.display_name}」？它的 ${history} 条会话与聊天记录会一并删除，无法恢复。`
      : `删除「${channel.display_name}」？`,
    danger: true,
  })
  if (!ok) return
  busy.value = true
  try {
    await botApi.deleteChannel(channel.id)
    emit('refresh')
  } catch (e) {
    error.value = toFriendlyMessage(e, '删除失败')
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.channel-card { display: flex; flex-direction: column; gap: var(--spacing-12); padding: var(--spacing-16); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); }
.channel-head { display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-16); flex-wrap: wrap; }
.channel-identity { display: flex; align-items: center; gap: var(--spacing-10); min-width: 0; }
.channel-titles { min-width: 0; }
.channel-identity h3 { margin: 0 0 var(--spacing-2); font-size: 1.0769rem; overflow-wrap: anywhere; }
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
.webhook-block { display: flex; flex-direction: column; gap: var(--spacing-12); padding: var(--spacing-12); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-base); }
.copy-row { display: flex; gap: var(--spacing-8); }
.copy-row input { flex: 1; min-width: 0; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.8462rem; }
.webhook-steps { margin: 0; padding-left: 1.3em; color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1.7; }
.webhook-warning { margin: 0; color: var(--status-warn-text); font-size: 0.9231rem; line-height: 1.7; }
.form-note, .privacy-note { margin: 0; color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1.7; }
.form-error { margin: 0; color: var(--danger-text); font-size: 0.9231rem; }
.channel-form-actions { display: flex; gap: var(--spacing-8); flex-wrap: wrap; }
</style>
