<template>
  <Teleport to="body">
    <div class="tunnel-overlay" @click.self="emit('close')">
      <div ref="dialogRef" class="modal" role="dialog" aria-modal="true" aria-labelledby="tunnel-modal-title" tabindex="-1">
        <button class="modal-close" @click="emit('close')" aria-label="关闭">✕</button>
        <h2 id="tunnel-modal-title">{{ isEdit ? '编辑隧道' : '新建隧道' }}</h2>

        <div class="field">
          <label>类型</label>
          <div class="seg">
            <button v-for="t in TYPES" :key="t.key" :class="{ on: form.type === t.key }" @click="pick(t.key)">
              <span class="mini" :class="t.key">{{ t.key.toUpperCase() }}</span>{{ t.label }}
            </button>
          </div>
        </div>

        <div class="field">
          <label>连接</label>
          <select v-model="form.connectionId">
            <option v-for="c in sshStore.connections" :key="c.id" :value="c.id">
              {{ c.name }} — {{ c.username }}@{{ c.host }}
            </option>
          </select>
          <div v-if="sshStore.connections.length === 0" class="err">请先在远程终端页创建 SSH 连接</div>
        </div>

        <div class="field">
          <label>名称（可选）</label>
          <input v-model="form.name" type="text" :placeholder="`留空则自动生成，如「${autoNameHint}」`">
        </div>

        <!-- local / dynamic：本地监听 -->
        <div v-if="form.type !== 'remote'" class="field-grid">
          <div class="field">
            <label>本地监听地址</label>
            <input v-model="form.listenHost" type="text" :disabled="form.type === 'dynamic'">
            <div v-if="form.type === 'dynamic'" class="hint">D 类固定 127.0.0.1，不对局域网开放</div>
          </div>
          <div class="field" :class="{ bad: !!portErr.listenPort }">
            <label>本地监听端口</label>
            <input v-model="form.listenPort" type="number" @input="touchPort('listenPort')">
            <div class="err">{{ portErr.listenPort }}</div>
          </div>
        </div>

        <!-- remote：远端绑定 -->
        <template v-if="form.type === 'remote'">
          <div class="field-grid">
            <div class="field">
              <label>远端绑定地址</label>
              <input v-model="form.remoteBindHost" type="text" @input="touchBindWarn">
            </div>
            <div class="field" :class="{ bad: !!portErr.bindPort }">
              <label>远端绑定端口</label>
              <input v-model="form.bindPort" type="number" @input="touchPort('bindPort')">
              <div class="err">{{ portErr.bindPort }}</div>
            </div>
          </div>
          <div v-if="bindWarn" class="confirm-box">
            <span>⚠</span>
            <span>绑定 0.0.0.0 将把该端口暴露给远端所在网段的任意主机，请确认远端防火墙策略。</span>
          </div>
        </template>

        <!-- local / remote：目标 -->
        <div v-if="form.type !== 'dynamic'" class="field-grid">
          <div class="field" :class="{ bad: !!dstHostErr }">
            <label>{{ form.type === 'remote' ? '本机目标主机（远端回连到这）' : '目标主机' }}</label>
            <input v-model="form.dstHost" type="text" @input="dstHostErr = !form.dstHost.trim() ? '必须填写目标主机' : ''">
            <div v-if="!dstHostErr && form.type !== 'remote'" class="hint">从远端视角解析（不是你本机）</div>
            <div class="err">{{ dstHostErr }}</div>
          </div>
          <div class="field" :class="{ bad: !!portErr.dstPort }">
            <label>目标端口</label>
            <input v-model="form.dstPort" type="number" @input="touchPort('dstPort')">
            <div class="err">{{ portErr.dstPort }}</div>
          </div>
        </div>

        <div class="modal-foot">
          <button class="btn" @click="emit('close')">取消</button>
          <button class="btn btn-primary" :disabled="!canSave" @click="save">
            {{ bindWarn ? '确认风险并保存' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'
import { useTunnelStore } from '@/stores/tunnel'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'
import { isPort, portRangeError } from '../../../shared/port-core'

const props = defineProps<{ rule?: TunnelRule }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const tunnelStore = useTunnelStore()
const sshStore = useSSHStore()
const toast = useToastStore()

const TYPES = [
  { key: 'local', label: '本地转发' },
  { key: 'remote', label: '远程转发' },
  { key: 'dynamic', label: '动态 SOCKS5' },
] as const
type FormType = TunnelRule['type']

const isEdit = computed(() => !!props.rule)

const form = reactive({
  type: (props.rule?.type ?? 'local') as FormType,
  name: props.rule?.name ?? '',
  connectionId: props.rule?.connectionId ?? '',
  listenHost: props.rule?.listenHost ?? '127.0.0.1',
  listenPort: props.rule?.listenPort?.toString() ?? '',
  remoteBindHost: props.rule?.remoteBindHost ?? '127.0.0.1',
  bindPort: props.rule?.bindPort?.toString() ?? '',
  dstHost: props.rule?.dstHost ?? '',
  dstPort: props.rule?.dstPort?.toString() ?? '',
})

const portErr = reactive<Record<'listenPort' | 'bindPort' | 'dstPort', string>>({
  listenPort: '', bindPort: '', dstPort: '',
})
const dstHostErr = ref('')
const bindWarn = ref(false)
const saving = ref(false)

const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef)

// 自绘遮罩的弹窗自己管 Esc（优先级链不在共享件里），与右上角 ✕ 同一出口
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}
onMounted(() => {
  activate()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  deactivate()
})

