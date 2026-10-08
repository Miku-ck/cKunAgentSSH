<template>
  <div class="panel models-panel">
    <header class="panel-header">
      <h3>模型</h3>
      <p class="panel-desc">管理 LLM 提供商与模型配置</p>
      <p class="panel-desc">
        两档默认互不影响：★ SSH 默认 = 对话 / 终端 / 定时任务读的那档；
        ⊕ 全局默认 = SSH 档未设时，起标题等后台链路兜底读的那档。
      </p>
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

      <!-- 错误提示：只有"没有列表可展示"时才占整块。
           原实现是无条件 v-else-if，于是任何一次 modelsError（GET 抖动、删除被拒、
           设为默认失败）都会把用户能用的模型卡片全部藏起来 —— 数据还在 store 里，
           界面上却像全没了。列表还在时降级成挂在头顶的一条横幅。 -->
      <div v-else-if="store.modelsError && !store.modelConfigs.length" class="state-error">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        {{ store.modelsError }}
        <button class="btn btn-ghost btn-sm" @click="store.fetchModelConfigs()">重试</button>
      </div>

      <!-- 模型列表 -->
      <div v-else class="model-list">
        <div v-if="store.modelsError" class="state-error">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
          {{ store.modelsError }}
          <button class="btn btn-ghost btn-sm" @click="store.fetchModelConfigs()">重试</button>
        </div>
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
              <!-- 审查R2修复：忙态判断由单值 === 改为 Set.has（并发操作互不顶掉） -->
              <button class="btn-icon" title="测试连接" @click="handleTest(m.id)" :disabled="testingIds.has(m.id)">
                <svg v-if="!testingIds.has(m.id)" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                <div v-else class="spinner spinner-sm" />
              </button>
              <!-- A1：切换 SSH 默认模型；已是默认时按钮置灰，靠 title 解释为什么点不动 -->
              <button
                class="btn-icon"
                :class="{ 'btn-icon-default-active': m.is_default_ssh }"
                :title="m.is_default_ssh ? '已是 SSH 默认模型' : '设为 SSH 默认模型'"
                :disabled="m.is_default_ssh || defaultingIds.has(key(m.id, 'ssh'))"
                @click="handleSetDefault(m, 'ssh')"
              >
                <svg v-if="defaultingIds.has(key(m.id, 'ssh'))" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/></svg>
                <svg v-else viewBox="0 0 24 24" width="14" height="14" :fill="m.is_default_ssh ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
              </button>
              <!-- C9：全局默认（is_default）。此前 UI 只能写 SSH 档，而全局档是活消费者
                   （后处理起标题、terminal/agent 在 SSH 档未设时的兜底链）——看得到回落却调不到。 -->
              <button
                class="btn-icon"
                :class="{ 'btn-icon-default-active': m.is_default }"
                :title="m.is_default ? '已是全局默认模型' : '设为全局默认模型'"
                :disabled="m.is_default || defaultingIds.has(key(m.id, 'global'))"
                @click="handleSetDefault(m, 'global')"
              >
                <svg v-if="defaultingIds.has(key(m.id, 'global'))" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/></svg>
                <svg v-else viewBox="0 0 24 24" width="14" height="14" :fill="m.is_default ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
              </button>
              <button class="btn-icon" title="编辑" @click="openEditDialog(m)">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
              </button>
              <!-- 默认模型删不掉是后端的硬规则（database.py delete 前查两档 is_default 直接
                   raise ValueError），而面板原来照样可点，失败把英文原文弹进 toast，且全仓
                   没有"取消默认"的入口 —— 一旦成为默认就看起来永久删不掉。先把话说在前面。 -->
              <button
                class="btn-icon btn-icon-danger"
                :disabled="isAnyDefault(m)"
                :title="isAnyDefault(m) ? '默认模型不可删除：先把默认切到别的模型' : '删除'"
                @click="handleDelete(m.id, m.name)"
              >
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
                <!-- 这个值是「上下文窗口」不是「最大回复长度」——标签写成 Max 会被
                     读成输出上限，与表单里「输入（上下文长度）」的叫法也对不上 -->
                <span class="param-label" title="上下文窗口（输入长度）">Ctx</span>
                <span class="param-value">{{ formatTokenCount(m.max_tokens ?? DEFAULT_CONTEXT_TOKENS) }}</span>
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
          <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
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
                    <!-- 未知 provider（库里的历史值，如建表默认 'custom'）显示原值而不是
                         "选择提供商"：那一行是假的空态，实际值好好的，只是不在选项表里 -->
                    <span>{{ selectedProvider?.label || form.provider || '选择提供商' }}</span>
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

              <!-- 本地部署把整块 Key 藏了，但这一行库里可能还存着此前远程部署时的密钥，
                   而面板此时画的是一张"本地不需要 Key"的干净脸。后端把 api_key 的空串
                   定义为「保留原值」（database.update_model_config），所以这一页没有任何
                   办法把它清掉 —— 不说破就等于替库里那份凭证打掩护。 -->
              <p v-if="isLocalProvider && isEditing && hasExistingKey" class="form-hint">
                本地部署不会用到 API Key，但这条模型仍存着此前设置的密钥；改回远程提供商时它会重新生效。
                后端把留空当作「保留原值」，这一页清不掉它 —— 要彻底去掉只能删除这条模型重建。
              </p>

              <!-- 模型名称 -->
              <div class="form-group">
                <label class="form-label">模型名称 <span class="required">*</span></label>
                <!-- maxlength 与后端 Field(max_length=100) 同口径：超出去是 422，
                     而 backendErrorDetail 会把 pydantic 英文原文（"String should have at
                     most 100 characters"）原样弹进 toast —— 边界拦在这里更省事 -->
                <input v-model="form.name" class="form-input" maxlength="100" placeholder="如：Qwen3-VL-8B-Instruct" />
              </div>

              <!-- 模型 ID -->
              <div class="form-group">
                <label class="form-label">模型 ID</label>
                <input v-model="form.model_id" class="form-input" maxlength="200" placeholder="" />
              </div>

              <!-- 高级配置 -->
              <div class="advanced-section">
                <div class="advanced-toggle" @click="showAdvanced = !showAdvanced">
                  <span>高级配置</span>
                  <svg :class="{ open: showAdvanced }" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                </div>
                <Transition name="collapse">
                  <div v-if="showAdvanced" class="advanced-body">
                    <!-- 能力勾选已撤：supports_thinking / supports_image / supports_tool_calling
                         三列在运行时无任何消费者（思考走 enable_thinking 与演化配置、
                         工具下发走 enable_tools、图片从不按模型判定），勾与不勾都不改变行为，
                         而不勾「工具调用」的模型照样收到全量工具 schema。库里那三列保留
                         （seed 与旧数据仍回填卡片徽标），但不再给一个「看起来能管」的开关。 -->
                    <div v-if="!isEditing" class="capability-row">
                      <!-- 仅新建时可勾选。编辑时的"切换默认"由卡片上的独立入口负责 -->
                      <label class="capability-item">
                        <input type="checkbox" v-model="form.is_default_ssh" class="form-checkbox" />
                        <span>设为 SSH 默认模型</span>
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

                    <!-- 核采样：后端 Field(ge=0, le=1)，此前只有新建时发过 0.9，
                         编辑表单从不提交，用户改不动也看不见 -->
                    <div class="form-group">
                      <label class="form-label">Top P（核采样）</label>
                      <div class="temp-slider-row">
                        <input v-model.number="form.top_p" class="form-input temp-input" type="number" min="0" max="1" step="0.05" />
                        <input
                          type="range"
                          v-model.number="form.top_p"
                          min="0"
                          max="1"
                          step="0.05"
                          class="temp-range"
                        />
                      </div>
                    </div>

                    <!-- 输入 Token 限制 -->
                    <div class="form-group">
                      <label class="form-label">输入（上下文长度）</label>
                      <div class="token-chips">
                        <button
                          v-for="opt in inputTokenOptions"
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
                          v-for="opt in outputTokenOptions"
                          :key="opt"
                          class="token-chip"
                          :class="{ active: form.max_output_tokens === opt }"
                          @click="form.max_output_tokens = opt"
                        >
                          {{ formatTokenCount(opt) }}
                        </button>
                      </div>
                      <p class="form-hint">
                        这是想要的上限 —— 运行时与「按上下文窗口算出的上限」「窗口减去本轮输入与思考预算的余量」取小，
                        选了这么大不代表每一轮都能跑满。
                      </p>
                      <p v-if="outputDegraded" class="form-hint warn">
                        已到上下文窗口的 80%：后端把这档当误配，实际会用
                        min(131072, max(8192, 窗口/4))，不是你选的这一个。
                      </p>
                    </div>

                    <!-- 思考预算：intention 分级、driver 每轮扩缩、schedule_engine 都按它算，
                         0 = 回落到「输出（最大回复长度）」。此前面板从不提交，
                         所有模型被钉死在建表默认 8192 上。 -->
                    <div class="form-group">
                      <label class="form-label">思考预算（reasoning tokens）</label>
                      <input
                        v-model.number="form.reasoning_tokens"
                        class="form-input"
                        type="number"
                        min="0"
                        max="1000000"
                        step="1024"
                      />
                      <p class="form-hint">思考/推理内容的独立上限；填 0 = 跟随上面的输出长度</p>
                    </div>
                  </div>
                </Transition>
              </div>
            </div>

            <!-- Footer -->
            <div class="modal-footer">
              <button class="btn btn-ghost" @click="closeDialog">取消</button>
              <button class="btn btn-primary" :disabled="!canSave || saving" @click="handleSave">
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
import { reactive, ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import type { ModelConfig, ModelConfigPayload, ModelDefaultMode } from '@/api/client'

const store = useSettingsStore()
const toast = useToastStore()
const { confirm } = useConfirm()

// ── 提供商定义 ──
interface ProviderDef {
  id: string
  label: string
  baseUrl: string
  isLocal: boolean
}

const PROVIDERS: ProviderDef[] = [
  { id: 'vllm', label: '自定义', baseUrl: 'http://localhost:8000/v1', isLocal: true },
  { id: 'deepseek', label: 'DeepSeek', baseUrl: 'https://api.deepseek.com/v1', isLocal: false },
  { id: 'openai', label: 'OpenAI', baseUrl: 'https://api.openai.com/v1', isLocal: false },
  { id: 'qwen', label: 'Qwen', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', isLocal: false },
  { id: 'kimi', label: 'Kimi', baseUrl: 'https://api.moonshot.cn/v1', isLocal: false },
  { id: 'glm', label: 'GLM', baseUrl: 'https://open.bigmodel.cn/api/paas/v4', isLocal: false },
  { id: 'xiaomi', label: 'Xiaomi', baseUrl: 'https://api.xiaomimimo.com/v1', isLocal: false },
  { id: 'anthropic', label: 'Anthropic', baseUrl: 'https://api.anthropic.com/v1', isLocal: false },
]

// 上下文窗口默认值必须与后端一致：models_config.max_tokens 列默认 16384，
// 消费方（routes/agent.py 的 max_context_tokens、schedule_engine）也都是 16384。
// 面板这里若用 4096，新建模型会把窗口悄悄缩到 1/4（safe_max_tokens 随之压低输出
// 预算、历史被更早截断），编辑一条 max_tokens 为 NULL 的旧行更会把 4096 落库。
const DEFAULT_CONTEXT_TOKENS = 16384
const DEFAULT_OUTPUT_TOKENS = 4096
// 与后端 CreateModelRequest.reasoning_tokens 的默认值同口径（routes/settings.py）。
// 面板此前不提供这一项，所有模型都被钉在建表默认上；提供后新建默认仍取该值。
const DEFAULT_REASONING_TOKENS = 8192
const DEFAULT_TOP_P = 0.9

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
  temperature: 0.7,
  top_p: DEFAULT_TOP_P,
  max_tokens: DEFAULT_CONTEXT_TOKENS,
  max_output_tokens: DEFAULT_OUTPUT_TOKENS,
  reasoning_tokens: DEFAULT_REASONING_TOKENS,
  is_default_ssh: false,
})

