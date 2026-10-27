<template>
  <Teleport to="body" :disabled="embedded">
  <div :class="embedded ? 'embed-root' : 'modal-overlay'">
    <div
      class="modal-dialog"
      :class="{ embedded }"
      :role="embedded ? undefined : 'dialog'"
      :aria-modal="embedded ? undefined : 'true'"
    >
      <div v-if="!embedded" class="modal-header">
        <h3>{{ isEdit ? '编辑连接' : '添加远程连接' }}</h3>
        <button class="modal-close" @click="handleOverlayClose">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>

      <div class="modal-body">
        <!-- 基本信息 -->
        <div class="form-row">
          <div class="form-group flex-1">
            <label>连接名称</label>
            <input v-model="form.name" type="text" placeholder="留空则自动生成 用户名@主机" />
          </div>
        </div>

        <div class="form-row two-col">
          <div class="form-group flex-2">
            <label>主机地址</label>
            <input v-model="form.host" type="text" placeholder="192.168.1.100 或 example.com" />
          </div>
          <div class="form-group flex-1">
            <label>端口</label>
            <input v-model.number="form.port" type="number" min="1" max="65535" placeholder="22" @blur="clampPort" />
          </div>
        </div>

        <div class="form-row">
          <div class="form-group flex-1">
            <label>用户名</label>
            <input v-model="form.username" type="text" placeholder="root" />
          </div>
        </div>

        <!-- 认证方式 -->
        <div class="form-row">
          <div class="form-group flex-1">
            <label>认证方式</label>
            <div class="auth-tabs">
              <button
                v-for="opt in authOptions"
                :key="opt.value"
                class="auth-tab"
                :class="{ active: form.authType === opt.value }"
                @click="form.authType = opt.value"
              >
                {{ opt.label }}
              </button>
            </div>
          </div>
        </div>

        <!-- 密码认证 -->
        <div v-if="form.authType === 'password'" class="form-row">
          <div class="form-group flex-1">
            <label>密码</label>
            <div class="input-with-btn">
              <input v-model="form.password" :type="showPassword ? 'text' : 'password'" :placeholder="passwordPlaceholder" />
              <button class="toggle-btn" @click="showPassword = !showPassword" :title="showPassword ? '隐藏' : '显示'">
                <svg v-if="showPassword" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
              </button>
            </div>
          </div>
        </div>

        <!-- 私钥认证 -->
        <template v-if="form.authType === 'privateKey'">
          <div class="form-row">
            <div class="form-group flex-1">
              <label>私钥文件路径</label>
              <div class="input-with-btn">
                <input v-model="form.privateKeyPath" type="text" placeholder="~/.ssh/id_rsa" />
                <button class="browse-btn" @click="selectKeyFile">浏览</button>
              </div>
            </div>
          </div>
          <div class="form-row">
            <div class="form-group flex-1">
              <label>密码短语 (可选)</label>
              <div class="input-with-btn">
                <input v-model="form.passphrase" :type="showPassphrase ? 'text' : 'password'" :placeholder="passphrasePlaceholder" />
                <button class="toggle-btn" @click="showPassphrase = !showPassphrase" :title="showPassphrase ? '隐藏' : '显示'">
                  <svg v-if="showPassphrase" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                  <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                </button>
              </div>
            </div>
          </div>
        </template>

        <!-- Agent 认证 -->
        <div v-if="form.authType === 'agent'" class="form-row">
          <div class="form-info">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="16" height="16"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>
            <span>使用 SSH Agent 进行认证,请确保 ssh-agent 已运行并添加了密钥。</span>
          </div>
        </div>

        <!-- 备注 -->
        <div class="form-row">
          <div class="form-group flex-1">
            <label>备注</label>
            <NoteInput v-model="form.note" placeholder="请输入备注..." :max-length="10" />
          </div>
        </div>

        <!-- 目标环境标记:驱动生产环境危险命令强制审批 -->
        <div class="form-row">
          <div class="form-group flex-1">
            <label>环境</label>
            <div class="env-select">
              <select v-model="form.environment">
                <option value="test">测试 (test)</option>
                <option value="prod">生产 (prod)</option>
              </select>
            </div>
            <div class="env-hint" v-if="form.environment === 'prod'">
              [注意] 生产环境：交互会话中危险命令会弹审批卡，由你现场确认；定时任务、工作流等无人值守运行无法弹卡，危险命令必须预先登记 allowlist。
            </div>
          </div>
        </div>

        <div class="form-row two-col">
          <div class="form-group flex-1">
            <label>连接超时 (秒)</label>
            <input v-model.number="form.readyTimeout" type="number" min="1" max="120" placeholder="15" />
          </div>
          <div class="form-group flex-1">
            <label>心跳间隔 (秒)</label>
            <input v-model.number="form.keepaliveInterval" type="number" min="0" max="120" placeholder="10" />
          </div>
        </div>

        <div class="form-row">
          <label class="checkbox-row">
            <input type="checkbox" v-model="form.connectNow" />
            <span>保存后立即连接</span>
          </label>
        </div>

        <!-- 测试结果 -->
        <div v-if="testResult" class="test-result" :class="testResult.ok ? 'success' : 'fail'">
          <svg v-if="testResult.ok" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="20 6 9 17 4 12"/></svg>
          <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
          <span>{{ testResult.ok ? `连接成功 (${testResult.latencyMs}ms)` : `连接失败: ${testResult.message}` }}</span>
        </div>

        <!-- 错误信息 -->
        <div v-if="errorMsg" class="form-error">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
          {{ errorMsg }}
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn btn-ghost" :disabled="saving || testing" @click="handleOverlayClose">取消</button>
        <button class="btn btn-test" :disabled="testing" @click="handleTest">
          <svg v-if="testing" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
          <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          {{ testing ? '测试中...' : '测试连接' }}
        </button>
        <button class="btn btn-primary" :disabled="saving || testing" @click="handleSave">
          {{ saving ? '保存中...' : (isEdit ? '保存' : '添加') }}
        </button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, reactive, watch, onMounted, onUnmounted } from 'vue'
