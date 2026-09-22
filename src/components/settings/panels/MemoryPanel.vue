<template>
  <div class="panel">
    <header class="panel-header">
      <h3>记忆</h3>
      <p class="panel-desc">Agent 长期记忆的注入预算与容量上限 —— 影响每次运行写进 system prompt 的记忆条目与常驻画像块</p>
    </header>

    <div class="panel-body">
      <SettingRow label="记忆总开关" desc="关闭后 Agent 运行时不再注入历史记忆，也不会从对话沉淀新记忆">
        <ToggleSwitch v-model="memory.enabled" />
      </SettingRow>

      <SettingRow label="容量上限" desc="每个记忆类别保留多少条活跃条目，超出由后台整理退休（手动新增的条目不受此限）">
        <div class="slider-row">
          <input
            type="range"
            v-model.number="memory.maxItems"
            min="10"
            max="500"
            step="10"
            class="setting-range"
          />
          <span class="slider-value">{{ memory.maxItems }} 条</span>
        </div>
      </SettingRow>

      <SettingRow label="检索注入预算" desc="每轮运行按相关性注入 system prompt 的历史经验上限（token）">
        <div class="slider-row">
          <input
            type="range"
            v-model.number="memory.injectBudgetTokens"
            min="200"
            max="8000"
            step="100"
            class="setting-range"
          />
          <span class="slider-value">{{ memory.injectBudgetTokens }} t</span>
        </div>
      </SettingRow>

      <SettingRow label="画像块预算" desc="常驻画像块（用户偏好/主机画像/规则）的独立上限 —— 与注入预算分账，改大这一项不会削减那一侧">
        <div class="slider-row">
          <input
            type="range"
            v-model.number="memory.blocksBudgetTokens"
            min="200"
            max="8000"
            step="100"
            class="setting-range"
          />
          <span class="slider-value">{{ memory.blocksBudgetTokens }} t</span>
        </div>
      </SettingRow>

      <SettingRow label="检索等待上限" desc="预检索超过这个秒数就放弃（降级为按新近兜底），避免首字节被慢向量服务拖住">
        <div class="slider-row">
          <input
            type="range"
            v-model.number="memory.searchTimeoutS"
            min="0.5"
            max="30"
            step="0.5"
            class="setting-range"
          />
          <span class="slider-value">{{ memory.searchTimeoutS }} s</span>
        </div>
      </SettingRow>

      <SettingRow label="后台整理调用模型" desc="开启后由模型消解矛盾与重复；关闭则只做阈值去重，记忆仍会写入">
        <ToggleSwitch v-model="memory.reconcileLlmEnabled" />
      </SettingRow>

      <div class="section-divider" />

      <div class="memory-stats">
        <span class="stats-label">当前记忆状态</span>
        <span class="stats-value">{{ statsText }}</span>
      </div>
      <p class="foot-hint">
        修改即时同步到后端；同步失败时自动回滚到上一次成功值。已有记忆的浏览与删除请通过 AI 面板顶部的"记忆"入口。
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 记忆设置面板 —— 字段与后端 MemorySettingsRequest（v2 六项）严格同集合。
 *
 * PUT 是全字段覆盖，面板漏渲染一项就等于把它静默重置回默认值，所以这里
 * 不做"可选展示"，六个字段全部可见可改。
 * 全部通过 settings store 的 deep watch 自动同步（含 R6 回滚基线），面板自身
 * 不再直接调 updateMemorySettings，避免与 watcher 双写。
 * 记忆体本身（列表/编辑/删除）在 AgentMemoryView（AI 面板记忆入口），不重复。
 */
import { computed, onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'

const settingsStore = useSettingsStore()
const memory = computed(() => settingsStore.settings.memory)

const statsText = computed(() => {
  const m = memory.value
  return m.enabled
    ? `启用 · 上限 ${m.maxItems} 条/类 · 注入 ${m.injectBudgetTokens}t + 画像 ${m.blocksBudgetTokens}t · `
      + `检索等待 ${m.searchTimeoutS}s · 整理${m.reconcileLlmEnabled ? '调用模型' : '仅阈值去重'}`
    : '已关闭 · Agent 运行时不注入也不写入'
})

onMounted(() => {
  // loadMemorySettings 已在应用启动路径调用；面板打开时再补拉一次，
  // 覆盖"启动时后端尚未就绪 → 静默降级到 localStorage 默认值"的场景。
  void settingsStore.loadMemorySettings()
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
.slider-row { display: flex; align-items: center; gap: 12px; }
.setting-range {
  width: 160px; height: 4px;
  -webkit-appearance: none; appearance: none;
  background: var(--bg-slider);
  border-radius: 2px; outline: none; cursor: pointer;
}
.setting-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--accent-primary);
  border: 2px solid var(--bg-input);
  box-shadow: 0 1px 3px rgba(0,0,0,0.35);
  cursor: pointer;
}
.slider-value {
  font-size: 12px; color: var(--text-secondary);
  min-width: 48px; text-align: right;
}
.section-divider {
  height: 1px; background: var(--border-subtle);
  margin: 20px 0;
}
.memory-stats {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px;
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  font-size: 12px;
}
.stats-label { color: var(--text-secondary); }
.stats-value { color: var(--text-primary); font-weight: 500; }
.foot-hint {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
  line-height: 1.5;
}
</style>