const selectedProvider = computed(() => PROVIDERS.find(p => p.id === form.provider))
const isLocalProvider = computed(() => selectedProvider.value?.isLocal ?? false)
/**
 * API Key 必填只对本面板认识的非本地提供商成立。
 * 后端 provider 列没有白名单、建表默认就是 'custom'，而它不在这份可选项里 ——
 * 按"远程必填 key"处理会让这条已存在的记录永久灰掉保存按钮（字段还显示着，
 * 但空 key + 灰按钮 = 谁都改不动它）。未知 provider 照常显示 key 字段、可填可留，
 * 只是不再强制：面板自己的选项表不能反过来把库里已有的数据锁死。
 */
const keyRequired = computed(() => !!selectedProvider.value && !isLocalProvider.value)
// 非本地部署时 API Key 必填；本地部署不需要
const canSave = computed(() => {
  if (!form.name || !form.base_url || !form.provider) return false
  if (keyRequired.value) {
    // 新建时必须填 key；编辑时 key 不能为空（除非和原脱敏值一样表示保留原值）
    const key = (form.api_key || '').trim()
    if (!isEditing.value && !key) return false
    if (isEditing.value && !key && !hasExistingKey.value) return false
  }
  return true
})

// 网络往返期间禁用保存入口：连点会重复创建同名模型/叠加并发更新
const saving = ref(false)