import NoteInput from './NoteInput.vue'
import { connNote, useSSHStore } from '@/stores/ssh'
import type { SSHConnection } from '@/stores/ssh'
import { useSettingsStore } from '@/stores/settings'
import { useConfirm } from '@/composables/useConfirm'
import { toFriendlyMessage } from '@/api/client'
import { isPort, PORT_MAX } from '../../../shared/port-core'

const props = defineProps<{
  editData?: SSHConnection
  /** 嵌入统一新建弹窗：不渲染遮罩/标题栏，Esc 交给宿主 */
  embedded?: boolean
}>()

const emit = defineEmits<{
  close: []
  saved: [payload: { savedId: string; connectNow: boolean }]
}>()

const store = useSSHStore()
const settingsStore = useSettingsStore()
const { confirm } = useConfirm()

const isEdit = !!props.editData
const saving = ref(false)
const testing = ref(false)
const errorMsg = ref('')
const showPassword = ref(false)
const showPassphrase = ref(false)
const testResult = ref<SSHTestResult | null>(null)

const form = reactive({
  name: props.editData?.name || '',
  host: props.editData?.host || '',
  port: props.editData?.port || 22,
  username: props.editData?.username || '',
  authType: (props.editData?.authType || 'password') as 'password' | 'privateKey' | 'agent',
  password: props.editData?.password || '',
  privateKeyPath: props.editData?.privateKeyPath || '',
  passphrase: props.editData?.passphrase || '',
  // 备注(单条字符串,最多 10 个字). 兼容旧数据:读法走 connNote 单一来源（note 优先、tags[0] 兜底）
  note: props.editData ? connNote(props.editData) : '',
  // 目标环境标记:生产环境危险命令将被强制要求预授权 allowlist
  // 新建时取「设置 → Agent 治理 → 运维策略」的默认环境（E4）；编辑已有连接
  // 仍以该条已存的标记为准，否则改默认值会静默重写历史归类。
  environment: (props.editData?.environment || settingsStore.settings.agent.defaultEnvironment) as 'test' | 'prod',
  // 回显用 != null 而非真值判断：已存的 0（关闭心跳）不能被显示成默认 10，
  // 否则用户看到「10」再保存，把显式配置覆盖回去
  readyTimeout: props.editData?.readyTimeout != null ? Math.round(props.editData.readyTimeout / 1000) : 15,
  keepaliveInterval: props.editData?.keepaliveInterval != null ? Math.round(props.editData.keepaliveInterval / 1000) : 10,
  connectNow: false,
})

