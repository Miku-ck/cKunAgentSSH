<template>
  <Teleport to="body" :disabled="embedded">
  <div :class="embedded ? 'embed-root' : 'overlay show'" @click.self="tryClose">
    <div
      class="dialog"
      :class="{ embedded }"
      :role="embedded ? undefined : 'dialog'"
      :aria-modal="embedded ? undefined : 'true'"
    >
      <div v-if="!embedded" class="dlg-head">
        <h2>{{ isNew ? '添加连接' : '修改连接' }}</h2>
        <button class="dlg-x" aria-label="关闭" :disabled="saving" @click="tryClose">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="dlg-body">
        <div class="form-row">
          <div class="form-group flex-1">
            <label for="acc-name">连接名称</label>
            <input id="acc-name" v-model.trim="form.name" placeholder="留空则用地址">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group flex-2">
            <label for="acc-host">服务器</label>
            <input id="acc-host" v-model.trim="form.host" placeholder="sg-lab.example.net">
          </div>
          <div class="form-group flex-1">
            <label for="acc-port">端口</label>
            <input id="acc-port" v-model.number="form.port" inputmode="numeric">
          </div>
        </div>
        <div v-if="bad.port" class="form-error">{{ portErrText }}</div>
        <div class="form-row">
          <div class="form-group flex-1">
            <label for="acc-user">用户名</label>
            <input id="acc-user" v-model.trim="form.user">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group flex-1">
            <label for="acc-pass">口令</label>
            <input id="acc-pass" v-model="form.password" type="password" :placeholder="isNew ? '' : '留空 = 保留已存口令'">
          </div>
        </div>

        <div class="advanced-section">
          <button class="advanced-toggle" type="button" @click="advOpen = !advOpen">
            <svg class="toggle-arrow" :class="{ expanded: advOpen }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="12" height="12"><polyline points="9 18 15 12 9 6"/></svg>
            高级配置
          </button>
          <div v-if="advOpen" class="advanced-body">
            <div class="form-row">
              <div class="form-group flex-1">
                <label for="acc-ca">CA 证书</label>
                <textarea id="acc-ca" v-model.trim="form.caPem" rows="3" placeholder="粘贴 .crt（PEM）。留空则只按下面的指纹信任"></textarea>
              </div>
            </div>
            <div class="form-row">
              <div class="form-group flex-1">
                <label>SPKI 指纹</label>
                <span class="form-value mono">{{ spkiDisplay || '  ' }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-ghost" :disabled="saving" @click="tryClose">取消</button>
        <button class="btn btn-primary" :disabled="!canSave || saving" @click="save">{{ isNew ? '添加' : '保存' }}</button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, reactive, ref, onMounted, onUnmounted } from 'vue'
import { useAccessStore } from '@/stores/access'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'
import { isPort, portRangeError } from '../../../shared/port-core'

/** 越界提示与主进程 gateway-store 同一句话（判据源 shared/port-core.ts） */
const portErrText = portRangeError()

const props = defineProps<{
  rec?: GatewayRecord
  /** 嵌入统一新建弹窗：不渲染遮罩/标题栏与类型页签，Esc 交给宿主 */
  embedded?: boolean
}>()
const emit = defineEmits<{ close: []; saved: [rec: GatewayRecord, submittedPassword: boolean] }>()

const accessStore = useAccessStore()
const toast = useToastStore()
const advOpen = ref(false)
const saving = ref(false)
const isNew = computed(() => !props.rec?.id)

/** 指纹由主进程的 TOFU 确认写入，表单只读展示；父组件关掉弹窗要等在途保存落地 */
const spkiDisplay = computed(() => props.rec?.trustedSpki || '')
function tryClose(): void {
  if (!saving.value) emit('close')
}

// Escape 关闭（在途保存时不关，见 tryClose）
function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') tryClose()
}
onMounted(() => {
  if (!props.embedded) window.addEventListener('keydown', handleKeydown)
})
onUnmounted(() => window.removeEventListener('keydown', handleKeydown))