/**
 * 对话框会话号。doSave 要 await 一次网络往返，期间用户完全可以取消后重开另一个对话框
 * （编辑 → 取消 → 添加），而原实现在 await 之后才读 isEditing/editingId：
 * 回填时会把刚打开的"添加模型"对话框关掉，toast 还说成「模型已添加」。
 * 每次开对话框 +1，await 之后先比会话号，不是自己那一轮就不关窗、不报成功。
 */
let dialogSeq = 0

/**
 * 预设档 + 当前值。库里可能是 8000 这类不在 5 枚预设档上的值（手填过的旧数据、
 * 别的写入方），而芯片高亮用的是 `=== opt` —— 于是一枚都不亮，用户看不到现在
 * 是多少，也无法原样保留（只能整格换成列表值）。补一枚显示实际值的芯片：
 * 它自然就是亮的那枚，不点它则原值不变。
 */
const withCurrent = (opts: number[], current: number) =>
  opts.includes(current) ? opts : [...opts, current].sort((a, b) => a - b)
const inputTokenOptions = computed(() => withCurrent(INPUT_TOKEN_OPTIONS, form.max_tokens))
const outputTokenOptions = computed(() => withCurrent(OUTPUT_TOKEN_OPTIONS, form.max_output_tokens))

/**
 * safe_tokens.safe_max_tokens 的误配降级条件：max_output_tokens ≥ 上下文窗口的 80%
 * 时后端不采纳这个值，改用 min(131072, max(8192, 窗口/4))。芯片选到窗口的一半以上
 * 就会撞上它 —— 存进去的是选中的那一档、跑起来的却是另一档，而 UI 上没有任何痕迹。
 */