const authOptions = [
  { value: 'password' as const, label: '密码' },
  { value: 'privateKey' as const, label: '私钥' },
  { value: 'agent' as const, label: 'Agent' },
]

// 编辑已保存连接时凭据框默认留空（主进程列表接口不返回明文，属安全设计）——
// 用占位符明确告知"留空 = 保持不变"，避免用户以为凭据丢失而反复重输
const passwordPlaceholder = isEdit && props.editData?.hasPassword
  ? '已保存，留空则保持原密码'
  : '输入密码'
const passphrasePlaceholder = isEdit && props.editData?.hasPassphrase
  ? '已保存，留空则保持原值'
  : 'Passphrase'

// 认证相关字段变动时清除旧的测试结果,避免误导
// 同时推进测试代次：testing 只禁用页脚三个按钮，host/port/用户名这些输入框在测试期间
// 照样可编辑（见 :24/:28/:35），改成 B 之后 A 的回包才落地 —— 没有代次判据就会把
// A 的「连接成功 (45ms)」或失败原因印在 B 名下。
let testSeq = 0
watch(
  () => [form.host, form.port, form.username, form.authType, form.password, form.privateKeyPath, form.passphrase],
  () => { testResult.value = null; testSeq++ },
)

// 审查修复（P2）：统一的关闭入口 — saving/testing 期间禁用
function handleOverlayClose() {
  if (saving.value || testing.value) return
  emit('close')
}

// Escape 键关闭弹窗
function handleKeydown(e: KeyboardEvent) {
  // 审查修复（P2）：saving/testing 期间禁止关闭（Esc/取消/右上角 ×
  // 统一走 handleOverlayClose，点击遮罩空白处不关闭）— 原实现关闭后保存/测试
  // 请求仍在后台继续并落库，用户看不到结果且可能重复提交
  if (e.key === 'Escape') handleOverlayClose()
}
onMounted(() => {
  if (!props.embedded) window.addEventListener('keydown', handleKeydown)
})
onUnmounted(() => window.removeEventListener('keydown', handleKeydown))

// 嵌入式宿主用自己的关闭按钮/Esc 时仍走这道闸门（saving/testing 期间不关）
defineExpose({ requestClose: handleOverlayClose })

// ARCH-011：端口判据收归 shared/port-core（与 TunnelModal/AccessModal 同源），
// 夹取的交互形态保留：合法整数直通，非有限/小于下限回落默认 22，超上界钳 65535。
// （附带修正：原先小数如 22.5 会原样放行进 buildPayload，整数判据下夹回 22）
function clampPortValue(n: number): number {
  if (isPort(n)) return n
  return Number.isFinite(n) && n > PORT_MAX ? PORT_MAX : 22
}

function clampPort() {
  form.port = clampPortValue(Number(form.port))
}

/** 将秒级输入安全转为毫秒;空/非法返回 undefined 让主进程用默认值。
 *  allowZero 时 0 是合法的显式值（心跳间隔 0 = 关闭心跳），不能被当成「未填」 */
function sanitizeMs(val: unknown, allowZero = false): number | undefined {
  if (val === '' || val === null || val === undefined) return undefined
  const n = Number(val)
  if (!Number.isFinite(n) || n < 0 || (n === 0 && !allowZero)) return undefined
  return Math.round(n) * 1000
}

