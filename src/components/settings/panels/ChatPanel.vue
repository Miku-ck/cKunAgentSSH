<template>
  <div class="panel">
    <header class="panel-header">
      <h3>对话</h3>
      <p class="panel-desc">配置对话行为的默认偏好</p>
    </header>
    <div class="panel-body">
      <SettingRow label="发送快捷键" desc="选择发送消息的快捷键">
        <select v-model="settings.chat.sendShortcut" class="setting-select">
          <option value="enter">Enter 发送</option>
          <option value="shiftEnter">Shift + Enter 发送</option>
        </select>
      </SettingRow>

      <SettingRow label="思考链默认" desc="推理模型的思考过程默认展开">
        <select v-model="settings.chat.thinkingExpanded" class="setting-select">
          <option :value="false">折叠</option>
          <option :value="true">展开</option>
        </select>
      </SettingRow>

      <SettingRow label="自动滚动" desc="新消息自动滚动到底部">
        <ToggleSwitch v-model="settings.chat.autoScroll" />
      </SettingRow>

      <SettingRow label="消息字号" desc="对话气泡中的文字大小">
        <select v-model.number="settings.chat.messageFontSize" class="setting-select">
          <option :value="12">12px</option>
          <option :value="13">13px</option>
          <option :value="14">14px</option>
          <option :value="15">15px</option>
        </select>
      </SettingRow>

      <SettingRow label="Markdown 渲染" desc="启用 Markdown 格式渲染">
        <ToggleSwitch v-model="settings.chat.markdown" />
      </SettingRow>

      <SettingRow label="自动技能匹配" desc="根据触发词自动调用已启用的 skill">
        <ToggleSwitch v-model="settings.chat.autoSkillMatch" />
      </SettingRow>

      <SettingRow label="上下文窗口" desc="保留最近多少条消息作为上下文">
        <div class="slider-row">
          <input
            type="range"
            v-model.number="settings.chat.contextWindow"
            min="5"
            max="50"
            step="1"
            class="setting-range"
          />
          <span class="slider-value">{{ settings.chat.contextWindow }} 条</span>
        </div>
      </SettingRow>

      <div class="section-divider" />

      <div class="default-model-info">
        <span class="default-model-label">当前默认模型（ssh）</span>
        <span class="default-model-name">{{ defaultModelName }}</span>
        <span v-if="defaultModelId" class="default-model-id">{{ defaultModelId }}</span>
      </div>

      <div class="section-divider" />

      <h4 class="section-title">后处理</h4>

      <SettingRow label="后处理总开关" desc="单轮对话完成后执行标题生成、记忆提取、技能生成">
        <ToggleSwitch v-model="settings.postProcess.enabled" />
      </SettingRow>

      <SettingRow label="自动生成标题" desc="首轮对话后根据内容生成会话标题">
        <ToggleSwitch v-model="settings.postProcess.generateTitle" />
      </SettingRow>

      <SettingRow label="自动提取记忆" desc="从对话中提取用户偏好和关键事实">
        <ToggleSwitch v-model="settings.postProcess.extractMemory" />
      </SettingRow>

      <SettingRow label="自动生成技能" desc="从对话中沉淀可复用技能（每 3 轮触发）">
        <ToggleSwitch v-model="settings.postProcess.generateSkill" />
      </SettingRow>

      <div class="section-divider" />

      <h4 class="section-title">会话保留</h4>

      <SettingRow label="自动清理旧会话" desc="开启后定期删除最旧的已结束会话（进行中的会话永不删除）。默认关闭，建议开启以避免数据库长期膨胀">
        <ToggleSwitch :model-value="retention.enabled" @update:model-value="onRetentionToggle" />
      </SettingRow>

      <SettingRow
        v-if="retention.enabled"
        label="保留会话数"
        desc="按最近活跃保留的会话条数，超出部分自动清理"
      >
        <input
          v-model.number="retention.keepCount"
          type="number"
          min="10"
          max="100000"
          class="setting-number"
          @change="onRetentionCountChange"
        />
      </SettingRow>
      <p v-if="retentionSaving" class="hint saving">保存中…</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import { api, toFriendlyMessage } from '@/api/client'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'