// 编辑弹窗预填时对已合法字段先做一次静默校验，避免打开即红框（新建时留空交给提交前校验）
function validateSilently(): void {
  if (form.type !== 'remote') validatePort('listenPort')
  if (form.type !== 'dynamic') { validatePort('dstPort'); dstHostErr.value = form.dstHost.trim() ? '' : '必须填写目标主机' }
  if (form.type === 'remote') { validatePort('bindPort'); touchBindWarn() }
}
if (props.rule) validateSilently()

function pick(t: FormType): void {
  form.type = t
  // 类型联动：切 dynamic 时钳死监听地址（安全边界：不对局域网开放）
  if (t === 'dynamic') form.listenHost = '127.0.0.1'
  validateSilently()
}

function touchBindWarn(): void {
  bindWarn.value = form.remoteBindHost === '0.0.0.0' || form.remoteBindHost === '::'
}

function validatePort(key: 'listenPort' | 'bindPort' | 'dstPort'): boolean {
  const raw = form[key]
  const v = Number(raw)
  const ok = raw !== '' && isPort(v)
  portErr[key] = ok ? '' : portRangeError()
  return ok
}
function touchPort(key: 'listenPort' | 'bindPort' | 'dstPort'): void {
  validatePort(key)
}

const autoNameHint = computed(() => {
  if (form.type === 'local') return `L ${form.listenPort || 8080} → ${form.dstHost || 'db.internal'}:${form.dstPort || 5432}`
  if (form.type === 'remote') return `R ${form.remoteBindHost || '127.0.0.1'}:${form.bindPort || 8080} → ${form.dstHost || 'localhost'}:${form.dstPort || 80}`
  return `D SOCKS5 127.0.0.1:${form.listenPort || 1080}`
})

const canSave = computed(() => {
  if (saving.value) return false
  if (!form.connectionId) return false
  if (sshStore.connections.length === 0) return false
  if (form.type !== 'remote' && portErr.listenPort) return false
  if (form.type === 'remote' && portErr.bindPort) return false
  if (form.type !== 'dynamic' && (portErr.dstPort || dstHostErr.value)) return false
  return true
})