/** 构建认证载荷,供测试与保存共用 */
function buildPayload() {
  return {
    name: form.name.trim() || `${form.username.trim()}@${form.host.trim()}`,
    host: form.host.trim(),
    port: clampPortValue(Number(form.port) || 22),
    username: form.username.trim(),
    authType: form.authType,
    password: form.authType === 'password' ? form.password : undefined,
    privateKeyPath: form.authType === 'privateKey' ? form.privateKeyPath : undefined,
    passphrase: form.authType === 'privateKey' ? form.passphrase : undefined,
    environment: form.environment,
    readyTimeout: sanitizeMs(form.readyTimeout),
    // 心跳必须 allowZero：输入框 min=0 且 0 = 关闭心跳（见上方回显注释）。
    // 不传的话 sanitizeMs 把 0 当「未填」返回 undefined → JSON 序列化时键消失 →
    // 新建连接拿主进程默认 10s，编辑时「10 → 0」也退回旧值：用户按下关闭心跳
    // 却毫无效果，且界面上看不出差别。readyTimeout 的 min=1，不存在这个问题。
    keepaliveInterval: sanitizeMs(form.keepaliveInterval, true),
  }
}

/** 把响应式对象转成 plain object,避免 IPC structuredClone 失败 */
function toPlain<T>(obj: T): T {
  return JSON.parse(JSON.stringify(obj))
}

function isDuplicate(): boolean {
  const key = `${form.host.trim()}:${form.port}:${form.username.trim()}`
  return store.connections.some(c =>
    `${c.host}:${c.port}:${c.username}` === key && c.id !== props.editData?.id
  )
}

async function selectKeyFile() {
  const api = window.electronAPI
  if (api?.fsSelectFile) {
    const path = await api.fsSelectFile()
    if (path) form.privateKeyPath = path
  }
}

async function handleTest() {
  errorMsg.value = ''
  testResult.value = null

  if (!form.host.trim() || !form.username.trim()) {
    errorMsg.value = '请先填写主机和用户名'
    return
  }
  // 编辑已保存密码的连接时允许留空（主进程用存储的凭据补全后再测试）
  if (form.authType === 'password' && !form.password && !(isEdit && props.editData?.hasPassword)) {
    errorMsg.value = '请输入密码'
    return
  }
  if (form.authType === 'privateKey' && !form.privateKeyPath) {
    errorMsg.value = '请选择私钥文件'
    return
  }

  const api = window.electronAPI
  if (!api) { errorMsg.value = 'Electron API 不可用'; return }

  testing.value = true
  const mySeq = ++testSeq
  try {
    const result = await api.sshTestConnection(toPlain({
      id: props.editData?.id || '',
      ...buildPayload(),
    }))
    // 测试期间目标被改过（或被那个 watch 清过结果）⇒ 这一笔已不属于当前表单
    if (mySeq !== testSeq) return
    testResult.value = result
  } catch (err: any) {
    if (mySeq !== testSeq) return
    testResult.value = { ok: false, message: toFriendlyMessage(err, '测试失败'), latencyMs: 0 }
  } finally {
    testing.value = false
  }
}