const outputDegraded = computed(() =>
  form.max_tokens > 0 && form.max_output_tokens > 0
  && form.max_output_tokens >= form.max_tokens * 0.8)

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
  form.temperature = 0.7
  form.top_p = DEFAULT_TOP_P
  form.max_tokens = DEFAULT_CONTEXT_TOKENS
  form.max_output_tokens = DEFAULT_OUTPUT_TOKENS
  form.reasoning_tokens = DEFAULT_REASONING_TOKENS
  form.is_default_ssh = false
}

function openCreateDialog() {
  resetForm()
  dialogSeq++
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
  dialogSeq++
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
  form.temperature = m.temperature ?? 0.7
  // ?? 而不是 ||：top_p=0 与 reasoning_tokens=0（= 跟随输出预算）都是合法读数，
  // 用 || 会把它们显示成默认值，于是"看一眼再保存"就把 0 改成了 0.9 / 8192。
  // 旧行为止于「面板根本不提交这两个键」，所以 NULL 行保持 NULL；现在表单
  // 看得见它们，NULL 行按后端默认反显并在保存时落库（与运行时的回落值一致）。
  form.top_p = m.top_p ?? DEFAULT_TOP_P
  form.max_tokens = m.max_tokens ?? DEFAULT_CONTEXT_TOKENS
  form.max_output_tokens = m.max_output_tokens ?? DEFAULT_OUTPUT_TOKENS
  form.reasoning_tokens = m.reasoning_tokens ?? DEFAULT_REASONING_TOKENS
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
  if (!canSave.value || saving.value) return
  saving.value = true
  try {
    await doSave()
  } finally {
    saving.value = false
  }
}

