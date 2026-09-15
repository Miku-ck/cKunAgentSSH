<template>
  <div class="panel models-panel">
    <header class="panel-header">
      <h3>模型</h3>
      <p class="panel-desc">管理 LLM 提供商与模型配置</p>
    </header>

    <div class="panel-body">
      <!-- 工具栏 -->
      <div class="toolbar">
        <button class="btn btn-primary btn-sm" @click="openCreateDialog">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          添加模型
        </button>
        <button class="btn btn-ghost btn-sm" @click="store.fetchModelConfigs()" :disabled="store.modelsLoading">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 4v6h6M23 20v-6h-6"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/></svg>
          刷新
        </button>
      </div>

      <!-- 加载状态 -->
      <div v-if="store.modelsLoading" class="state-loading">
        <div class="spinner" />
        <span>加载中...</span>
      </div>

      <!-- 错误提示 -->
      <div v-else-if="store.modelsError" class="state-error">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#f85149" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        {{ store.modelsError }}
        <button class="btn btn-ghost btn-sm" @click="store.fetchModelConfigs()">重试</button>
      </div>

      <!-- 模型列表 -->
      <div v-else class="model-list">
        <div
          v-for="m in store.modelConfigs"
          :key="m.id"
          class="model-card"
          :class="{ 'is-default': isAnyDefault(m) }"
        >
          <div class="model-card-header">
            <div class="model-card-title">
              <span class="model-name">{{ m.name }}</span>
              <span v-if="m.supports_thinking" class="badge badge-cap">思考</span>
              <span v-if="m.supports_image" class="badge badge-cap">视觉</span>
              <span v-if="m.supports_tool_calling" class="badge badge-cap">工具</span>
              <span class="badge badge-provider">{{ providerLabel(m.provider) }}</span>
            </div>
            <div class="model-card-actions">
              <button class="btn-icon" title="测试连接" @click="handleTest(m.id)" :disabled="testingId === m.id">
                <svg v-if="testingId !== m.id" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                <div v-else class="spinner spinner-sm" />
              </button>
              <button class="btn-icon" title="编辑" @click="openEditDialog(m)">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
              </button>
              <button class="btn-icon btn-icon-danger" title="删除" @click="handleDelete(m.id, m.name)">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              </button>
            </div>
          </div>

          <div class="model-card-body">
            <div class="model-field">
              <span class="field-label">端点</span>
              <span class="field-value field-monospace">{{ m.base_url }}</span>
            </div>
            <div v-if="m.model_id" class="model-field">
              <span class="field-label">模型 ID</span>
              <span class="field-value field-monospace">{{ m.model_id }}</span>
            </div>
            <div class="model-params">
              <div class="param-chip">
                <span class="param-label">Temp</span>
                <span class="param-value">{{ m.temperature ?? 0.7 }}</span>
              </div>
              <div class="param-chip">
                <span class="param-label">Max</span>
                <span class="param-value">{{ formatTokenCount(m.max_tokens ?? 4096) }}</span>
              </div>
              <div class="param-chip">
                <span class="param-label">Top-P</span>
                <span class="param-value">{{ m.top_p ?? 0.9 }}</span>
              </div>
            </div>
          </div>

          <!-- 测试状态 -->
          <div v-if="testStatus[m.id]" class="test-status" :class="'test-' + testStatus[m.id].status">
            <svg v-if="testStatus[m.id].status === 'ok'" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            <svg v-else-if="testStatus[m.id].status === 'error'" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
            <span>{{ testStatus[m.id].message }}</span>
            <span v-if="testStatus[m.id].latencyMs" class="test-latency">{{ testStatus[m.id].latencyMs }}ms</span>
          </div>
        </div>

        <div v-if="store.modelConfigs.length === 0" class="model-empty">
          <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="#606060" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          <p>暂无模型配置</p>
          <p class="hint">点击「添加模型」配置第一个 LLM 端点</p>
        </div>
      </div>
    </div>

    <!-- 添加/编辑模型对话框 -->
    <Teleport to="body">
      <Transition name="modal-fade">
        <div v-if="showDialog" class="modal-overlay">
          <div class="modal-container">
            <!-- Header -->
            <div class="modal-header">
              <h4>{{ isEditing ? '编辑模型' : '添加模型' }}</h4>
              <button class="modal-close" @click="closeDialog">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>

            <!-- Body -->
            <div class="modal-body">
              <!-- 提供商选择 -->
              <div class="form-group">
                <label class="form-label">提供商 <span class="required">*</span></label>
                <div class="provider-select" @click="showProviderDropdown = !showProviderDropdown" ref="providerRef">
                  <div class="provider-selected">
                    <span>{{ selectedProvider?.label || '选择提供商' }}</span>
                    <svg class="provider-arrow" :class="{ open: showProviderDropdown }" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                  </div>
                  <Transition name="dropdown">
                    <div v-if="showProviderDropdown" class="provider-dropdown">
                      <div
                        v-for="p in PROVIDERS"
                        :key="p.id"
                        class="provider-option"
                        :class="{ active: form.provider === p.id }"
                        @click.stop="selectProvider(p)"
                      >
                        <span>{{ p.label }}</span>
                      </div>
                    </div>
                  </Transition>
                </div>
              </div>

              <!-- 接口地址 -->
              <div class="form-group">
                <label class="form-label">接口地址 <span class="required">*</span></label>
                <input v-model="form.base_url" class="form-input" placeholder="如：http://192.168.150.40:9030/v1" />
              </div>

              <!-- API Key —— 本地部署无需配置，隐藏整块 -->
              <div v-if="!isLocalProvider" class="form-group">
                <div class="label-row">
                  <label class="form-label">API Key <span class="required">*</span></label>
                  <span v-if="isEditing && hasExistingKey" class="local-note">已配置 · 留空则保留原值</span>
                </div>
                <div class="api-key-row">
                  <input
                    v-model="form.api_key"
                    class="form-input"
                    :type="showApiKey ? 'text' : 'password'"
                    :placeholder="isEditing && hasExistingKey ? '留空保留原 Key，输入新值则替换' : '输入 API Key'"
                    autocomplete="off"
                    spellcheck="false"
                  />
                  <button
                    type="button"
                    class="btn-eye"
                    :title="showApiKey ? '隐藏' : '显示'"
                    @click="showApiKey = !showApiKey"
                  >
                    <svg v-if="showApiKey" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                    <svg v-else viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                  </button>
                </div>
              </div>

              <!-- 模型名称 -->
              <div class="form-group">
                <label class="form-label">模型名称 <span class="required">*</span></label>
                <input v-model="form.name" class="form-input" placeholder="如：Qwen3-VL-8B-Instruct" />
              </div>

              <!-- 模型 ID -->
              <div class="form-group">
                <label class="form-label">模型 ID</label>
                <input v-model="form.model_id" class="form-input" placeholder="" />
              </div>

              <!-- 高级配置 -->
              <div class="advanced-section">
                <div class="advanced-toggle" @click="showAdvanced = !showAdvanced">
                  <span>高级配置</span>
                  <svg :class="{ open: showAdvanced }" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                </div>
                <Transition name="collapse">
                  <div v-if="showAdvanced" class="advanced-body">
                    <!-- 能力开关 -->
                    <div class="capability-row">
                      <label class="capability-item">
                        <input type="checkbox" v-model="form.supports_tool_calling" class="form-checkbox" />
                        <span>工具调用</span>
                      </label>
                      <label class="capability-item">
                        <input type="checkbox" v-model="form.supports_image" class="form-checkbox" />
                        <span>图片输入</span>
                      </label>
                      <label class="capability-item">
                        <input type="checkbox" v-model="form.supports_thinking" class="form-checkbox" />
                        <span>思考模式</span>
                      </label>
                    </div>

                    <!-- 温度 -->
                    <div class="form-group">
                      <label class="form-label">Temperature</label>
                      <div class="temp-slider-row">
                        <input v-model.number="form.temperature" class="form-input temp-input" type="number" min="0" max="2" step="0.1" />
                        <input
                          type="range"
                          v-model.number="form.temperature"
                          min="0"
                          max="2"
                          step="0.1"
                          class="temp-range"
                        />
                      </div>
                    </div>

                    <!-- 输入 Token 限制 -->
                    <div class="form-group">
                      <label class="form-label">输入（上下文长度）</label>
                      <div class="token-chips">
                        <button
                          v-for="opt in INPUT_TOKEN_OPTIONS"
                          :key="opt"
                          class="token-chip"
                          :class="{ active: form.max_tokens === opt }"
                          @click="form.max_tokens = opt"
                        >
                          {{ formatTokenCount(opt) }}
                        </button>
                      </div>
                    </div>

                    <!-- 输出 Token 限制 -->
                    <div class="form-group">
                      <label class="form-label">输出（最大回复长度）</label>
                      <div class="token-chips">
                        <button
                          v-for="opt in OUTPUT_TOKEN_OPTIONS"
                          :key="opt"
                          class="token-chip"
                          :class="{ active: form.max_output_tokens === opt }"
                          @click="form.max_output_tokens = opt"
                        >
                          {{ formatTokenCount(opt) }}
                        </button>
                      </div>
                    </div>
                  </div>
                </Transition>
              </div>
            </div>

            <!-- Footer -->
            <div class="modal-footer">
              <button class="btn btn-ghost" @click="closeDialog">取消</button>
              <button class="btn btn-primary" :disabled="!canSave" @click="handleSave">
                {{ isEditing ? '保存' : '添加' }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import type { ModelConfig } from '@/api/client'

const store = useSettingsStore()
const toast = useToastStore()
const { confirm } = useConfirm()

// ── 提供商定义 ──
interface ProviderDef {
  id: string
  label: string
  baseUrl: string
  isLocal: boolean
  icon: string
}

const PROVIDERS: ProviderDef[] = [
  { id: 'vllm', label: '自定义', baseUrl: 'http://localhost:8000/v1', isLocal: true, icon: '' },
  { id: 'deepseek', label: 'DeepSeek', baseUrl: 'https://api.deepseek.com/v1', isLocal: false, icon: '' },
  { id: 'openai', label: 'OpenAI', baseUrl: 'https://api.openai.com/v1', isLocal: false, icon: '' },
  { id: 'qwen', label: 'Qwen', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', isLocal: false, icon: '' },
  { id: 'kimi', label: 'Kimi', baseUrl: 'https://api.moonshot.cn/v1', isLocal: false, icon: '' },
  { id: 'glm', label: 'GLM', baseUrl: 'https://open.bigmodel.cn/api/paas/v4', isLocal: false, icon: '' },
  { id: 'xiaomi', label: 'Xiaomi', baseUrl: 'https://api.xiaomimimo.com/v1', isLocal: false, icon: '' },
  { id: 'anthropic', label: 'Anthropic', baseUrl: 'https://api.anthropic.com/v1', isLocal: false, icon: '' },

]

const INPUT_TOKEN_OPTIONS = [4096, 8192, 16384, 262144, 1000000]
const OUTPUT_TOKEN_OPTIONS = [4096, 8192, 16384, 262144, 1000000]

// ── 对话框状态 ──
const showDialog = ref(false)
const isEditing = ref(false)
const editingId = ref('')
const showApiKey = ref(false)
const showAdvanced = ref(false)
const showProviderDropdown = ref(false)
const hasExistingKey = ref(false)
const maskedApiKey = ref('')  // 编辑时后端返回的脱敏 key，用于判断用户是否修改
const providerRef = ref<HTMLElement | null>(null)

// ── 表单 ──
const form = reactive({
  name: '',
  model_id: '',
  base_url: '',
  api_key: '',
  provider: 'vllm',
  supports_thinking: false,
  supports_image: false,
  supports_tool_calling: false,
  temperature: 0.7,
  max_tokens: 4096,
  max_output_tokens: 4096,
})

const selectedProvider = computed(() => PROVIDERS.find(p => p.id === form.provider))
const isLocalProvider = computed(() => selectedProvider.value?.isLocal ?? false)
// 非本地部署时 API Key 必填；本地部署不需要
const canSave = computed(() => {
  if (!form.name || !form.base_url || !form.provider) return false
  if (!isLocalProvider.value) {
    // 新建时必须填 key；编辑时 key 不能为空（除非和原脱敏值一样表示保留原值）
    const key = (form.api_key || '').trim()
    if (!isEditing.value && !key) return false
    if (isEditing.value && !key && !hasExistingKey.value) return false
  }
  return true
})

function selectProvider(p: ProviderDef) {
  form.provider = p.id
  if (p.baseUrl && !isEditing.value) {
    form.base_url = p.baseUrl
  }
  showProviderDropdown.value = false
}

function resetForm() {
  form.name = ''
  form.model_id = ''
  form.base_url = ''
  form.api_key = ''
  form.provider = 'vllm'
  form.supports_thinking = false
  form.supports_image = false
  form.supports_tool_calling = false
  form.temperature = 0.7
  form.max_tokens = 4096
  form.max_output_tokens = 4096
}

function openCreateDialog() {
  resetForm()
  isEditing.value = false
  editingId.value = ''
  hasExistingKey.value = false
  // 默认隐藏 API Key
  showApiKey.value = false
  showAdvanced.value = false
  showProviderDropdown.value = false
  // 自动填充 vLLM 默认地址
  const vllm = PROVIDERS.find(p => p.id === 'vllm')
  if (vllm) form.base_url = vllm.baseUrl
  showDialog.value = true
}

function openEditDialog(m: ModelConfig) {
  resetForm()
  isEditing.value = true
  editingId.value = m.id
  form.name = m.name
  form.model_id = m.model_id || ''
  form.base_url = m.base_url || ''
  form.provider = m.provider || PROVIDERS[0]?.id || 'vllm'
  // API Key 反显：后端返回脱敏值（如 "sk-a****"），放入输入框让用户可见前缀并编辑
  const masked = m.api_key || ''
  const hasReal = !!(masked && masked !== '****' && masked !== '')
  hasExistingKey.value = hasReal
  maskedApiKey.value = hasReal ? masked : ''
  form.api_key = hasReal ? masked : ''
  form.supports_thinking = !!m.supports_thinking
  form.supports_image = !!m.supports_image
  form.supports_tool_calling = !!m.supports_tool_calling
  form.temperature = m.temperature ?? 0.7
  form.max_tokens = m.max_tokens ?? 4096
  form.max_output_tokens = m.max_output_tokens ?? 4096
  // 默认隐藏 API Key
  showApiKey.value = false
  showAdvanced.value = false
  showProviderDropdown.value = false
  showDialog.value = true
}

function closeDialog() {
  showDialog.value = false
}

async function handleSave() {
  if (!canSave.value) return
  const trim = (s: string) => (s || '').trim()
  const payload: any = {
    name: trim(form.name),
    base_url: trim(form.base_url),
    model_id: trim(form.model_id),  // 始终发送字符串（含空串），确保清空也能落库
    provider: form.provider,
    supports_thinking: form.supports_thinking,
    supports_image: form.supports_image,
    supports_tool_calling: form.supports_tool_calling,
    temperature: form.temperature,
    max_tokens: form.max_tokens,
    max_output_tokens: form.max_output_tokens,
  }
  // top_p 仅新建时发送默认值；编辑时 UI 从未暴露该字段，不发以保留后端原值
  if (!isEditing.value) {
    payload.top_p = 0.9
  }
  const trimmedKey = trim(form.api_key)
  // 本地部署不发送 api_key；编辑时若值与原脱敏值相同表示未修改，不发送
  if (!isLocalProvider.value && trimmedKey) {
    if (!(isEditing.value && trimmedKey === maskedApiKey.value)) {
      payload.api_key = trimmedKey
    }
  }
  let result: ModelConfig | null
  if (isEditing.value) {
    result = await store.updateModelConfig(editingId.value, payload)
  } else {
    result = await store.createModelConfig(payload)
  }
  if (result) {
    // 持久化校验在 store.createModelConfig 内置：若刚创建的 id 不在列表，store.modelsError 会被置告警
    if (!isEditing.value && store.modelsError) {
      toast.error(`模型未持久化：${store.modelsError}`, 8000)
      // 不关对话框，让用户看到 toast 并可重试
    } else {
      // 保存成功后关闭窗口（× / 取消 / 保存均有关闭入口；点遮罩不关，防止误触丢表单）
      closeDialog()
      toast.success(isEditing.value ? '模型已更新' : '模型已添加')
    }
  } else {
    const raw = store.modelsError || '保存失败，请检查输入或后端连接'
    const friendly = /fetch/i.test(raw)
      ? '后端服务暂不可用，正在尝试自动恢复，请稍后再试。'
      : raw
    toast.error(friendly, 6000)
  }
}

async function handleDelete(id: string, name: string) {
  const ok = await confirm({
    title: '删除模型',
    message: `确定删除模型「${name}」？此操作不可撤销。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  const success = await store.deleteModelConfig(id)
  if (success) {
    // 清理已删除模型的测试状态，避免内存泄漏
    delete testStatus[id]
    toast.success(`已删除模型「${name}」`)
  } else {
    toast.error(store.modelsError || '删除失败', 6000)
  }
}

// ── 测试 ──
const testingId = ref('')
const testStatus = reactive<Record<string, { status: 'ok' | 'error' | 'testing'; message: string; latencyMs?: number }>>({})

async function handleTest(id: string) {
  testingId.value = id
  testStatus[id] = { status: 'testing', message: '正在测试连接...' }
  await store.testModelConnection(id, (status, detail, latencyMs) => {
    // onStatus('testing') 是 store 同步调的首个回调，此时 testStatus[id] 必为 'testing'，放行；
    // 异步 await api 后的 ok/error 回调进来时，testStatus[id] 仍为 'testing'（无人改），放行。
    // 删除原 stale 检查——它在某竞态下会误吞 ok 回调，致 UI 永显示"正在测试..."或误判失败。
    if (status === 'ok') {
      testStatus[id] = { status: 'ok', message: '连接成功', latencyMs }
    } else if (status === 'testing') {
      // store 首个同步 testing 回调，UI 已是 testing 状态，无需重写
      return
    } else {
      testStatus[id] = { status: 'error', message: detail ? `连接失败: ${detail}` : '连接失败' }
    }
    testingId.value = ''
  })
}

// ── 点击外部关闭下拉 ──
function handleClickOutside(e: MouseEvent) {
  if (providerRef.value && !providerRef.value.contains(e.target as Node)) {
    showProviderDropdown.value = false
  }
}

onMounted(() => {
  if (store.modelConfigs.length === 0 && !store.modelsLoading) {
    store.fetchModelConfigs()
  }
  document.addEventListener('click', handleClickOutside)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', handleClickOutside)
})

// ── 辅助函数 ──
function providerLabel(provider?: string): string {
  const p = PROVIDERS.find(x => x.id === provider)
  return p ? p.label : (provider || '未知')
}

// 判断模型是否为默认（is_default 为全局标记，is_default_ssh 为 SSH 模式标记）
function isAnyDefault(m: ModelConfig): boolean {
  return !!(m.is_default || m.is_default_ssh)
}

function formatTokenCount(n: number): string {
if (n >= 1000000) return (n / 1000000).toFixed(0) + 'M'
if (n >= 1000) return (n / 1000).toFixed(0) + 'K'
return String(n)
}

</script>

<style scoped>
.models-panel { height: 100%; display: flex; flex-direction: column; }

.panel-header { margin-bottom: 16px; flex-shrink: 0; }
.panel-header h3 { margin: 0 0 4px; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.panel-desc { margin: 0; font-size: 12px; color: var(--text-secondary); }

.panel-body { flex: 1; overflow-y: auto; scrollbar-width: none; }
.panel-body::-webkit-scrollbar { display: none; }

/* 工具栏 */
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  flex-shrink: 0;
}

/* 状态 */
.state-loading, .state-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  font-size: 13px;
  color: var(--text-secondary);
}
.state-error { color: #f85149; flex-direction: column; align-items: flex-start; gap: 8px; }

.spinner {
  width: 16px; height: 16px;
  border: 2px solid #2d2d30;
  border-top-color: var(--accent-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
.spinner-sm { width: 12px; height: 12px; border-width: 1.5px; display: inline-block; }

@keyframes spin { to { transform: rotate(360deg); } }

/* 模型列表 */
.model-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.model-card {
  background: var(--bg-input);
  border: 1px solid #2d2d30;
  border-radius: 8px;
  padding: 14px 16px;
  transition: border-color 0.15s;
}
.model-card:hover { border-color: #444; }
.model-card.is-default { border-color: color-mix(in srgb, var(--accent-primary) 20%, transparent); background: color-mix(in srgb, var(--accent-primary) 3%, transparent); }

.model-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.model-card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.model-name { font-size: 13px; font-weight: 600; color: var(--text-primary); }

.model-card-actions { display: flex; gap: 4px; }

.model-card-body { display: flex; flex-direction: column; gap: 6px; }

.model-field {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.field-label { color: var(--text-secondary); min-width: 56px; }
.field-value { color: var(--text-primary); }
.field-monospace { font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 11px; }

.model-params {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}
.param-chip {
  background: var(--bg-sidebar);
  border: 1px solid #2d2d30;
  border-radius: 4px;
  padding: 3px 8px;
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
}
.param-label { color: var(--text-secondary); }
.param-value { color: var(--text-primary); font-weight: 500; }

/* Badge */
.badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 500;
  white-space: nowrap;
}
.badge-cap { background: #3fb95022; color: #3fb950; border: 1px solid #3fb95044; }
.badge-provider { background: color-mix(in srgb, var(--text-secondary) 13%, transparent); color: var(--text-secondary); border: 1px solid #ffffff10; }


/* 测试状态 */
.test-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid #2d2d30;
}
.test-ok { color: #3fb950; }
.test-error { color: #f85149; }
.test-testing { color: var(--text-secondary); }
.test-latency {
  margin-left: auto;
  font-size: 11px;
  font-weight: 500;
  padding: 1px 8px;
  border-radius: 10px;
  background: #3fb95022;
  color: #3fb950;
  border: 1px solid #3fb95044;
}

/* 空状态 */
.model-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  color: #606060;
  gap: 8px;
}
.model-empty p { margin: 0; font-size: 13px; }
.model-empty .hint { font-size: 12px; color: #505050; }

/* 按钮 */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: none;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
  font-family: inherit;
}
.btn-sm { padding: 4px 10px; font-size: 11px; }
.btn-primary { background: var(--accent-primary); color: #fff; }
.btn-primary:hover { background: #4a9aef; }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-ghost { background: transparent; color: var(--text-secondary); }
.btn-ghost:hover { background: #ffffff10; color: var(--text-primary); }
.btn-ghost:disabled { opacity: 0.4; cursor: not-allowed; }

.btn-icon {
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  background: transparent;
  border: none;
  border-radius: 4px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
}
.btn-icon:hover { background: #ffffff10; color: var(--text-primary); }
.btn-icon:disabled { opacity: 0.3; cursor: not-allowed; }
.btn-icon-danger:hover { background: #f8514915; color: #f85149; }

/* ── Modal ── */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000;
}
.modal-container {
  background: var(--bg-input-solid);
  border: 1px solid #2d2d30;
  border-radius: 10px;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
  width: 90vw;
  max-width: 480px;
  max-height: 85vh;
  overflow-y: auto;
  scrollbar-width: none;
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #2d2d30;
  flex-shrink: 0;
}
.modal-header h4 { margin: 0; font-size: 14px; font-weight: 600; }

.modal-close {
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: none; border-radius: 4px;
  color: var(--text-secondary); cursor: pointer; transition: all 0.15s;
}
.modal-close:hover { background: #ffffff10; color: var(--text-primary); }

.modal-body { padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }
.modal-footer {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid #2d2d30;
  flex-shrink: 0;
}

/* ── Form ── */
.form-group { display: flex; flex-direction: column; gap: 4px; }
.form-label { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
.required { color: #f85149; }
.form-input {
  width: 100%;
  padding: 8px 10px;
  background: var(--bg-sidebar);
  border: 1px solid #2d2d30;
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
  box-sizing: border-box;
}
.form-input:focus { border-color: var(--accent-primary); box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent-primary) 20%, transparent); }
.form-input::placeholder { color: #606060; }

.label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.local-note {
  font-size: 11px;
  color: #3fb950;
}

/* API Key */
.api-key-row {
  position: relative;
  display: flex;
  align-items: center;
}
.api-key-row .form-input { padding-right: 36px; }
.btn-eye {
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: 4px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
}
.btn-eye:hover { color: var(--accent-primary); background: #ffffff10; }

/* ── 提供商选择器 ── */
.provider-select {
  position: relative;
  cursor: pointer;
}
.provider-selected {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: var(--bg-sidebar);
  border: 1px solid #2d2d30;
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 13px;
  transition: border-color 0.15s;
}
.provider-selected:hover { border-color: #444; }
.provider-arrow {
  margin-left: auto;
  transition: transform 0.15s;
  color: var(--text-secondary);
}
.provider-arrow.open { transform: rotate(180deg); }

.provider-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  background: var(--bg-input-solid);
  border: 1px solid #2d2d30;
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  z-index: 10;
  overflow: hidden;
  padding: 4px;
}

.provider-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--text-primary);
  cursor: pointer;
  transition: all 0.1s;
}
.provider-option:hover { background: var(--bg-hover); }
.provider-option.active { background: color-mix(in srgb, var(--accent-primary) 8%, transparent); color: var(--accent-primary); }

/* Dropdown animation */
.dropdown-enter-active, .dropdown-leave-active {
  transition: opacity 0.15s, transform 0.15s;
}
.dropdown-enter-from, .dropdown-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

/* ── 高级配置 ── */
.advanced-section {
  border: 1px solid #2d2d30;
  border-radius: 8px;
  overflow: hidden;
}
.advanced-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
  background: var(--bg-sidebar);
  transition: all 0.15s;
  user-select: none;
}
.advanced-toggle:hover { color: var(--text-primary); }
.advanced-toggle svg { transition: transform 0.2s; color: #606060; }
.advanced-toggle svg.open { transform: rotate(180deg); }

.advanced-body {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--bg-base);
}

/* Collapse animation */
.collapse-enter-active, .collapse-leave-active {
  transition: all 0.2s ease;
  max-height: 600px;
  overflow: hidden;
}
.collapse-enter-from, .collapse-leave-to {
  max-height: 0;
  opacity: 0;
  padding-top: 0;
  padding-bottom: 0;
}

/* 能力开关 */
.capability-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.capability-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-primary);
  cursor: pointer;
  padding: 4px 0;
}
.form-checkbox {
  width: 14px;
  height: 14px;
  accent-color: #58a6ff;
  margin: 0;
  flex-shrink: 0;
}

/* 温度滑块 */
.temp-slider-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.temp-input {
  width: 60px;
  flex-shrink: 0;
  text-align: center;
}
.temp-range {
  flex: 1;
  -webkit-appearance: none;
  appearance: none;
  height: 4px;
  background: var(--bg-slider);
  border-radius: 2px;
  outline: none;
}
.temp-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 14px;
  height: 14px;
  background: var(--accent-primary);
  border-radius: 50%;
  cursor: pointer;
  border: 2px solid var(--bg-input);
}
.temp-range::-moz-range-thumb {
  width: 14px;
  height: 14px;
  background: var(--accent-primary);
  border-radius: 50%;
  cursor: pointer;
  border: 2px solid var(--bg-input);
}

/* Token 芯片 */
.token-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.token-chip {
  padding: 5px 12px;
  background: var(--bg-sidebar);
  border: 1px solid #2d2d30;
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
  font-family: inherit;
}
.token-chip:hover { border-color: #444; color: var(--text-primary); }
.token-chip.active {
  background: color-mix(in srgb, var(--accent-primary) 8%, transparent);
  border-color: color-mix(in srgb, var(--accent-primary) 33%, transparent);
  color: var(--accent-primary);
  font-weight: 500;
}

/* Transition */
.modal-fade-enter-active, .modal-fade-leave-active { transition: opacity 0.2s; }
.modal-fade-enter-from, .modal-fade-leave-to { opacity: 0; }
</style>