async function save(): Promise<void> {
  // 提交前统一跑一遍校验（用户可能没碰过某些输入框）
  if (form.type !== 'remote' && !validatePort('listenPort')) return
  if (form.type === 'remote' && !validatePort('bindPort')) return
  if (form.type !== 'dynamic') {
    if (!validatePort('dstPort')) return
    dstHostErr.value = form.dstHost.trim() ? '' : '必须填写目标主机'
    if (dstHostErr.value) return
  }

  // 本机端口预检（remote 的绑定端口在服务端，本地探不了 → 启动时由 forwardIn 报错）
  if (form.type !== 'remote') {
    const api = window.electronAPI
    if (api) {
      const probe = await api.tunnelCheckPort(form.type === 'dynamic' ? '127.0.0.1' : form.listenHost || '127.0.0.1', Number(form.listenPort))
      if (!probe.ok) {
        portErr.listenPort = probe.message || '端口不可用'
        return
      }
    }
  }

  const now = Date.now()
  const rule: TunnelRule = {
    id: props.rule?.id ?? '',
    name: form.name.trim(),
    connectionId: form.connectionId,
    type: form.type,
    listenHost: form.type === 'dynamic' ? '127.0.0.1' : form.listenHost || undefined,
    listenPort: form.type !== 'remote' ? Number(form.listenPort) : undefined,
    remoteBindHost: form.type === 'remote' ? form.remoteBindHost || '127.0.0.1' : undefined,
    bindPort: form.type === 'remote' ? Number(form.bindPort) : undefined,
    dstHost: form.type !== 'dynamic' ? form.dstHost.trim() : undefined,
    dstPort: form.type !== 'dynamic' ? Number(form.dstPort) : undefined,
    autoStart: props.rule?.autoStart ?? false,
    createdAt: props.rule?.createdAt ?? now,
    updatedAt: now,
  }

  saving.value = true
  try {
    await tunnelStore.save(rule)
    toast.success(isEdit.value ? '隧道已更新' : '隧道已创建')
    emit('saved')
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '保存失败'))
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
/* 全量消费 variables.css 令牌，零硬编码色值 */
.tunnel-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  width: 520px;
  max-width: 90vw;
  max-height: 80vh;
  overflow-y: auto;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
  box-shadow: var(--shadow-lg);
  position: relative;
}
.modal h2 {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 16px;
}
.modal-close {
  position: absolute;
  top: 12px;
  right: 12px;
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 16px;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  transition: background var(--transition-fast), color var(--transition-fast);
}
.modal-close:hover { background: var(--bg-hover); color: var(--text-primary); }
.field {
  margin-bottom: 14px;
}
.field > label {
  display: block;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 5px;
}
.field input,
.field select {
  width: 100%;
  height: 30px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  padding: 0 9px;
  font-size: 12.5px;
  font-family: var(--font-sans);
  outline: none;
}
.field input:focus,
.field select:focus { border-color: var(--accent-primary); }
.field input:disabled { opacity: 0.5; cursor: not-allowed; }
.field .hint {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 4px;
}
.field .err {
  display: none;
  font-size: 11px;
  color: var(--danger-text);
  margin-top: 4px;
}
/* 内联校验错误：.bad 才显示（含无输入时兜底文案） */
.field.bad input { border-color: var(--accent-red); }
.field.bad .err { display: block; }
.field .err:empty { display: none; }
.field-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.seg {
  display: flex;
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.seg button {
  flex: 1;
  height: 30px;
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font-size: 12.5px;
  font-family: var(--font-sans);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: background var(--transition-fast);
}
.seg button + button { border-left: 1px solid var(--border-input); }
.seg button.on {
  background: var(--accent-indigo-bg);
  color: var(--text-primary);
}
.mini {
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: var(--radius-sm);
}
.mini.local {
  color: var(--tint-l, var(--accent-cyan));
  background: color-mix(in srgb, var(--tint-l, var(--accent-cyan)) 12%, transparent);
}
.mini.remote {
  color: var(--tint-r, var(--accent-purple));
  background: color-mix(in srgb, var(--tint-r, var(--accent-purple)) 14%, transparent);
}
.mini.dynamic {
  color: var(--tint-d, var(--accent-indigo));
  background: color-mix(in srgb, var(--tint-d, var(--accent-indigo)) 16%, transparent);
}

.confirm-box {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  font-size: 11.5px;
  color: var(--danger-text);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  margin-bottom: 14px;
}
.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
}
</style>