async function doSave() {
  const trim = (s: string) => (s || '').trim()
  // 数字输入框可以被清空或手打成越界值（min/max 只约束步进器，不拦手打）。
  // 原样提交会撞上后端 Field 的同款区间（0-2 / 1-1000000）→ pydantic 原文进 toast。
  // 提交前按后端口径钳制，并把钳好的值写回表单，避免界面显示 5、实际存 2。
  //
  // 空串必须先短路回 fallback：`type=number` 上的 v-model.number 在框被清空时给的是
  // ''（looseToNumber 的 parseFloat('') 是 NaN → 原样返回），而 Number('') === 0 且
  // 有限 —— 于是"清空温度"会静默存成 0、"清空思考预算"存成 0（后端读作"跟随输出预算"），
  // 落在 lo=1 的字段上更是直接把窗口写成 1 token。清空 = 没填，不是填了 0。
  const clampNum = (value: unknown, fallback: number, lo: number, hi: number) => {
    if (value === '' || value === null || value === undefined) return fallback
    const n = typeof value === 'number' ? value : Number(value)
    if (!Number.isFinite(n)) return fallback
    return Math.min(hi, Math.max(lo, n))
  }
  form.temperature = clampNum(form.temperature, 0.7, 0, 2)
  form.top_p = clampNum(form.top_p, DEFAULT_TOP_P, 0, 1)
  form.max_tokens = clampNum(form.max_tokens, DEFAULT_CONTEXT_TOKENS, 1, 1000000)
  form.max_output_tokens = clampNum(form.max_output_tokens, DEFAULT_OUTPUT_TOKENS, 1, 1000000)
  // 下限 0 与后端 Field(ge=0) 一致：0 是「跟随输出预算」的合法读数，不能钳成 1
  form.reasoning_tokens = clampNum(form.reasoning_tokens, DEFAULT_REASONING_TOKENS, 0, 1000000)
  const payload: ModelConfigPayload = {
    name: trim(form.name),
    base_url: trim(form.base_url),
    model_id: trim(form.model_id),  // 始终发送字符串（含空串），确保清空也能落库
    provider: form.provider,
    temperature: form.temperature,
    top_p: form.top_p,
    max_tokens: form.max_tokens,
    max_output_tokens: form.max_output_tokens,
    reasoning_tokens: form.reasoning_tokens,
  }
  // 「设为 SSH 默认」只在创建时生效；已存在模型的默认切换走独立入口，避免与编辑语义混淆
  if (!isEditing.value && form.is_default_ssh) payload.is_default_ssh = true
  const trimmedKey = trim(form.api_key)
  // 本地部署不发送 api_key；编辑时若值与原脱敏值相同表示未修改，不发送
  if (!isLocalProvider.value && trimmedKey) {
    if (!(isEditing.value && trimmedKey === maskedApiKey.value)) {
      payload.api_key = trimmedKey
    }
  }
  let result: ModelConfig | null
  // 会话号与"这一轮到底是编辑还是新建"都必须在 await 之前定死（见 dialogSeq 注）
  const mySeq = dialogSeq
  const wasEditing = isEditing.value
  const wasEditingId = editingId.value
  if (wasEditing) {
    result = await store.updateModelConfig(wasEditingId, payload)
  } else {
    result = await store.createModelConfig(payload)
  }
  if (mySeq !== dialogSeq) return
  if (result) {
    // 持久化校验在 store.createModelConfig 内置：若刚创建的 id 不在列表，store.modelsError 会被置告警
    if (!wasEditing && store.modelsError) {
      toast.error(`模型未持久化：${store.modelsError}`, 8000)
      // 不关对话框，让用户看到 toast 并可重试
    } else {
      // 保存成功后关闭窗口（× / 取消 / 保存均有关闭入口；点遮罩不关，防止误触丢表单）
      closeDialog()
      toast.success(wasEditing ? '模型已更新' : '模型已添加')
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
  // 按钮已经置灰，这里兜"另一个窗口刚把它设成默认"的竞态：
  // 后端那句 ValueError 是英文原文，直通 toast 就等于把内部错误码给用户看
  const row = store.modelConfigs.find(m => m.id === id)
  if (row && isAnyDefault(row)) {
    toast.warning('默认模型不可删除：先把默认切到别的模型')
    return
  }
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
    // 后端拒绝删除默认模型时给的是英文 ValueError 原文，直通 toast 等于把内部错误码当文案
    const raw = store.modelsError || ''
    toast.error(
      /default/i.test(raw) ? '默认模型不可删除：先把默认切到别的模型' : (raw || '删除失败'),
      6000,
    )
  }
}

// ── A1 / C9：设为默认（两档）──
// 审查R2修复：单值 defaultingId 在并发设置时会互相清忙态（后发起的把前一个的
// 忙标记顶掉，前一个完成后又置空 → 还在跑的条目丢转圈/按钮重新可点），改 Set 增删。
// round 22 起同一张卡有两枚控件（ssh / global），键必须带档位，否则两枚互相顶忙态。
const defaultingIds = ref(new Set<string>())
const key = (id: string, mode: ModelDefaultMode) => `${id}:${mode}`

const DEFAULT_MODE_LABEL: Record<ModelDefaultMode, string> = {
  ssh: 'SSH',
  global: '全局',
}

async function handleSetDefault(m: ModelConfig, mode: ModelDefaultMode) {
  const label = DEFAULT_MODE_LABEL[mode]
  const ok = await confirm({
    title: `设为 ${label} 默认模型`,
    message: `将「${m.name}」设为${label}模式默认模型？当前${label}默认会被取消，下一次相关调用即生效。`,
    confirmText: '设为默认',
  })
  if (!ok) return
  const k = key(m.id, mode)
  defaultingIds.value.add(k)
  const success = await store.setDefaultModel(m.id, mode)
  defaultingIds.value.delete(k)
  if (success) {
    toast.success(`「${m.name}」已设为${label}默认`)
  } else {
    toast.error(store.modelsError || '设置默认模型失败', 6000)
  }
}

// ── 测试 ──
// 审查R2修复：同上，多模型并发测试互清 testingId，改 Set 增删
const testingIds = ref(new Set<string>())
const testStatus = reactive<Record<string, { status: 'ok' | 'error' | 'testing'; message: string; latencyMs?: number }>>({})

async function handleTest(id: string) {
  testingIds.value.add(id)
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
    testingIds.value.delete(id)
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

<style scoped src="./settings-panel-base.css"></style>
<style scoped>
/* 面板骨架与 .setting-select 见 settings-panel-base.css */

.models-panel { height: 100%; display: flex; flex-direction: column; }

/* 有意的本地差异：卡片网格隐藏滚动条（骨架的 overflow-y / padding-right 仍生效） */
.panel-body { scrollbar-width: none; }
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
.state-error { color: var(--danger-text); flex-direction: column; align-items: flex-start; gap: 8px; }

.spinner {
  width: 16px; height: 16px;
  border: 2px solid var(--border-subtle);
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
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 14px 16px;
  transition: border-color 0.15s;
}
.model-card:hover { border-color: var(--border-default); }
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
.field-monospace { font-family: var(--font-mono); font-size: 11px; }

.model-params {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}
.param-chip {
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
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
.badge-cap { background: color-mix(in srgb, var(--status-success-text) 13%, transparent); color: var(--status-success-text); border: 1px solid color-mix(in srgb, var(--status-success-text) 27%, transparent); }
.badge-provider { background: color-mix(in srgb, var(--text-secondary) 13%, transparent); color: var(--text-secondary); border: 1px solid color-mix(in srgb, var(--text-primary) 8%, transparent); }

/* 测试状态 */
.test-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
}
.test-ok { color: var(--status-success-text); }
.test-error { color: var(--danger-text); }
.test-testing { color: var(--text-secondary); }
.test-latency {
  margin-left: auto;
  font-size: 11px;
  font-weight: 500;
  padding: 1px 8px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--status-success-text) 13%, transparent);
  color: var(--status-success-text);
  border: 1px solid color-mix(in srgb, var(--status-success-text) 27%, transparent);
}

