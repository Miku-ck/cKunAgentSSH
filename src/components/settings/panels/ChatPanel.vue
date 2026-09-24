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

      <!-- E2：三态而非开关。'inherit' 保留"不发该字段"，让后端演化配置接管；
           强制开/关覆盖演化。选 inherit 就是回到演化默认（多数模型 off） -->
      <SettingRow label="深度思考模式" desc="Agent 运行是否强制启用思考链，或交由演化配置决定">
        <select v-model="settings.chat.enableThinking" class="setting-select">
          <option value="inherit">跟随演化（默认）</option>
          <option value="on">强制开启</option>
          <option value="off">强制关闭</option>
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
        <SettingSlider v-model="settings.chat.contextWindow" :min="5" :max="100" unit=" 条" />
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
import { ref, computed, onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import { api, toFriendlyMessage } from '@/api/client'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import SettingSlider from '@/components/settings/fields/SettingSlider.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'

const settingsStore = useSettingsStore()
const settings = settingsStore.settings
const toast = useToastStore()

// A1：默认模型显示改为响应式派生自 store.modelConfigs。
// 原来是 onMounted 里 api.getDefaultModel 一次性拉 → 用户切了默认后
// 本面板仍显示旧值。改派生后，只要 fetchModelConfigs 刷新过一次
// （SettingsDialog 打开时 ModelsPanel 会拉），卡片 star 与这里就同步。
const defaultSshModel = computed(() => {
  const list = settingsStore.modelConfigs
  if (!list.length) return null
  return list.find((m) => m.is_default_ssh) ?? list.find((m) => m.is_default) ?? list[0]
})
const defaultModelName = computed(() => {
  const m = defaultSshModel.value
  if (!m) return '未配置'
  return m.name || m.model_id || m.id || '未配置'
})
const defaultModelId = computed(() => defaultSshModel.value?.model_id || '')

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
  // 首次进对话面板时若还没拉过模型列表，这里补一次；有数据后 computed 会自动填
  if (!settingsStore.modelConfigs.length) void settingsStore.fetchModelConfigs()
})
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped>
/* 面板骨架与 .setting-select 见 settings-panel-base.css */

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