// 宿主关闭入口仍走 saving 闸门
defineExpose({ requestClose: tryClose })

const form = reactive({
  id: props.rec?.id || '',
  name: props.rec?.name || '',
  host: props.rec?.host || '',
  port: props.rec?.port || 4433,
  caPem: props.rec?.caPem || '',
  user: props.rec?.user || '',
  // 编辑时留空 = 保留原口令（store 侧同一口径），所以初值必须是空而不是解密后的口令
  password: '',
})

const bad = computed(() => ({
  port: !isPort(form.port),
}))
const canSave = computed(() => !!form.host && !!form.user && !bad.value.port)

async function save(): Promise<void> {
  if (!canSave.value || saving.value) return
  const payload: Partial<GatewayRecord> & { password?: string } = {
    ...form,
    id: form.id,
    name: form.name || form.host,
  }
  if (!payload.password) delete payload.password
  if (!payload.caPem) delete payload.caPem
  const submittedPassword = !!form.password
  saving.value = true
  try {
    const saved = await accessStore.save(payload as GatewayRecord)
    emit('saved', saved, submittedPassword)
  } catch (err) {
    toast.error(toFriendlyMessage(err, '保存失败'))
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
/* 表单排布与 ConnectionModal 对齐：标签在上、32px 输入框、同一套高级选项与页脚。
   z-index 跟同目录的 ConnectionModal 同层：Teleport 到 body 后它已不在页面容器里。 */
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(2px);
}
.dialog {
  display: flex;
  flex-direction: column;
  width: 560px;
  max-height: 85vh;
  overflow: hidden;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}

/* 嵌入统一新建弹窗：骨架交给宿主，正文滚动、页脚固定 */
.embed-root {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.dialog.embedded {
  flex: 1;
  width: 100%;
  max-height: none;
  min-height: 0;
  background: transparent;
  border: none;
  border-radius: 0;
  box-shadow: none;
}

.dialog.embedded .dlg-body {
  flex: 1;
  min-height: 0;
}

.dlg-head {
  height: 44px;
  display: flex;
  align-items: center;
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.dlg-head h2 { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.dlg-x {
  width: 24px;
  height: 24px;
  margin-left: auto;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
}
.dlg-x:hover { background: var(--bg-hover); color: var(--text-secondary); }

.dlg-body {
  padding: 8px 16px 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
}

.form-row { display: flex; gap: 12px; }
.form-group { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.form-group.flex-1 { flex: 1; }
.form-group.flex-2 { flex: 2; }

.form-group label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.form-group input,
.form-group textarea {
  width: 100%;
  box-sizing: border-box;
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  transition: border-color var(--transition-fast);
}

.form-group input {
  height: 32px;
  padding: 0 10px;
}

.form-group textarea {
  padding: 6px 10px;
  resize: vertical;
}

.form-group input:focus,
.form-group textarea:focus { border-color: var(--accent-primary); }

.form-group input::placeholder,
.form-group textarea::placeholder { color: var(--text-tertiary); }

.mono { font-family: var(--font-mono); }
.form-value { font-size: 12px; color: var(--text-secondary); word-break: break-all; line-height: 1.5; }

.form-error {
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--danger-text);
  background: color-mix(in srgb, var(--accent-red) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-red) 20%, transparent);
}

.advanced-section {
  border-top: 1px solid var(--border-subtle);
  padding-top: 8px;
}
.advanced-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--font-sans);
  font-size: 12px;
  cursor: pointer;
}
.advanced-toggle:hover { color: var(--text-primary); }
.toggle-arrow { transition: transform var(--transition-fast); }
.toggle-arrow.expanded { transform: rotate(90deg); }
.advanced-body { display: flex; flex-direction: column; gap: 12px; margin-top: 8px; }

.modal-footer {
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 0 16px;
  border-top: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
</style>