const settingsStore = useSettingsStore()
const settings = settingsStore.settings
const toast = useToastStore()

const defaultModelName = ref('加载中…')
const defaultModelId = ref('')

// ── 会话保留（服务端设置，默认关闭；删除用户数据，故默认不启用）──
const retention = ref<{ enabled: boolean; keepCount: number }>({ enabled: false, keepCount: 1000 })
const retentionSaving = ref(false)

async function loadRetention() {
  try {
    const data = await api.getConvRetention()
    // ?? 而不是 ||：keep_count=0 是后端允许写入的合法值（清空全部已结束会话），
    // 用 || 会把它显示成 1000 —— 界面说留 1000 条、实际在删用户的会话。
    retention.value = { enabled: !!data.enabled, keepCount: data.keep_count ?? 1000 }
  } catch {
    // 加载失败保持默认关闭（安全方向）
  }
}

async function saveRetention(patch: Partial<{ enabled: boolean; keepCount: number }>) {
  const prev = { ...retention.value }
  const next = {
    enabled: patch.enabled ?? prev.enabled,
    keepCount: patch.keepCount ?? prev.keepCount,
  }
  // 乐观更新
  retention.value = { ...next }
  retentionSaving.value = true
  try {
    await api.updateConvRetention({ enabled: next.enabled, keep_count: next.keepCount })
  } catch (e: unknown) {
    retention.value = { ...prev }
    // 失败只回滚开关、不提示 → 用户看到"点了没反应"，会反复重试
    toast.error(toFriendlyMessage(e, '会话保留设置保存失败'))
  } finally {
    retentionSaving.value = false
  }
}

function onRetentionToggle(v: boolean) {
  void saveRetention({ enabled: v })
}

function onRetentionCountChange() {
  const n = Math.max(10, Math.min(100000, Number(retention.value.keepCount) || 1000))
  retention.value.keepCount = n
  void saveRetention({ keepCount: n })
}

onMounted(async () => {
  void loadRetention()
  try {
    const model = await api.getDefaultModel('ssh')
    defaultModelName.value = model.name || model.model_id || model.id || '未配置'
    defaultModelId.value = model.model_id || ''
  } catch {
    defaultModelName.value = '未配置'
    defaultModelId.value = ''
  }
})
</script>

<style scoped>
.panel { height: 100%; display: flex; flex-direction: column; }
.panel-header { margin-bottom: 20px; flex-shrink: 0; }
.panel-header h3 {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.panel-desc {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
}
.panel-body {
  flex: 1;
  overflow-y: auto;
  padding-right: 4px;
}

/* 下拉选择 */
.setting-select {
  height: 28px;
  padding: 0 24px 0 8px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 12px;
  font-family: inherit;
  outline: none;
  cursor: pointer;
  -webkit-appearance: none;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12' fill='none' stroke='%238b949e' stroke-width='1.5' stroke-linecap='round'%3E%3Cpolyline points='3 5 6 8 9 5'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 6px center;
  transition: border-color 0.15s ease;
}
.setting-select:hover { border-color: var(--border-default); }
.setting-select:focus { border-color: var(--accent-primary); }

/* 滑块行 */
.slider-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.setting-range {
  width: 120px;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--bg-slider);
  border-radius: 2px;
  outline: none;
  cursor: pointer;
}
.setting-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--accent-primary);
  border: 2px solid var(--bg-input);
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.4);
}
.slider-value {
  font-size: 12px;
  color: var(--text-secondary);
  min-width: 40px;
  text-align: right;
}

.setting-number {
  width: 100px;
  height: 28px;
  padding: 0 8px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 12px;
  font-family: inherit;
  outline: none;
}
.setting-number:focus { border-color: var(--accent-primary); }
.hint.saving { margin: 8px 0 0; font-size: 11px; color: var(--text-tertiary); }

.section-divider {
  height: 1px;
  background: var(--border-subtle);
  margin: 20px 0 16px;
}

.section-title {
  margin: 0 0 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.default-model-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 0 4px;
}

.default-model-label {
  font-size: 10px;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.default-model-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}

.default-model-id {
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
}
</style>