/* 空状态 */
.model-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  color: var(--text-tertiary);
  gap: 8px;
}
.model-empty p { margin: 0; font-size: 13px; }
.model-empty .hint { font-size: 12px; color: var(--text-tertiary); }

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
.btn-primary { background: var(--accent-primary); color: var(--accent-on); }
.btn-primary:hover { background: color-mix(in srgb, var(--accent-primary) 85%, white); }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-ghost { background: transparent; color: var(--text-secondary); }
.btn-ghost:hover { background: var(--bg-hover); color: var(--text-primary); }
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
.btn-icon:hover { background: var(--bg-hover); color: var(--text-primary); }
.btn-icon:disabled { opacity: 0.3; cursor: not-allowed; }
.btn-icon-danger:hover { background: color-mix(in srgb, var(--danger-text) 10%, transparent); color: var(--danger-text); }
.btn-icon-default-active { color: var(--accent-yellow); opacity: 1 !important; }
.btn-icon-default-active:disabled { opacity: 0.75 !important; cursor: default; }

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
  border: 1px solid var(--border-subtle);
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
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.modal-header h4 { margin: 0; font-size: 14px; font-weight: 600; }

.modal-close {
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: none; border-radius: 4px;
  color: var(--text-secondary); cursor: pointer; transition: all 0.15s;
}
.modal-close:hover { background: var(--bg-hover); color: var(--text-primary); }

