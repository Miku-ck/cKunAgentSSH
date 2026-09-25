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
        <SettingSlider v-model="memory.maxItems" :min="10" :max="500" :step="10" unit=" 条" />
      </SettingRow>

      <SettingRow label="检索注入预算" desc="每轮运行按相关性注入 system prompt 的历史经验上限（token）">
        <SettingSlider v-model="memory.injectBudgetTokens" :min="200" :max="8000" :step="100" unit=" t" />
      </SettingRow>

      <SettingRow label="画像块预算" desc="常驻画像块（用户偏好/主机画像/规则）的独立上限 —— 与注入预算分账，改大这一项不会削减那一侧">
        <SettingSlider v-model="memory.blocksBudgetTokens" :min="200" :max="8000" :step="100" unit=" t" />
      </SettingRow>

      <SettingRow label="检索等待上限" desc="预检索超过这个秒数就放弃（降级为按新近兜底），避免首字节被慢向量服务拖住">
        <SettingSlider v-model="memory.searchTimeoutS" :min="0.5" :max="30" :step="0.5" unit=" s" />
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
        修改即时同步到后端；同步失败时自动回滚到上一次成功值。
      </p>

      <SettingRow label="记忆管理" desc="浏览/检索/置顶/编辑/失效已有记忆，查看对账队列与画像块（含历史回滚）">
        <button type="button" class="manage-btn" @click="showManager = !showManager">
          {{ showManager ? '收起记忆管理' : '打开记忆管理' }}
        </button>
      </SettingRow>

      <!-- 懒挂载：不开就不发 stats/列表请求 -->
      <AgentMemoryView v-if="showManager" @close="showManager = false" />
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
 * 记忆体本身（列表/编辑/新增/失效/审计/对账队列/画像块）内嵌 AgentMemoryView，
 * 点「打开记忆管理」才挂载。
 */
import { computed, onMounted, ref } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import SettingSlider from '@/components/settings/fields/SettingSlider.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'
import AgentMemoryView from '@/components/agent/AgentMemoryView.vue'

const settingsStore = useSettingsStore()
const memory = computed(() => settingsStore.settings.memory)
const showManager = ref(false)

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

<style scoped src="./settings-panel-base.css"></style>
<style scoped>
/* 面板骨架与 .setting-select 见 settings-panel-base.css；滑杆样式见 SettingSlider.vue */

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
.manage-btn {
  padding: 4px 10px;
  font-size: 12px;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.manage-btn:hover { background: var(--bg-hover, rgba(255, 255, 255, .06)); }
</style>