async function handleSave() {
  errorMsg.value = ''

  if (!form.host.trim()) { errorMsg.value = '请输入主机地址'; return }
  if (!form.username.trim()) { errorMsg.value = '请输入用户名'; return }
  // 私钥方式必填（新建/编辑皆然）；密码仅新建时强制，编辑允许留空沿用原值
  if (!isEdit && form.authType === 'password' && !form.password) { errorMsg.value = '请输入密码'; return }
  if (form.authType === 'privateKey' && !form.privateKeyPath) { errorMsg.value = '请选择私钥文件'; return }

  if (isDuplicate()) {
    const ok = await confirm({
      title: '检测到重复连接',
      message: `已存在相同连接 ${form.host}:${form.port} (${form.username}),是否继续保存?`,
      confirmText: '继续保存',
    })
    if (!ok) return
  }

  saving.value = true
  try {
    // 备注:限长 10 字,去除首尾空格
    const note = (form.note || '').trim().slice(0, 10)
    const payload = toPlain({ ...buildPayload(), note, tags: note ? [note] : [] })
    let savedId: string
    if (isEdit) {
      await store.updateConnection(props.editData!.id, payload)
      savedId = props.editData!.id
    } else {
      savedId = await store.addConnection(payload)
    }
    emit('saved', { savedId, connectNow: form.connectNow })
  } catch (err: any) {
    console.error('[ConnectionModal] save failed:', err)
    errorMsg.value = toFriendlyMessage(err, '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-dialog {
  width: 560px;
  max-height: 85vh;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 嵌入统一新建弹窗：骨架由宿主提供，这里只留可滚动正文与页脚 */
.embed-root {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.modal-dialog.embedded {
  flex: 1;
  width: 100%;
  max-height: none;
  min-height: 0;
  background: transparent;
  border: none;
  border-radius: 0;
  box-shadow: none;
}

.modal-dialog.embedded .modal-body {
  flex: 1;
  min-height: 0;
}

.modal-header {
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.modal-header h3 {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.modal-close {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.modal-close:hover {
  background: var(--bg-hover);
  color: var(--text-secondary);
}

.modal-close svg {
  width: 14px;
  height: 14px;
}

.modal-body {
  padding: 8px 16px 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.form-row {
  display: flex;
  gap: 12px;
}

.form-row.two-col {
  gap: 12px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.form-group.flex-1 { flex: 1; }
.form-group.flex-2 { flex: 2; }

.form-group label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.form-group input[type="text"],
.form-group input[type="password"],
.form-group input[type="number"] {
  height: 32px;
  padding: 0 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  transition: border-color var(--transition-fast);
  width: 100%;
  box-sizing: border-box;
}

.form-group input:focus {
  border-color: var(--accent-primary);
}

.form-group input::placeholder {
  color: var(--text-tertiary);
}

.env-select select {
  height: 32px;
  width: 100%;
  padding: 0 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  cursor: pointer;
  box-sizing: border-box;
}
.env-select select:focus {
  border-color: var(--accent-primary);
}
.env-hint {
  margin-top: 6px;
  font-size: 11px;
  color: var(--accent-red);
  line-height: 1.4;
}

.auth-tabs {
  display: flex;
  gap: 2px;
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  padding: 2px;
}

.auth-tab {
  flex: 1;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
  font-family: var(--font-sans);
  transition: all var(--transition-fast);
}

.auth-tab:hover {
  color: var(--text-secondary);
}

.auth-tab.active {
  color: var(--text-primary);
  background: var(--bg-hover);
  box-shadow: var(--shadow-sm);
}

.input-with-btn {
  display: flex;
  gap: 6px;
}

.input-with-btn input {
  flex: 1;
  height: 32px;
  padding: 0 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-mono);
  outline: none;
  transition: border-color var(--transition-fast);
  min-width: 0;
}

.input-with-btn input:focus {
  border-color: var(--accent-primary);
}

.browse-btn,
.toggle-btn {
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition-fast);
  flex-shrink: 0;
}

.browse-btn:hover,
.toggle-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.form-info {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: color-mix(in srgb, var(--accent-primary) 6%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-primary) 15%, transparent);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.4;
}

.form-info svg {
  flex-shrink: 0;
  color: var(--accent-text);
}

.checkbox-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
}

.checkbox-row input {
  width: 14px;
  height: 14px;
  accent-color: var(--accent-primary);
  cursor: pointer;
}

.test-result {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.test-result.success {
  background: color-mix(in srgb, var(--accent-green) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-green) 25%, transparent);
  color: var(--accent-green);
}

.test-result.fail {
  background: color-mix(in srgb, var(--accent-red) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-red) 20%, transparent);
  color: var(--accent-red);
}

.form-error {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: color-mix(in srgb, var(--accent-red) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-red) 20%, transparent);
  border-radius: var(--radius-sm);
  color: var(--accent-red);
  font-size: 12px;
}

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

/* 测试连接：margin-right: auto 把它推到左侧，取消/添加靠右
   （modal-footer 上 justify-content: flex-end） */
.btn-test { margin-right: auto; }

.spin {
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