.modal-body { padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }
.modal-footer {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

/* ── Form ── */
.form-group { display: flex; flex-direction: column; gap: 4px; }
.form-label { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
.form-hint { margin: 0; font-size: 11px; color: var(--text-tertiary); }
.form-hint.warn { color: var(--status-warn-text); }
.required { color: var(--danger-text); }
.form-input {
  width: 100%;
  padding: 8px 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
  box-sizing: border-box;
}
.form-input:focus { border-color: var(--accent-primary); box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent-primary) 20%, transparent); }
.form-input::placeholder { color: var(--text-tertiary); }

.label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.local-note {
  font-size: 11px;
  color: var(--status-success-text);
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
.btn-eye:hover { color: var(--accent-primary); background: var(--bg-hover); }

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
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 13px;
  transition: border-color 0.15s;
}
.provider-selected:hover { border-color: var(--border-default); }
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
  border: 1px solid var(--border-subtle);
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
  border: 1px solid var(--border-subtle);
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
  background: var(--bg-input);
  transition: all 0.15s;
  user-select: none;
}
.advanced-toggle:hover { color: var(--text-primary); }
.advanced-toggle svg { transition: transform 0.2s; color: var(--text-tertiary); }
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
  accent-color: var(--accent-primary);
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
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
  font-family: inherit;
}
.token-chip:hover { border-color: var(--border-default); color: var(--text-primary); }
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
